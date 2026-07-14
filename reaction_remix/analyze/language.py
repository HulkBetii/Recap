from __future__ import annotations


class LinguaLanguageVerifier:
    def __init__(self) -> None:
        try:
            from lingua import LanguageDetectorBuilder
        except ImportError as exc:
            raise RuntimeError("lingua-language-detector is required for reaction analysis") from exc
        self._detector = LanguageDetectorBuilder.from_all_languages().build()

    def detect(self, text: str) -> tuple[str, float]:
        values = self._detector.compute_language_confidence_values(text)
        if not values:
            return "und", 0.0
        best = values[0]
        iso = getattr(best.language, "iso_code_639_1", None)
        code = str(getattr(iso, "name", "und")).lower()
        return code, float(best.value)

