"""One-command quality gate for the safety Agent repository."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "benchmarks" / "reports" / "verification_summary.json"
REQUIRED_ASSETS = (
    ROOT / "knowledge" / "sop" / "safety_procedures.json",
    ROOT / "benchmarks" / "datasets" / "agent_policy_cases.jsonl",
    ROOT / "benchmarks" / "datasets" / "sop_retrieval_cases.jsonl",
)


def run_step(name: str, arguments: list[str], environment: dict[str, str]) -> dict:
    started = time.perf_counter()
    print(f"\n[VERIFY] {name}", flush=True)
    completed = subprocess.run(
        [sys.executable, "-B", *arguments], cwd=ROOT, env=environment, check=False,
    )
    return {
        "name": name,
        # Keep committed reports portable; subprocess execution still uses the
        # current interpreter selected above.
        "command": " ".join(["python", "-B", *arguments]),
        "exit_code": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "passed": completed.returncode == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reproducible Agent quality gates")
    parser.add_argument("--live", action="store_true", help="also call the configured local Qwen model")
    parser.add_argument("--live-timeout", type=int, default=90)
    args = parser.parse_args()

    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_ASSETS if not path.is_file()]
    if missing:
        print(f"[VERIFY] missing repository assets: {', '.join(missing)}")
        return 2

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    steps = [
        ("agent_policy", ["-m", "benchmarks.run_agent_benchmark"]),
        ("runtime_faults", ["-m", "benchmarks.run_runtime_faults"]),
        ("sop_retrieval", ["-m", "benchmarks.run_sop_benchmark"]),
        ("unit_and_integration", ["-m", "unittest", "discover", "-s", "tests", "-v"]),
    ]
    if args.live:
        steps.append((
            "multimodal_qwen",
            ["-m", "benchmarks.run_multimodal_benchmark", "--timeout", str(args.live_timeout), "--require-model"],
        ))

    results = []
    for name, command in steps:
        result = run_step(name, command, environment)
        results.append(result)
        if not result["passed"]:
            break

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": sys.platform,
        "live_model_included": args.live,
        "passed": len(results) == len(steps) and all(item["passed"] for item in results),
        "steps": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[VERIFY] {'PASS' if summary['passed'] else 'FAIL'}: {REPORT}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
