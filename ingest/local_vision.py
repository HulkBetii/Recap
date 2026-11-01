from __future__ import annotations

from pathlib import Path


class LocalVisionError(RuntimeError):
    pass


class LocalQwenVisionClient:
    def __init__(self, model_name: str, *, device: str = "auto", resize_long_edge: int = 768, batch_size: int = 1) -> None:
        try:
            import torch
            from PIL import Image, ImageOps
            from transformers import AutoProcessor
        except ImportError as exc:  # pragma: no cover - optional dependency path
            raise LocalVisionError(
                "local Qwen vision requires optional dependencies: torch, transformers, Pillow"
            ) from exc

        self._torch = torch
        self._Image = Image
        self._ImageOps = ImageOps
        self._processor_cls = AutoProcessor
        self.model_name = model_name
        self.resize_long_edge = max(64, int(resize_long_edge))
        self.batch_size = max(1, int(batch_size))
        self.device = self._resolve_device(device)
        self.processor = self._processor_cls.from_pretrained(model_name, trust_remote_code=True)
        self.model = self._load_model(model_name)
        self.model.eval()

    def _resolve_device(self, device: str) -> str:
        if device == "cuda" and self._torch.cuda.is_available():
            return "cuda"
        if device == "auto":
            return "cuda" if self._torch.cuda.is_available() else "cpu"
        return "cpu"

    def _load_model(self, model_name: str):
        try:
            from transformers import AutoModelForImageTextToText
        except ImportError:
            AutoModelForImageTextToText = None  # type: ignore[assignment]

        model_cls = AutoModelForImageTextToText
        if model_cls is None:
            raise LocalVisionError("transformers is missing AutoModelForImageTextToText for local vision")

        dtype = self._torch.float16 if self.device == "cuda" else self._torch.float32
        model = model_cls.from_pretrained(
            model_name,
            trust_remote_code=True,
            torch_dtype=dtype,
        )
        return model.to(self.device)

    def _prepare_image(self, frame_path: Path):
        image = self._Image.open(frame_path).convert("RGB")
        image = self._ImageOps.exif_transpose(image)
        if max(image.size) > self.resize_long_edge:
            image.thumbnail((self.resize_long_edge, self.resize_long_edge), self._Image.Resampling.LANCZOS)
        return image

    def describe_frame(self, frame_path: Path) -> str:
        image = self._prepare_image(frame_path)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {
                        "type": "text",
                        "text": (
                            "Describe this anime frame in one or two short English sentences. "
                            "State visible characters, action, setting, and mood. "
                            "Do not infer plot beyond the image."
                        ),
                    },
                ],
            }
        ]
        prompt = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.processor(text=[prompt], images=[image], return_tensors="pt")
        inputs = inputs.to(self.device)
        with self._torch.inference_mode():
            generated = self.model.generate(
                **inputs,
                max_new_tokens=96,
                do_sample=False,
            )
        prompt_len = inputs["input_ids"].shape[-1] if "input_ids" in inputs else 0
        decoded = self.processor.batch_decode(generated[:, prompt_len:], skip_special_tokens=True)[0].strip()
        if not decoded:
            raise LocalVisionError("local vision response was empty")
        return decoded
