from __future__ import annotations

import json
from pathlib import Path

from common.integrity import file_hash
from common.schema import RemixEdl, validate_remix_edl


def output_is_current(output_path: Path, meta_path: Path, identity: dict[str, object]) -> bool:
    if not output_path.is_file() or not meta_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        edl = RemixEdl.model_validate_json(output_path.read_text(encoding="utf-8"))
        validate_remix_edl(edl)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        meta.get("input_hashes") == identity.get("input_hashes")
        and meta.get("config_hash") == identity.get("config_hash")
        and meta.get("output_hashes", {}).get(output_path.name) == file_hash(output_path)
    )
