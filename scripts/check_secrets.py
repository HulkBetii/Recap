from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

SECRET_PATTERNS = (
    ("openai_key", re.compile(r"\bsk-(?:proj|svcacct)-[A-Za-z0-9_-]{20,}\b")),
    ("provider_key", re.compile(r"\bsk_[A-Za-z0-9]{24,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    kind: str
    redacted: str


def redact(value: str) -> str:
    if len(value) <= 12:
        return "<redacted>"
    return f"{value[:8]}...{value[-4:]}"


def scan_text(source: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(line):
                findings.append(Finding(source, line_number, kind, redact(match.group(0))))
    return findings


def git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def tracked_files(root: Path) -> list[Path]:
    output = git_output(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [root / item for item in output.split("\0") if item]


def scan_tracked_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in tracked_files(root):
        relative = path.relative_to(root).as_posix()
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            findings.append(Finding(relative, 1, "tracked_env", "<tracked environment file>"))
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        if b"\0" in payload:
            continue
        findings.extend(scan_text(relative, payload.decode("utf-8", errors="replace")))
    return findings


def scan_history(root: Path) -> list[Finding]:
    history = git_output(root, "log", "--all", "-p", "--no-ext-diff", "--unified=0")
    return scan_text("git-history", history)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan tracked files and Git history for credential material")
    parser.add_argument("--root", default=Path.cwd(), type=Path)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.expanduser().resolve()
    findings = scan_tracked_tree(root)
    if args.history:
        findings.extend(scan_history(root))
    payload = {
        "tracked_files_scanned": len(tracked_files(root)),
        "history_scanned": bool(args.history),
        "findings": [asdict(item) for item in findings],
    }
    if args.report:
        report = args.report.expanduser().resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if findings:
        for finding in findings:
            print(f"{finding.source}:{finding.line}: {finding.kind}: {finding.redacted}")
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
