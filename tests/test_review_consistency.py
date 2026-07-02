from __future__ import annotations

from review.consistency import apply_narration_consistency, extract_canonical_terms
from review.models import NarrationBeat


def test_extract_canonical_terms_reads_names_roles_and_aliases() -> None:
    glossary = [{"name": "Hwang Jun-hyun", "role": "người của Choi Seong", "aliases": ["Junhyun"]}]

    terms = extract_canonical_terms(glossary)
    canonical = {term.canonical: set(term.aliases) for term in terms}

    assert "Hwang Jun-hyun" in canonical
    assert "Hwang Junhyun" in canonical["Hwang Jun-hyun"]
    assert "Choi Seong" in canonical
    assert "Choi Seon" in canonical["Choi Seong"]


def test_apply_narration_consistency_normalizes_known_aliases() -> None:
    glossary = [{"name": "Hwang Jun-hyun", "role": "cầu thủ của Choi Seong"}]
    narration = [
        NarrationBeat(beat_id=0, narration="Choi Seon gọi Hwang Junhyun bước vào sân."),
        NarrationBeat(beat_id=1, narration="Choi Sung tiếp tục gây áp lực."),
    ]

    corrected, warnings = apply_narration_consistency(narration, glossary)

    assert corrected[0].narration == "Choi Seong gọi Hwang Jun-hyun bước vào sân."
    assert corrected[1].narration == "Choi Seong tiếp tục gây áp lực."
    assert warnings == ["narration consistency normalized glossary terms in beat(s): 0, 1"]
