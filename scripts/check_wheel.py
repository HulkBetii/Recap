from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.release_helpers import inspect_wheel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Recap wheel contents")
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = inspect_wheel(args.wheel.expanduser().resolve())
    if args.report:
        report = args.report.expanduser().resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
