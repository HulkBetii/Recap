from __future__ import annotations

import importlib.util
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable

from common.media import run_command

DEFAULT_VIENEU_MODEL = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
DEFAULT_VIENEU_STYLE = "doc_truyen"
DEFAULT_VIENEU_BACKEND = "onnx"
DEFAULT_VIENEU_PRECISION = "int8"
DEFAULT_VIENEU_VOICE = "Ngọc Linh"
VIENEU_STYLES = {"tu_nhien", "tin_tuc", "doc_truyen"}
VIENEU_BACKENDS = {"onnx"}
VIENEU_PRECISIONS = {"int8", "fp32"}


class VieneuProviderError(RuntimeError):
    pass


def vieneu_package_version() -> str | None:
    try:
        return version("vieneu")
    except PackageNotFoundError:
        return None


def missing_vieneu_modules() -> list[str]:
    return [name for name in ("vieneu", "onnxruntime", "soundfile") if importlib.util.find_spec(name) is None]


def validate_vieneu_settings(*, backend: str, precision: str, style: str, threads: int) -> None:
    if backend not in VIENEU_BACKENDS:
        raise VieneuProviderError("VieNeu V1 supports only backend=onnx")
    if precision not in VIENEU_PRECISIONS:
        raise VieneuProviderError("VieNeu precision must be int8 or fp32")
    if style not in VIENEU_STYLES:
        raise VieneuProviderError("VieNeu style must be tu_nhien, tin_tuc, or doc_truyen")
    if threads < 0:
        raise VieneuProviderError("VieNeu threads must be >= 0")


def require_vieneu_runtime() -> None:
    missing = missing_vieneu_modules()
    if missing:
        raise VieneuProviderError(
            "VieNeu runtime is missing: "
            + ", ".join(missing)
            + '; install with python -m pip install -e ".[tts-vieneu]"'
        )


class VieneuSynthesizer:
    def __init__(self, engine_factory: Callable[..., Any] | None = None) -> None:
        self._engine_factory = engine_factory
        self._engine: Any | None = None
        self._engine_identity: tuple[str, str, str, int] | None = None
        self._lock = threading.Lock()

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        style: str,
        backend: str,
        precision: str,
        model: str,
        threads: int,
        speed: float,
        output_path: Path,
    ) -> None:
        validate_vieneu_settings(backend=backend, precision=precision, style=style, threads=threads)
        if not voice_id.strip():
            raise VieneuProviderError("VieNeu voice_id cannot be empty")
        if speed <= 0:
            raise VieneuProviderError("VieNeu speed must be > 0")

        # The ONNX engine and its Hugging Face cache are shared across beats in this TTS process.
        with self._lock:
            engine = self._get_engine(backend=backend, precision=precision, model=model, threads=threads)
            self._synthesize_locked(
                engine=engine,
                text=text,
                voice_id=voice_id,
                style=style,
                speed=speed,
                output_path=output_path,
            )

    def _get_engine(self, *, backend: str, precision: str, model: str, threads: int) -> Any:
        identity = (backend, precision, model, threads)
        if self._engine is not None:
            if self._engine_identity != identity:
                raise VieneuProviderError("VieNeu engine settings changed after initialization")
            return self._engine

        try:
            if self._engine_factory is None:
                require_vieneu_runtime()
                from vieneu import Vieneu

                factory = Vieneu
            else:
                factory = self._engine_factory
            self._engine = factory(
                mode="v3turbo",
                backbone_repo=model,
                backend=backend,
                device="cpu",
                precision=precision,
                threads=threads,
            )
        except Exception as exc:  # noqa: BLE001 - optional SDK exposes HF/ONNX exception families
            raise VieneuProviderError(f"Could not initialize VieNeu ONNX runtime: {exc}") from exc
        self._engine_identity = identity
        return self._engine

    @staticmethod
    def _synthesize_locked(
        *,
        engine: Any,
        text: str,
        voice_id: str,
        style: str,
        speed: float,
        output_path: Path,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_wav = output_path.with_name(f"{output_path.stem}.vieneu.wav")
        temp_mp3 = output_path.with_name(f"{output_path.stem}.vieneu.part.mp3")
        try:
            available = {voice for _description, voice in engine.list_preset_voices()}
            if voice_id not in available:
                raise VieneuProviderError(f"Unknown VieNeu preset voice: {voice_id}")
            audio = engine.infer(
                text,
                voice=voice_id,
                style=style,
                apply_watermark=True,
            )
            engine.save(audio, temp_wav)
            if not temp_wav.is_file() or temp_wav.stat().st_size == 0:
                raise VieneuProviderError("VieNeu produced an empty WAV file")

            command = ["ffmpeg", "-y", "-i", str(temp_wav)]
            if abs(speed - 1.0) > 1e-6:
                command.extend(["-filter:a", f"atempo={speed:.6f}"])
            command.extend(["-codec:a", "libmp3lame", "-q:a", "2", str(temp_mp3)])
            run_command(command)
            temp_mp3.replace(output_path)
        except VieneuProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify SDK/model/media failures for the provider boundary
            raise VieneuProviderError(f"VieNeu synthesis failed: {exc}") from exc
        finally:
            for path in (temp_wav, temp_mp3):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
