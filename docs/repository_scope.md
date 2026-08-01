# Repository scope

This repository should contain the reproducible Agent system and its evidence, not machine-specific runtime data.

## Track in Git

- `agents/`, `services/`, `tools/`: Agent, runtime, policy and tool source.
- `benchmarks/datasets/`: deterministic evaluation inputs.
- `benchmarks/reports/`: reviewed baseline reports used by the README.
- `benchmarks/run_*.py`, `verify.py`, `verify.bat`: reproducible quality gates.
- `knowledge/sop/`: versioned project evaluation procedures.
- `tests/`: unit and integration tests.
- `frontend/`: local digital-twin source and vendored runtime assets whose licenses permit redistribution.
- `.github/workflows/`, `requirements*.txt`, `.env.example`, `setup.bat`, `start.bat`, `stop.bat`.
- `models/README.md`, but never private or licensed model weights.

## Keep local

- `.env`, credentials, RTSP URLs and notification tokens.
- `models/*.pt`, `*.onnx`, `*.engine`.
- `alarms/`, `data/`, `runtime/`, `logs/` and generated approval/execution records.
- Resume sources such as root `main.tex`.
- Machine-specific executables, absolute paths and tunnel configuration.

## Before publishing

1. Run `python -B verify.py` and inspect `benchmarks/reports/verification_summary.json`.
2. Confirm `git diff --check` is clean.
3. Review third-party frontend assets and model/data licenses.
4. Stage only the paths listed under **Track in Git**; do not use a blind `git add -A` in a mixed worktree.
