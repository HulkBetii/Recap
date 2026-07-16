from __future__ import annotations

from review.repetition import detect_repetition_findings, repetition_issues


def test_repetition_detector_flags_near_duplicate_narration() -> None:
    texts = [
        (0, "Minh phát hiện túi tiền trong cốp xe và lập tức gọi đồng đội tới."),
        (1, "Lan chuyển hướng sang nghi phạm khác ở bãi xe."),
        (2, "Minh phát hiện túi tiền trong cốp xe rồi lập tức gọi đồng đội tới."),
    ]

    findings = detect_repetition_findings(texts)

    assert len(findings) == 1
    assert findings[0].beat_id == 2
    assert findings[0].matched_beat_id == 0
    assert findings[0].reason == "near-duplicate narration"


def test_repetition_issues_wrap_detector_output() -> None:
    issues = repetition_issues(
        [
            (0, "Một bí mật được hé lộ."),
            (1, "Một bí mật được hé lộ."),
        ]
    )

    assert len(issues) == 1
    assert issues[0].beat_id == 1
    assert issues[0].type == "repetition"
