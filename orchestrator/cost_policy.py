from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from tts.providers import TtsProviderError, resolve_provider_order

QualityMode = Literal["low_cost", "balanced", "max_quality"]
ApiBudgetGuard = Literal["off", "warn", "block"]

@dataclass(frozen=True)
class CostPolicy:
    quality_mode: QualityMode
    text_llm_backend: str
    api_budget_guard: ApiBudgetGuard
    stages: dict[str, dict[str, Any]]
    warnings: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "quality_mode": self.quality_mode,
            "text_llm_backend": self.text_llm_backend,
            "api_budget_guard": self.api_budget_guard,
            "stages": self.stages,
            "warnings": self.warnings,
        }

def resolve_cost_policy(config: dict[str, Any]) -> tuple[dict[str, Any], CostPolicy]:
    resolved = deepcopy(config)
    orchestrator = resolved.setdefault("orchestrator", {})
    quality_mode = orchestrator.get("quality_mode", "balanced")
    if quality_mode not in {"low_cost", "balanced", "max_quality"}:
        raise ValueError("orchestrator.quality_mode must be low_cost|balanced|max_quality")
    text_llm_backend = orchestrator.get("text_llm_backend", "chatgpt_playwright")
    if text_llm_backend != "chatgpt_playwright":
        raise ValueError("orchestrator.text_llm_backend must be chatgpt_playwright")
    api_budget_guard = orchestrator.get("api_budget_guard", "warn")
    if api_budget_guard not in {"off", "warn", "block"}:
        raise ValueError("orchestrator.api_budget_guard must be off|warn|block")

    warnings: list[str] = []
    ingest = resolved.setdefault("ingest", {})
    tts = resolved.setdefault("tts", {})
    review = resolved.setdefault("review", {})
    if review.get("llm_backend", "chatgpt_playwright") != "chatgpt_playwright":
        raise ValueError("review.llm_backend must be chatgpt_playwright")

    asr_policy = ingest.get("asr_policy", "preset")
    if asr_policy not in {"preset", "local_first", "openai_hybrid", "manual"}:
        raise ValueError("ingest.asr_policy must be preset|local_first|openai_hybrid|manual")

    if quality_mode == "low_cost":
        if asr_policy == "preset":
            asr_policy = "local_first"
            ingest["asr_policy"] = "local_first"
        ingest["asr_provider"] = "faster-whisper" if asr_policy == "local_first" else ingest.get("asr_provider", "faster-whisper")
        if asr_policy == "local_first" and ingest.get("aligner") in {None, "none"}:
            ingest["aligner"] = "whisperx"
        ingest["max_vision_frames"] = 0
        if tts.get("pronunciation_suggest_backend") is None:
            tts["pronunciation_suggest_backend"] = "off"
        warnings.append("low_cost disables OpenAI vision and uses local-first ASR; KO translation may still require API unless translate_mode=none")
    elif quality_mode == "max_quality":
        if asr_policy in {"preset", "openai_hybrid"}:
            ingest["asr_policy"] = "openai_hybrid"
            ingest["asr_provider"] = "openai-gpt4o-hybrid"
        if tts.get("pronunciation_suggest_backend") is None:
            tts["pronunciation_suggest_backend"] = "chatgpt_playwright"
    else:
        if tts.get("pronunciation_suggest_backend") is None:
            tts["pronunciation_suggest_backend"] = "chatgpt_playwright"

    review["llm_backend"] = text_llm_backend
    review_fallback_configured = bool(review.get("openai_fallback_model"))
    review_fallback_blocked = review_fallback_configured and api_budget_guard == "block"

    stages = {
        "ingest": describe_ingest(ingest),
        "review": {
            "backend": text_llm_backend,
            "cost": "playwright_session" if text_llm_backend == "chatgpt_playwright" else text_llm_backend,
            "openai_fallback_model": review.get("openai_fallback_model"),
            "openai_fallback_configured": review_fallback_configured,
            "openai_fallback_allowed": review_fallback_configured and not review_fallback_blocked,
            "openai_fallback_blocked": review_fallback_blocked,
            "openai_fallback_possible": review_fallback_configured,
        },
        "tts": describe_tts(tts),
        "preflight": {"backend": resolved.get("preflight", {}).get("classifier", "heuristic"), "cost": "local"},
        "visual_index": {"backend": resolved.get("visual_index", {}).get("embedding_mode", "off"), "cost": "local"},
        "match": {"backend": resolved.get("match", {}).get("semantic_mode", "off"), "cost": "local"},
        "render": {"backend": "ffmpeg", "cost": "local"},
    }
    policy = CostPolicy(
        quality_mode=quality_mode,
        text_llm_backend=text_llm_backend,
        api_budget_guard=api_budget_guard,
        stages=stages,
        warnings=warnings,
    )
    return resolved, policy

