from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol

DEFAULT_VISUAL_MODEL = "google/siglip2-base-patch16-384"

class VisualEncoderError(RuntimeError):
    pass

class VisualEncoder(Protocol):
    device: str

    def encode_images(self, image_paths: list[Path], *, batch_size: int) -> list[list[float]]:
        ...

    def encode_texts(self, texts: list[str], *, batch_size: int) -> list[list[float]]:
        ...

def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return [0.0 for _ in vector]
    return [value / norm for value in vector]

def dense_cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right))))

def resolve_device(policy: str) -> str:
    if policy not in {"auto", "cpu", "cuda"}:
        raise VisualEncoderError("--device must be auto, cpu, or cuda")
    if policy == "cpu":
        return "cpu"
    try:
        import torch
    except ImportError as exc:
        if policy == "cuda":
            raise VisualEncoderError('device=cuda requires torch. Install with: pip install -e ".[visual-index]"') from exc
        return "cpu"
    cuda_available = bool(torch.cuda.is_available())
    if policy == "cuda" and not cuda_available:
        raise VisualEncoderError("device=cuda requested but CUDA is not available")
    return "cuda" if cuda_available else "cpu"

class TransformerVisualEncoder:
    def __init__(self, model_name: str, *, device: str = "auto", trust_remote_code: bool = False) -> None:
        try:
            import torch
            from transformers import AutoModel, AutoProcessor
        except ImportError as exc:
            raise VisualEncoderError('visual indexing requires optional deps. Install with: pip install -e ".[visual-index]"') from exc
        self.torch = torch
        self.device = resolve_device(device)
        self.processor = AutoProcessor.from_pretrained(model_name, trust_remote_code=trust_remote_code)
        self.model = AutoModel.from_pretrained(model_name, trust_remote_code=trust_remote_code)
        self.model.to(self.device)
        self.model.eval()

    def encode_images(self, image_paths: list[Path], *, batch_size: int) -> list[list[float]]:
        if not image_paths:
            return []
        try:
            from PIL import Image
        except ImportError as exc:
            raise VisualEncoderError('visual indexing requires Pillow. Install with: pip install -e ".[visual-index]"') from exc
        vectors: list[list[float]] = []
        for start in range(0, len(image_paths), batch_size):
            batch_paths = image_paths[start:start + batch_size]
            images = [Image.open(path).convert("RGB") for path in batch_paths]
            try:
                inputs = self.processor(images=images, return_tensors="pt")
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                with self.torch.no_grad():
                    if hasattr(self.model, "get_image_features"):
                        features = self.model.get_image_features(**inputs)
                    else:
                        output = self.model(**inputs)
                        features = getattr(output, "image_embeds", None)
                        if features is None:
                            features = getattr(output, "pooler_output", None)
                    if features is None:
                        raise VisualEncoderError("model did not expose image features")
                    vectors.extend(_tensor_rows(features))
            finally:
                for image in images:
                    image.close()
        return [normalize_vector(vector) for vector in vectors]

    def encode_texts(self, texts: list[str], *, batch_size: int) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            inputs = self.processor(text=batch, padding=True, truncation=True, return_tensors="pt")
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            with self.torch.no_grad():
                if hasattr(self.model, "get_text_features"):
                    features = self.model.get_text_features(**inputs)
                else:
                    output = self.model(**inputs)
                    features = getattr(output, "text_embeds", None)
                    if features is None:
                        features = getattr(output, "pooler_output", None)
                if features is None:
                    raise VisualEncoderError("model did not expose text features")
                vectors.extend(_tensor_rows(features))
        return [normalize_vector(vector) for vector in vectors]

def _tensor_rows(tensor) -> list[list[float]]:  # type: ignore[no-untyped-def]
    rows = tensor.detach().cpu().float().tolist()
    if rows and isinstance(rows[0], float):
        return [[float(value) for value in rows]]
    return [[float(value) for value in row] for row in rows]
