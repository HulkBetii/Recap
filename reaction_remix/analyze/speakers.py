from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import re

from common.schema import ReactionSpeakerCluster, ReactionTurn


class SpeechBrainSpeakerClusterer:
    def __init__(self, *, threshold: float, device: str, cache_dir: Path) -> None:
        self.threshold = threshold
        self.device = device
        self.cache_dir = cache_dir

    def cluster(self, audio_path: Path, turns: list[ReactionTurn]) -> dict[int, tuple[str, float]]:
        if not turns:
            return {}
        try:
            import numpy as np
            import torch
            import torchaudio
            from sklearn.cluster import AgglomerativeClustering
            from speechbrain.inference.speaker import EncoderClassifier
            from speechbrain.utils.fetching import LocalStrategy
        except ImportError as exc:
            raise RuntimeError("SpeechBrain, Torch, Torchaudio and scikit-learn are required for speaker clustering") from exc
        run_device = self.device
        if run_device == "auto":
            run_device = "cuda" if torch.cuda.is_available() else "cpu"
        classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(self.cache_dir),
            run_opts={"device": run_device},
            local_strategy=LocalStrategy.COPY,
        )
        waveform, sample_rate = torchaudio.load(str(audio_path))
        waveform = waveform.mean(dim=0)
        units = _speaker_units(turns)
        embeddings: list[object] = []
        for unit in units:
            start = max(0, round(unit.tc_start * sample_rate))
            end = min(waveform.shape[-1], round(unit.tc_end * sample_rate))
            clip = waveform[start:end]
            minimum = max(1, round(sample_rate * 0.5))
            if clip.numel() < minimum:
                clip = torch.nn.functional.pad(clip, (0, minimum - clip.numel()))
            embedding = classifier.encode_batch(clip.unsqueeze(0)).squeeze().detach().cpu().numpy()
            embeddings.append(embedding)
        matrix = np.stack(embeddings)
        if len(units) == 1:
            labels = np.array([0])
        else:
            kwargs = {
                "n_clusters": None,
                "distance_threshold": self.threshold,
                "linkage": "average",
            }
            try:
                labels = AgglomerativeClustering(metric="cosine", **kwargs).fit_predict(matrix)
            except TypeError:
                labels = AgglomerativeClustering(affinity="cosine", **kwargs).fit_predict(matrix)
        output: dict[int, tuple[str, float]] = {}
        for label in sorted(set(int(value) for value in labels)):
            indices = [index for index, value in enumerate(labels) if int(value) == label]
            centroid = matrix[indices].mean(axis=0)
            centroid_norm = float(np.linalg.norm(centroid)) or 1.0
            for index in indices:
                vector = matrix[index]
                similarity = float(np.dot(vector, centroid) / ((float(np.linalg.norm(vector)) or 1.0) * centroid_norm))
                for turn_id in units[index].turn_ids:
                    output[turn_id] = (f"speaker-{label:03d}", max(0.0, min(1.0, similarity)))
        return output


@dataclass
class _SpeakerUnit:
    tc_start: float
    tc_end: float
    turn_ids: list[int] = field(default_factory=list)
    languages: set[str] = field(default_factory=set)


def _speaker_units(turns: list[ReactionTurn]) -> list[_SpeakerUnit]:
    units: list[_SpeakerUnit] = []
    for turn in sorted(turns, key=lambda item: (item.tc_start, item.tc_end)):
        if turn.language != "und":
            language = {turn.language}
        elif re.search(r"[ぁ-んァ-ン一-龯]", turn.text):
            language = {"ja"}
        elif re.search(r"[A-Za-z]", turn.text):
            language = {"latin"}
        else:
            language = set()
        if units:
            previous = units[-1]
            compatible_language = not previous.languages or not language or previous.languages == language
            if (
                compatible_language
                and turn.tc_start - previous.tc_end <= 0.65
                and turn.tc_end - previous.tc_start <= 15.0
            ):
                previous.tc_end = turn.tc_end
                previous.turn_ids.append(turn.turn_id)
                previous.languages.update(language)
                continue
        units.append(
            _SpeakerUnit(
                tc_start=turn.tc_start,
                tc_end=turn.tc_end,
                turn_ids=[turn.turn_id],
                languages=language,
            )
        )
    return units


def build_speaker_clusters(turns: list[ReactionTurn]) -> list[ReactionSpeakerCluster]:
    grouped: dict[str, list[ReactionTurn]] = defaultdict(list)
    for turn in turns:
        grouped[turn.speaker_id].append(turn)
    output: list[ReactionSpeakerCluster] = []
    for speaker_id, speaker_turns in sorted(grouped.items()):
        durations: dict[str, float] = defaultdict(float)
        total = 0.0
        for turn in speaker_turns:
            duration = turn.tc_end - turn.tc_start
            durations[turn.language] += duration
            total += duration
        output.append(
            ReactionSpeakerCluster(
                speaker_id=speaker_id,
                region_count=len({turn.region_id for turn in speaker_turns}),
                total_duration_s=total,
                language_ratios={key: value / total for key, value in durations.items()} if total else {},
                narrator_candidate=False,
                confidence=sum(turn.speaker_confidence for turn in speaker_turns) / len(speaker_turns),
            )
        )
    return output


def select_narrator_speaker(
    turns: list[ReactionTurn],
    clusters: list[ReactionSpeakerCluster],
    *,
    source_duration_s: float,
) -> tuple[str | None, list[ReactionSpeakerCluster]]:
    threshold_s = min(20.0, max(8.0, source_duration_s * 0.03))
    by_speaker: dict[str, list[ReactionTurn]] = defaultdict(list)
    for turn in turns:
        by_speaker[turn.speaker_id].append(turn)
    candidates: list[str] = []
    updated: list[ReactionSpeakerCluster] = []
    for cluster in clusters:
        own_turns = sorted(by_speaker[cluster.speaker_id], key=lambda item: item.tc_start)
        separated_pairs = 0
        for previous, current in zip(own_turns, own_turns[1:]):
            if any(
                other.speaker_id != cluster.speaker_id
                and other.tc_start >= previous.tc_end - 1e-6
                and other.tc_end <= current.tc_start + 1e-6
                for other in turns
            ):
                separated_pairs += 1
        is_candidate = (
            cluster.region_count >= 3
            and cluster.language_ratios.get("ja", 0.0) >= 0.90
            and cluster.total_duration_s >= threshold_s
            and separated_pairs >= 2
        )
        if is_candidate:
            candidates.append(cluster.speaker_id)
        updated.append(cluster.model_copy(update={"narrator_candidate": is_candidate}))
    if not candidates:
        return None, updated
    best = max(candidates, key=lambda speaker_id: next(item.total_duration_s for item in updated if item.speaker_id == speaker_id))
    return best, updated
