from __future__ import annotations

import subprocess
from pathlib import Path


class RemixRenderError(RuntimeError):
    pass


def run_media_command(args: list[str], commands: list[list[str]]) -> None:
    commands.append(list(args))
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "media command failed"
        raise RemixRenderError(message.splitlines()[-1])


def concat_manifest(paths: list[Path], manifest_path: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"file '{path.resolve().as_posix()}'" for path in paths)
    manifest_path.write_text(body + "\n", encoding="utf-8")


def ffmpeg_filter_names(commands: list[list[str]]) -> set[str]:
    names: set[str] = set()
    for command in commands:
        for index, arg in enumerate(command):
            if arg not in {"-vf", "-filter:v", "-filter_complex"} or index + 1 >= len(command):
                continue
            expression = command[index + 1].lower()
            for name in (
                "subtitles",
                "ass",
                "drawtext",
                "overlay",
                "delogo",
                "boxblur",
                "gblur",
                "maskedmerge",
                "alphamerge",
            ):
                if name in expression:
                    names.add(name)
    return names
