# Release Candidate Gate

Project version is `1.0.2`. The gates below are required before creating or pushing the release tag.

## CI Gate

GitHub Actions runs the Windows/Python 3.11 offline gate on every push to `main` and every pull request:

```powershell
./scripts/release_check.ps1 -SkipMediaSmoke
```

The CI gate requires no repository secrets, browser install, paid API, GPU, or real movie file. It checks:

- tracked files and full Git history for credential material;
- `git diff --check`, `ruff check`, Tach boundary check, full pytest, and compileall;
- editable-install metadata and wheel contents;
- wheel install/import from outside the checkout;
- `ingest`, `match`, and `visual_index` CLI help;
- `config.movie.production.yaml` orchestrator dry-run.

CI uploads `report.json`, `tach-report.txt`, secret/package reports, and the built wheel from `work/release-gate/`.
Tach boundary findings fail the release gate.

## Local Media Gate

Install development dependencies and ensure `ffmpeg`/`ffprobe` are available, then run from a clean worktree:

```powershell
python -m pip install -e ".[dev]"
powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1 `
  -MediaPath "C:\path\to\movie.mp4"
```

During development only, `-AllowDirty` may bypass the clean-worktree check. It must not be used for the final release decision.

The media gate creates a 30-second clip under `work/release-gate/cache-smoke`, removes OpenAI/AI33/Genmax keys from subprocess environments, and runs only GĐ0/GĐ1 with manual transcript, no translation API, no vision API, and no aligner/GPU.

Required cache assertions:

- unchanged rerun reuses every GĐ1 cache stage;
- profile content change rebuilds only vision;
- glossary change reuses aligned transcript and rebuilds correction/translation/vision;
- film identity change rebuilds audio, transcript, correction, translation, and vision artifacts.

## Release Decision

The release tag is allowed only when:

- GitHub Release Gate is green for the intended commit;
- local media gate passes without `-AllowDirty`;
- `work/release-gate/report.json` has `status: passed` and media smoke enabled;
- secret scan has zero findings;
- worktree is clean and `main` is synchronized with `origin/main`.

Do not add VLM/OCR/new matching features between a passing release gate and the release tag.

## Release Status

- GitHub Release Gate passed for pre-release commit `3327fa8` on 2026-07-12.
- Local media gate passed on the same commit without `-AllowDirty`, with media smoke enabled and zero secret findings.
- Tag `v1.0.0` remains attached to release commit `42d0e99`.
- Tag `v1.0.1` remains attached to release commit `c37ac94`.
- The `v1.0.2` candidate contains the locked Playwright-first review policy, resumed-chat history stabilization, and transient AI33/Genmax polling recovery.
