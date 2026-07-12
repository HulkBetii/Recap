from __future__ import annotations

from pathlib import Path

from common.integrity import media_identity_hash, stable_hash

PREFLIGHT_CACHE_VERSION = "preflight-v1"
PREFLIGHT_PREPROCESSING_VERSION = "intro-sampling-v1"


def preflight_config_hash(
    *,
    classifier: str,
    max_intro_s: float,
    sample_every_s: float,
    confidence_threshold: float,
    uncertain_threshold: float,
) -> str:
    return stable_hash(
        {
            "classifier": classifier,
            "max_intro_s": max_intro_s,
            "sample_every_s": sample_every_s,
            "confidence_threshold": confidence_threshold,
            "uncertain_threshold": uncertain_threshold,
            "preprocessing_version": PREFLIGHT_PREPROCESSING_VERSION,
        }
    )


def preflight_identity(
    film: Path,
    *,
    classifier: str,
    max_intro_s: float,
    sample_every_s: float,
    confidence_threshold: float,
    uncertain_threshold: float,
) -> tuple[str, str]:
    return (
        media_identity_hash(film),
        preflight_config_hash(
            classifier=classifier,
            max_intro_s=max_intro_s,
            sample_every_s=sample_every_s,
            confidence_threshold=confidence_threshold,
            uncertain_threshold=uncertain_threshold,
        ),
    )
