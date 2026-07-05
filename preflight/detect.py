from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from common.media import probe_duration
from common.schema import IntroDetection, NonStoryRange, VideoProfile
from datetime import datetime, timezone

INTRO_LABEL = "intro_opening"
STORY_LABELS = ("story scene from the movie",)
NON_STORY_LABELS = ("opening credits", "title card", "studio logo", "music montage", "end credits")

@dataclass(frozen=True)
class FrameSample:
    time_s: float
    path: Path

@dataclass(frozen=True)
class FrameScore:
    time_s: float
    story_score: float
    non_story_score: float
    label: str

class PreflightError(RuntimeError):
    pass

def sample_frames(input_path: Path, frames_dir: Path, *, max_intro_s: float, sample_every_s: float, duration_s: float) -> list[FrameSample]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    limit = min(max_intro_s, duration_s)
    samples: list[FrameSample] = []
    time_s = 0.0
    index = 0
    while time_s <= limit + 1e-6:
        path = frames_dir / f"intro-{index:03d}.jpg"
        if not path.exists():
            subprocess.run([
                "ffmpeg", "-y", "-ss", f"{time_s:.3f}", "-i", str(input_path),
                "-frames:v", "1", "-q:v", "3", str(path),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if path.exists() and path.stat().st_size > 0:
            samples.append(FrameSample(time_s=round(time_s, 3), path=path))
        time_s += sample_every_s
        index += 1
    return samples

def heuristic_scores(samples: list[FrameSample]) -> list[FrameScore]:
    scores: list[FrameScore] = []
    for sample in samples:
        # Conservative fallback: no automatic intro exclusion without visual model evidence.
        scores.append(FrameScore(sample.time_s, story_score=0.6, non_story_score=0.4, label="heuristic_uncertain"))
    return scores

def openclip_scores(samples: list[FrameSample], model_name: str = "ViT-B-32", pretrained: str = "laion2b_s34b_b79k") -> list[FrameScore]:
    try:
        import torch
        from PIL import Image
        import open_clip
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise PreflightError('openclip classifier requires optional deps: pip install -e ".[video-profile]"') from exc
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, preprocess = open_clip.create_model_and_transforms(model_name, pretrained=pretrained, device=device)
    tokenizer = open_clip.get_tokenizer(model_name)
    labels = list(STORY_LABELS + NON_STORY_LABELS)
    text = tokenizer(labels).to(device)
    with torch.no_grad():
        text_features = model.encode_text(text)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
    output: list[FrameScore] = []
    for sample in samples:
        image = preprocess(Image.open(sample.path).convert("RGB")).unsqueeze(0).to(device)
        with torch.no_grad():
            image_features = model.encode_image(image)
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            probs = (100.0 * image_features @ text_features.T).softmax(dim=-1)[0].detach().cpu().tolist()
        story_score = max(probs[:len(STORY_LABELS)])
        non_story_items = probs[len(STORY_LABELS):]
        non_story_score = max(non_story_items)
        label = NON_STORY_LABELS[non_story_items.index(non_story_score)] if non_story_score >= story_score else STORY_LABELS[0]
        output.append(FrameScore(sample.time_s, story_score=float(story_score), non_story_score=float(non_story_score), label=label))
    return output

def decide_intro(scores: list[FrameScore], *, confidence_threshold: float, uncertain_threshold: float, sample_every_s: float, max_intro_s: float) -> tuple[IntroDetection, list[NonStoryRange], list[str]]:
    warnings: list[str] = []
    if not scores:
        return IntroDetection(detected=False, confidence=0.0, reasons=["no_samples"]), [], ["no intro samples were extracted"]
    non_story_prefix: list[FrameScore] = []
    for score in scores:
        if score.non_story_score >= score.story_score and score.non_story_score >= uncertain_threshold:
            non_story_prefix.append(score)
            continue
        break
    if not non_story_prefix:
        confidence = max((score.non_story_score for score in scores), default=0.0)
        if confidence >= uncertain_threshold:
            warnings.append("uncertain_intro")
        return IntroDetection(detected=False, confidence=round(confidence, 3), reasons=["no_confident_non_story_prefix"]), [], warnings
    confidence = sum(score.non_story_score for score in non_story_prefix) / len(non_story_prefix)
    end_s = min(max_intro_s, non_story_prefix[-1].time_s + sample_every_s)
    reasons = sorted({score.label for score in non_story_prefix}) + ["non_story_visual_prefix"]
    high_non_story = [
        score for score in scores
        if score.non_story_score >= confidence_threshold and score.non_story_score >= score.story_score
    ]
    if len(high_non_story) >= 3:
        last_high = high_non_story[-1]
        last_high_index = scores.index(last_high)
        stable_story_run = 0
        for score in scores[last_high_index + 1:]:
            if score.story_score >= confidence_threshold and score.story_score > score.non_story_score:
                stable_story_run += 1
                if stable_story_run >= 3:
                    confidence = sum(score.non_story_score for score in high_non_story) / len(high_non_story)
                    end_s = min(max_intro_s, last_high.time_s + sample_every_s)
                    reasons = sorted({score.label for score in high_non_story}) + ["intercut_opening_sequence"]
                    intro = IntroDetection(detected=True, start_s=0.0, end_s=round(end_s, 3), confidence=round(confidence, 3), reasons=reasons)
                    return intro, [NonStoryRange(start_s=0.0, end_s=round(end_s, 3), label=INTRO_LABEL, confidence=round(confidence, 3))], warnings
            else:
                stable_story_run = 0

    if confidence >= confidence_threshold:
        intro = IntroDetection(detected=True, start_s=0.0, end_s=round(end_s, 3), confidence=round(confidence, 3), reasons=reasons)
        return intro, [NonStoryRange(start_s=0.0, end_s=round(end_s, 3), label=INTRO_LABEL, confidence=round(confidence, 3))], warnings

    warnings.append("uncertain_intro")
    return IntroDetection(detected=False, confidence=round(confidence, 3), reasons=reasons), [], warnings

def build_video_profile(
    input_path: Path,
    work_dir: Path,
    *,
    classifier: str,
    max_intro_s: float,
    sample_every_s: float,
    confidence_threshold: float,
    uncertain_threshold: float,
) -> VideoProfile:
    duration_s = probe_duration(input_path)
    samples = sample_frames(input_path, work_dir / "frames", max_intro_s=max_intro_s, sample_every_s=sample_every_s, duration_s=duration_s)
    if classifier == "heuristic":
        scores = heuristic_scores(samples)
    elif classifier == "openclip":
        scores = openclip_scores(samples)
    else:
        raise PreflightError(f"unsupported classifier: {classifier}")
    intro, ranges, warnings = decide_intro(scores, confidence_threshold=confidence_threshold, uncertain_threshold=uncertain_threshold, sample_every_s=sample_every_s, max_intro_s=max_intro_s)
    return VideoProfile(
        input_path=str(input_path),
        duration_s=duration_s,
        intro=intro,
        non_story_ranges=ranges,
        classifier=classifier,
        created_at=datetime.now(timezone.utc),
        warnings=warnings,
    )
