"""Subprocess fixture used to prove crash and multi-worker Run semantics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents import AlarmEvent
from services.run_store import RunStore
from services.tool_executor import ToolExecutionStore


def make_event(case_id: str) -> AlarmEvent:
    return AlarmEvent(
        timestamp="subprocess-test",
        event_id=f"EVT_{case_id}",
        run_id=f"RUN_{case_id}",
        trace_id=f"TRACE_{case_id}",
        events=[{
            "type": "vehicle_detection",
            "level": "C",
            "bbox": {"x": 1, "y": 1, "w": 10, "h": 10},
            "detail": "subprocess crash fixture",
        }],
        dispatch_decision={
            "final_level": "C",
            "plan_validation": {"final_plan": ["database.store"]},
        },
    )


def signal_ready(path: Path) -> None:
    path.write_text("ready", encoding="utf-8")


def crash_worker(args) -> int:
    store = RunStore(args.database)
    event = make_event(args.case_id)
    store.create(event, "subprocess")
    if args.point == "after_create":
        signal_ready(args.ready)
        time.sleep(60)
        return 0

    claimed = store.claim_run(event.run_id, "crashed-worker", args.lease_seconds)
    if claimed is None:
        return 3
    event.owner_id = str(claimed["owner_id"])
    event.execution_attempt = int(claimed["execution_attempt"])
    store.save_snapshot(
        event, owner_id=event.owner_id,
        execution_attempt=event.execution_attempt,
    )
    store.transition(
        event.run_id, "decided", "policy", event=event,
        owner_id=event.owner_id, execution_attempt=event.execution_attempt,
    )
    store.transition(
        event.run_id, "executing", "tools", event=event,
        owner_id=event.owner_id, execution_attempt=event.execution_attempt,
    )

    executions = ToolExecutionStore(args.database)
    executions.begin(
        execution_id=f"TOOL_{args.case_id}", run_id=event.run_id,
        event_id=event.event_id, step_id=f"STEP_{args.case_id}",
        idempotency_key=f"KEY_{args.case_id}", tool="database", action="store",
        owner_id=event.owner_id, execution_attempt=event.execution_attempt,
    )
    args.marker.write_text("side-effect\n", encoding="utf-8")
    if args.point == "after_tool_result":
        executions.record_attempt(
            f"KEY_{args.case_id}", 1, run_id=event.run_id,
            owner_id=event.owner_id, execution_attempt=event.execution_attempt,
        )
        executions.finish(
            f"KEY_{args.case_id}", "succeeded", result={"stored": True},
            run_id=event.run_id, owner_id=event.owner_id,
            execution_attempt=event.execution_attempt,
        )
    signal_ready(args.ready)
    time.sleep(60)
    return 0


def claim_worker(args) -> int:
    deadline = time.time() + 10
    while not args.gate.exists() and time.time() < deadline:
        time.sleep(0.01)
    claimed = RunStore(args.database).claim_run(
        args.run_id, args.owner_id, args.lease_seconds
    )
    args.output.write_text(
        json.dumps({
            "claimed": claimed is not None,
            "owner_id": claimed.get("owner_id") if claimed else None,
            "execution_attempt": claimed.get("execution_attempt") if claimed else None,
        }),
        encoding="utf-8",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices={"crash", "claim"})
    parser.add_argument("--database", required=True)
    parser.add_argument("--lease-seconds", type=float, default=0.25)
    parser.add_argument("--case-id", default="CASE")
    parser.add_argument(
        "--point", choices={"after_create", "after_side_effect", "after_tool_result"}
    )
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--owner-id")
    args = parser.parse_args()
    return crash_worker(args) if args.mode == "crash" else claim_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