def describe_ingest(ingest: dict[str, Any]) -> dict[str, Any]:
    asr_provider = ingest.get("asr_provider", "faster-whisper")
    translate_mode = ingest.get("translate_mode", "ko-en")
    vision_frames = int(ingest.get("max_vision_frames", 0) or 0)
    openai_uses: list[str] = []
    if str(asr_provider).startswith("openai"):
        openai_uses.append("asr")
    if translate_mode not in {"none", "off", None}:
        openai_uses.append("translation")
    if vision_frames > 0:
        openai_uses.append("vision")
    return {
        "asr_policy": ingest.get("asr_policy", "preset"),
        "asr_provider": asr_provider,
        "aligner": ingest.get("aligner", "none"),
        "translate_mode": translate_mode,
        "max_vision_frames": vision_frames,
        "backend": "openai_api" if openai_uses else "local",
        "openai_uses": openai_uses,
        "cost": "paid_api" if openai_uses else "local",
    }

def describe_tts(tts: dict[str, Any]) -> dict[str, Any]:
    try:
        available_providers = resolve_provider_order(
            tts.get("provider_mode", "auto"),
            voice_id=str(tts.get("voice_id") or ""),
            genmax_voice_id=tts.get("genmax_voice_id"),
        )
    except TtsProviderError:
        available_providers = []
    return {
        "provider_mode": tts.get("provider_mode", "auto"),
        "available_providers": available_providers,
        "text_normalization": tts.get("text_normalization", "vi"),
        "pronunciation_suggest_backend": tts.get("pronunciation_suggest_backend", "off"),
        "backend": "paid_tts_provider",
        "cost": "paid_audio_cacheable",
    }

def build_cost_summary(
    policy: CostPolicy,
    selected: set[str],
    will_run: set[str],
    *,
    openai_fallback_possible: bool = False,
    openai_fallback_triggered: bool = False,
    review_fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stage_rows = []
    warnings = list(policy.warnings)
    for stage, info in policy.stages.items():
        if stage not in selected:
            continue
        row = {"stage": stage, "selected": True, "will_run": stage in will_run, **info}
        stage_rows.append(row)
        if stage in will_run and info.get("cost") in {"paid_api", "paid_audio_cacheable"}:
            warnings.append(f"{stage} may incur {info['cost']}")
    review_status = {
        "configured": bool(policy.stages.get("review", {}).get("openai_fallback_configured")),
        "allowed": bool(policy.stages.get("review", {}).get("openai_fallback_allowed")),
        "blocked": bool(policy.stages.get("review", {}).get("openai_fallback_blocked")),
        "triggered": False,
        "playwright_attempts": 0,
        "error_code": None,
        "error": None,
        "block_reason": None,
    }
    if review_fallback:
        review_status.update(review_fallback)
    return {
        "quality_mode": policy.quality_mode,
        "text_llm_backend": policy.text_llm_backend,
        "api_budget_guard": policy.api_budget_guard,
        "stages": stage_rows,
        "openai_fallback_possible": openai_fallback_possible,
        "openai_fallback_triggered": openai_fallback_triggered,
        "review_openai_fallback": review_status,
        "warnings": warnings,
    }

def disallowed_openai_stages(policy: CostPolicy, will_run: set[str]) -> list[str]:
    if policy.api_budget_guard != "block":
        return []
    blocked: list[str] = []
    ingest = policy.stages.get("ingest", {})
    if "ingest" in will_run and ingest.get("openai_uses"):
        blocked.append("ingest:" + ",".join(ingest.get("openai_uses", [])))
    return blocked
