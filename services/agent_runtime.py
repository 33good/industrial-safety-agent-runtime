"""Application service that owns the safety Agent lifecycle."""
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid

from agents.dispatch import DispatchAgent
from agents.evidence_replan import (
    AdjacentFrameEvidenceTool,
    append_decision_round,
    new_replan_trace,
    terminal_review_reason,
)
from agents.failure_attribution import append_unique_attributions, new_repair_trace
from agents.memory import MemoryModule
from agents.perception import PerceptionAgent
from agents.safety_agent import SafetyAgent
from agents.sop_retriever import SOPRetriever
from tools.actuator import ActuatorTool
from tools.database import DatabaseTool
from tools.human_loop import HumanLoopTool
from tools.notifier import NotifierTool
from tools.reporter import ReporterTool

from .analysis_limiter import AnalysisLimiter
from .evidence import annotate_image, save_evidence
from .local_vision import ViolationGate
from .recent_events import RecentEventStore
from .run_lease import RunLeaseHeartbeat
from .run_store import RunStore, StaleRunOwnerError, restore_event
from .runtime_metrics import RuntimeMetricsService
from .tool_executor import ToolExecutor, ToolSpec
from .trace_validator import RunTraceService


def event_payload(event: dict) -> dict:
    payload = {
        "type": event["type"],
        "level": event["level"],
        "bbox": event["bbox"],
        "detail": event["detail"],
        "targetId": event.get("targetId", 0),
    }
    if event.get("confidence") is not None:
        payload["confidence"] = event["confidence"]
    if event.get("person_bbox"):
        payload["person_bbox"] = event["person_bbox"]
    if event.get("vehicle_bbox"):
        payload["vehicle_bbox"] = event["vehicle_bbox"]
        payload["vehicle_targetId"] = event.get("vehicle_targetId", 0)
    if event.get("base_level"):
        payload["base_level"] = event["base_level"]
    if event.get("memory_escalation"):
        payload["memory_escalation"] = dict(event["memory_escalation"])
    return payload


def timeline(stage: str, label: str, detail: str = "") -> dict:
    return {
        "stage": stage,
        "label": label,
        "detail": detail,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


class AgentRuntime:
    """Coordinates perception, cognition, policy, tools, and frontend events."""

    COOLDOWNS = {
        "未戴安全帽": 5,
        "未穿反光背心": 5,
        "火焰检测": 3,
        "车辆检测": 10,
    }

    def __init__(self, settings, broadcaster):
        self.settings = settings
        self.broadcaster = broadcaster
        self.settings.alarm_dir.mkdir(parents=True, exist_ok=True)

        self.memory = MemoryModule(str(settings.database_path))
        self.database = DatabaseTool(str(settings.database_path))
        self.human_loop = HumanLoopTool(str(settings.pending_dir))
        self.notifier = NotifierTool(
            webhook_url=settings.notify_webhook,
            platform=settings.notify_platform,
            image_required=settings.notify_image_required,
            image_check_attempts=settings.notify_image_check_attempts,
            image_check_timeout=settings.notify_image_check_timeout_seconds,
        )
        self.reporter = ReporterTool(str(settings.report_dir))
        self.actuator = ActuatorTool(str(settings.execution_dir))

        self.perception = PerceptionAgent()
        sop_catalog_path = getattr(
            settings, "sop_catalog_path",
            Path(__file__).resolve().parents[1] / "knowledge" / "sop" / "safety_procedures.json",
        )
        self.sop_retriever = SOPRetriever(
            sop_catalog_path,
            top_k=getattr(settings, "sop_top_k", 3),
            min_score=getattr(settings, "sop_min_score", 3.0),
        )
        self.safety = SafetyAgent(
            mode=settings.llm_mode,
            model=settings.ollama_model,
            memory=self.memory,
            sop_retriever=self.sop_retriever,
            base_url=settings.ollama_url,
            timeout_seconds=settings.llm_timeout_seconds,
            context_token_budget=getattr(settings, "context_token_budget", 1200),
        )
        self.evidence_tool = AdjacentFrameEvidenceTool()
        self.analysis_limiter = AnalysisLimiter(settings.llm_max_inflight)
        self.run_store = RunStore(str(settings.database_path))
        self.tool_executor = ToolExecutor(str(settings.database_path))
        self.trace_service = RunTraceService(self.run_store, self.tool_executor.store)
        self.metrics_service = RuntimeMetricsService(
            self.run_store, self.tool_executor.store, self.analysis_limiter
        )
        self._ensure_runtime_identity()
        self.dispatch = DispatchAgent(tool_executor=self.tool_executor)
        self.dispatch.register_tool(
            "human_loop", lambda event, action: self.human_loop.handle(event, action),
            ToolSpec(name="human_loop", max_attempts=1),
        )
        self.dispatch.register_tool(
            "database", lambda event, action: self.database.handle(event, action),
            ToolSpec(name="database", max_attempts=2, backoff_seconds=0.05),
        )
        self.dispatch.register_tool(
            "notifier", lambda event, action: self.notifier.handle(event, action),
            ToolSpec(name="notifier", max_attempts=1),
        )
        self.dispatch.register_tool(
            "reporter", lambda event, action: self.reporter.handle(event, action),
            ToolSpec(name="reporter", max_attempts=1),
        )

        self._recent = RecentEventStore(limit=50)
        self._event_gate = ViolationGate(
            min_hits=settings.vision_min_hits,
            cooldown_seconds=settings.vision_event_cooldown_seconds,
        )
        self._last_report = {}
        self._report_lock = threading.Lock()
        # Fixed stripes avoid an unbounded per-event lock cache. SQLite remains the
        # source of truth for uniqueness across processes; these locks only keep
        # duplicate requests in this process from doing redundant preprocessing.
        self._ingest_locks = [threading.Lock() for _ in range(64)]
        self._lifecycle_lock = threading.Lock()
        self._pipeline_threads = {}
        self._accepting_runs = True

    def ingest_detection(self, body: dict, image_bytes: bytes, source: str = "external",
                         source_event_id: str = "") -> dict:
        """Accept a normalized detector payload and dispatch only stable incidents."""
        body = dict(body or {})
        source_event_id = str(
            source_event_id
            or body.get("source_event_id")
            or body.get("sourceEventId")
            or ""
        ).strip()
        camera_id = str(
            body.get("camera_id") or body.get("cameraId") or self.settings.camera_id
        ).strip() or self.settings.camera_id
        ingest_key, payload_hash = self._ingress_identity(
            source, camera_id, source_event_id, body, image_bytes
        )
        lock = self._ingest_locks[int(ingest_key[:8], 16) % len(self._ingest_locks)] if ingest_key else None
        if lock is not None:
            with lock:
                return self._ingest_detection_once(
                    body, image_bytes, source, source_event_id, camera_id,
                    ingest_key, payload_hash,
                )
        return self._ingest_detection_once(
            body, image_bytes, source, source_event_id, camera_id, ingest_key, payload_hash,
        )

    def _ingest_detection_once(self, body: dict, image_bytes: bytes, source: str,
                               source_event_id: str, camera_id: str,
                               ingest_key: str, payload_hash: str) -> dict:
        if ingest_key:
            existing = self.run_store.get_by_ingest_key(ingest_key, payload_hash)
            if existing:
                return self._reused_ingress_response(existing)

        body["source"] = source
        event = self.perception.process(body, image_bytes, verbose=source != "local_yolo")
        event.event_id = self._new_event_id()
        event.run_id = "RUN_" + uuid.uuid4().hex
        event.trace_id = "TRACE_" + uuid.uuid4().hex
        event.source_event_id = source_event_id or f"generated:{event.event_id}"
        event.ingest_key = ingest_key
        event.ingest_payload_hash = payload_hash
        event.camera_id = camera_id
        event.evidence_id = "EVID_" + hashlib.sha256(
            f"{event.event_id}\n{payload_hash}".encode("utf-8")
        ).hexdigest()[:24]
        event.lifecycle_status = "analyzing"

        detected_incidents = list(event.events)
        incidents = detected_incidents
        if source == "local_yolo":
            incidents = self._event_gate.filter(incidents)
        after_temporal_gate = len(incidents)
        incidents = [item for item in incidents if self._should_report(item, camera_id)]
        if not incidents:
            if not ingest_key:
                return {
                    "status": "filtered", "source": source, "events": 0,
                    "source_event_id": source_event_id, "reused": False,
                }
            if not detected_incidents:
                filter_reason = "no_policy_incident"
            elif source == "local_yolo" and not after_temporal_gate:
                filter_reason = "temporal_gate"
            else:
                filter_reason = "cooldown"
            event.events = []
            event.lifecycle_status = "filtered"
            event.timeline = [timeline(
                "filtered", "入口过滤",
                f"事件未进入 Agent 管线: {filter_reason}",
            )]
            stored_run, created = self.run_store.create_or_get(
                event, source, initial_status="filtered", initial_stage="ingress",
                transition_detail=filter_reason,
            )
            if not created:
                return self._reused_ingress_response(stored_run)
            return {
                "status": "filtered", "source": source, "events": 0,
                "event_id": event.event_id, "run_id": event.run_id,
                "trace_id": event.trace_id, "run_status": "filtered",
                "source_event_id": source_event_id, "camera_id": camera_id,
                "reused": False,
            }

        event.events = incidents
        detected_label = "感知接收"
        detected_detail = f"{self._source_label(source)}上报 {len(incidents)} 项安全事件"
        event.timeline = [
            timeline("detected", detected_label, detected_detail),
            timeline("analyzing", "Agent分析", "安全Agent正在进行视觉语义分析"),
        ]
        stored_run, created = self.run_store.create_or_get(event, source)
        if not created:
            return self._reused_ingress_response(stored_run)

        claimed = self._claim_run(event.run_id)
        if claimed is None:
            current = self.run_store.get(event.run_id) or stored_run
            return self._reused_ingress_response(current)
        self._apply_claim(event, claimed)

        # Evidence and other observable side effects happen only after this request
        # wins the atomic ingest-key insert and the Run execution lease.
        self._attach_evidence(event, prefix="alarm")
        self._log_incident(event, source)
        self.run_store.save_snapshot(
            event, owner_id=event.owner_id,
            execution_attempt=event.execution_attempt,
        )

        early_message = {
            "type": "alarm",
            "event_id": event.event_id,
            "run_id": event.run_id,
            "trace_id": event.trace_id,
            "timestamp": event.timestamp,
            "source": source,
            "camera_id": camera_id,
            "source_event_id": source_event_id,
            "reused": False,
            "lifecycle_status": event.lifecycle_status,
            "timeline": event.timeline,
            "image_url": event.image_url,
            "events": [event_payload(item) for item in incidents],
        }
        self._remember_and_broadcast(early_message)
        if not self._start_pipeline(event, self._run_agent_pipeline, "analysis"):
            event.lifecycle_status = "retryable_failed"
            self.run_store.transition(
                event.run_id, "retryable_failed", "shutdown",
                "runtime stopped accepting new pipelines", event=event,
                expected={"analyzing"}, owner_id=event.owner_id,
                execution_attempt=event.execution_attempt,
            )
            return {
                "status": "unavailable", "error": "runtime_shutting_down",
                "source": source, "event_id": event.event_id,
                "run_id": event.run_id, "trace_id": event.trace_id,
                "events": len(incidents), "source_event_id": source_event_id,
                "camera_id": camera_id, "reused": False,
            }
        return {
            "status": "ok", "source": source, "event_id": event.event_id,
            "run_id": event.run_id, "trace_id": event.trace_id, "events": len(incidents),
            "source_event_id": source_event_id, "camera_id": camera_id, "reused": False,
        }

    def _ensure_runtime_identity(self) -> None:
        if not getattr(self, "owner_id", ""):
            configured = str(getattr(self.settings, "runtime_owner_id", "") or "").strip()
            self.owner_id = configured or (
                f"worker-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            )
        self.run_lease_seconds = max(
            0.1, float(getattr(self.settings, "run_lease_seconds", 30.0))
        )
        self.run_heartbeat_seconds = max(
            0.05, float(getattr(self.settings, "run_heartbeat_seconds", 5.0))
        )
        self.run_recovery_scan_seconds = max(
            0.5, float(getattr(self.settings, "run_recovery_scan_seconds", 5.0))
        )

    def _ensure_lifecycle(self) -> None:
        """Initialize lifecycle state for lightweight recovery-test instances too."""
        if not hasattr(self, "_lifecycle_lock"):
            self._lifecycle_lock = threading.Lock()
            self._pipeline_threads = {}
            self._accepting_runs = True

    def _start_pipeline(self, event, target, role: str) -> bool:
        self._ensure_lifecycle()
        run_id = str(event.run_id or event.event_id or uuid.uuid4().hex)
        with self._lifecycle_lock:
            if not self._accepting_runs:
                return False
            current = self._pipeline_threads.get(run_id)
            if current is not None and current.is_alive():
                return False
            thread = threading.Thread(
                target=self._run_tracked_pipeline,
                args=(run_id, target, event),
                name=f"agent-{role}-{run_id}",
                daemon=True,
            )
            self._pipeline_threads[run_id] = thread
            thread.start()
            return True

    def _run_tracked_pipeline(self, run_id: str, target, event) -> None:
        try:
            target(event)
        finally:
            current = threading.current_thread()
            with self._lifecycle_lock:
                if self._pipeline_threads.get(run_id) is current:
                    self._pipeline_threads.pop(run_id, None)

    def lifecycle_status(self) -> dict:
        self._ensure_lifecycle()
        with self._lifecycle_lock:
            active_runs = sorted(
                run_id for run_id, thread in self._pipeline_threads.items()
                if thread.is_alive()
            )
            accepting = self._accepting_runs
        analysis = self.analysis_limiter.status() if hasattr(self, "analysis_limiter") else {
            "inflight": 0
        }
        return {
            "status": "accepting" if accepting else "draining",
            "accepting": accepting,
            "active_run_count": len(active_runs),
            "active_run_ids": active_runs,
            "analysis_inflight": int(analysis.get("inflight", 0)),
        }

    def shutdown(self, timeout: float = 10.0) -> dict:
        """Stop recovery and drain tracked work; leases recover anything left after timeout."""
        self._ensure_lifecycle()
        with self._lifecycle_lock:
            self._accepting_runs = False
        self.stop_recovery_monitor(timeout=min(3.0, max(0.1, float(timeout))))
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lifecycle_lock:
                threads = [
                    thread for thread in self._pipeline_threads.values()
                    if thread.is_alive()
                ]
            if not threads:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            threads[0].join(timeout=min(0.2, remaining))
        remaining = max(0.0, deadline - time.monotonic())
        analysis_idle = True
        if hasattr(self, "analysis_limiter"):
            analysis_idle = self.analysis_limiter.wait_for_idle(remaining)
        status = self.lifecycle_status()
        status["drained"] = (
            status["active_run_count"] == 0
            and status["analysis_inflight"] == 0
            and analysis_idle
        )
        status["recovery"] = "lease_recovery_required" if not status["drained"] else "none"
        return status

    def _claim_run(self, run_id: str, allowed_statuses: set[str] | None = None) -> dict | None:
        self._ensure_runtime_identity()
        return self.run_store.claim_run(
            run_id, self.owner_id, self.run_lease_seconds, allowed_statuses
        )

    @staticmethod
    def _apply_claim(event, claimed: dict) -> None:
        event.owner_id = str(claimed.get("owner_id") or "")
        event.execution_attempt = int(claimed.get("execution_attempt") or 0)

    def _lease_guard(self, event) -> RunLeaseHeartbeat:
        self._ensure_runtime_identity()
        return RunLeaseHeartbeat(
            self.run_store,
            event.run_id,
            event.owner_id,
            event.execution_attempt,
            self.run_lease_seconds,
            self.run_heartbeat_seconds,
        )

    def start_recovery_monitor(self) -> None:
        """Periodically reclaim Runs whose former process died before lease expiry."""
        self._ensure_runtime_identity()
        self._ensure_lifecycle()
        with self._lifecycle_lock:
            self._accepting_runs = True
        current = getattr(self, "_recovery_thread", None)
        if current is not None and current.is_alive():
            return
        self._recovery_stop = threading.Event()
        self._recovery_thread = threading.Thread(
            target=self._recovery_loop,
            name=f"run-recovery-{self.owner_id}",
            daemon=True,
        )
        self._recovery_thread.start()

    def _recovery_loop(self) -> None:
        while not self._recovery_stop.wait(self.run_recovery_scan_seconds):
            try:
                summary = self.recover_incomplete_runs()
                if summary["audited"]:
                    print(
                        "[Recovery sweep] "
                        f"audited={summary['audited']} "
                        f"analysis={summary['analysis_resumed']} "
                        f"tools={summary['tools_resumed']} "
                        f"manual={summary['manual_takeover']}"
                    )
            except Exception as exc:
                print(f"[Recovery sweep] failed: {type(exc).__name__}: {exc}")

    def stop_recovery_monitor(self, timeout: float = 3.0) -> None:
        """Stop the background recovery sweep without abandoning active Runs."""
        stop = getattr(self, "_recovery_stop", None)
        thread = getattr(self, "_recovery_thread", None)
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, float(timeout)))

    @staticmethod
    def _ingress_identity(source: str, camera_id: str, source_event_id: str,
                          body: dict, image_bytes: bytes) -> tuple[str, str]:
        semantic_body = {
            key: value for key, value in body.items()
            if key not in {
                "source", "source_event_id", "sourceEventId", "camera_id", "cameraId",
            }
        }
        canonical = json.dumps(
            semantic_body, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")
        image_hash = hashlib.sha256(image_bytes or b"").digest()
        payload_hash = hashlib.sha256(canonical + b"\x00" + image_hash).hexdigest()
        if not source_event_id:
            return "", payload_hash
        identity = f"ingress-v1\n{source.strip()}\n{camera_id.strip()}\n{source_event_id}"
        ingest_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return ingest_key, payload_hash

    @staticmethod
    def _reused_ingress_response(run: dict) -> dict:
        snapshot = run.get("event") or {}
        run_status = str(run.get("status") or "")
        return {
            "status": "filtered" if run_status == "filtered" else "ok",
            "source": run.get("source") or snapshot.get("raw_json", {}).get("source", "external"),
            "event_id": run.get("event_id") or snapshot.get("event_id", ""),
            "run_id": run.get("run_id") or snapshot.get("run_id", ""),
            "trace_id": run.get("trace_id") or snapshot.get("trace_id", ""),
            "events": len(snapshot.get("events") or []),
            "source_event_id": run.get("source_event_id") or snapshot.get("source_event_id", ""),
            "camera_id": run.get("camera_id") or snapshot.get("camera_id", ""),
            "run_status": run_status,
            "reused": True,
        }

    def approve(self, action: str, data: dict) -> tuple[dict, int]:
        if action not in {"approve", "reject"}:
            return {"status": "error", "message": f"unsupported approval action: {action}"}, 400
        pending_id = data.get("approval_id") or data.get("pending_id") or data.get("id") or ""
        if not pending_id:
            return {"status": "error", "message": "approval_id missing"}, 400
        order = self.human_loop._load_order(pending_id)
        if not order:
            return {"status": "error", "message": f"approval order not found: {pending_id}"}, 404
        status = "approved" if action == "approve" else "rejected"
        current_status = order.get("status", "pending")
        if current_status not in {"pending", status}:
            return {"status": "error", "message": f"approval order already {current_status}: {pending_id}"}, 409

        if current_status == "pending":
            result = self.human_loop.handle(pending_id, action)
        else:
            result = f"工单 {pending_id} 已{status}，继续幂等恢复执行"
        order = self.human_loop._load_order(pending_id) or order
        event_id = data.get("event_id") or order.get("event_id", "")
        run_id = order.get("run_id", "")
        trace_id = order.get("trace_id", "")
        operator = data.get("operator") or "frontend"
        comment = data.get("comment") or ""
        detail = f"{operator} {result}" + (f"；备注：{comment}" if comment else "")
        approval_step = timeline(status, "人工审批", detail)
        db_persisted = self.database.update_approval_status(event_id, pending_id, status, approval_step)

        review_only = (
            order.get("hold_reason") in self.human_loop.REVIEW_ONLY_HOLD_REASONS
        )
        if action == "reject":
            actuator_action = "cancel"
        elif review_only:
            actuator_action = "review"
        else:
            actuator_action = "execute"
        execution = self.actuator.handle(order, actuator_action)
        execution_step = timeline(execution.get("status", "execution"), "执行回写", execution.get("detail", ""))
        execution_persisted = self.database.update_execution_status(event_id, pending_id, execution, execution_step)
        lifecycle_status = (
            "succeeded" if execution.get("status") in {"executed", "reviewed"} else "cancelled"
        )
        if run_id and self.run_store.get(run_id):
            current_event = dict(
                (self.run_store.get(run_id) or {}).get("event") or {}
            )
            event_patch = {
                "approval_status": status,
                "lifecycle_status": lifecycle_status,
                "execution_id": execution.get("execution_id", ""),
                "execution_status": execution.get("status", ""),
                "execution_result": execution.get("detail", ""),
                "execution_actions": execution.get("commands", []) or [],
                "timeline": (
                    list(current_event.get("timeline") or [])
                    + [approval_step, execution_step]
                ),
            }
            replan = copy.deepcopy(current_event.get("evidence_replan") or {})
            if review_only and replan:
                resolution = dict(replan.get("review_resolution") or {})
                if not resolution:
                    resolution = {
                        "approval_id": pending_id,
                        "decision": status,
                        "operator": operator,
                        "resolved_at": datetime.now().isoformat(),
                    }
                replan["status"] = "reviewed" if action == "approve" else "cancelled"
                replan["manual_review_required"] = False
                replan["review_resolution"] = resolution
                event_patch["evidence_replan"] = replan
            self.run_store.patch_event(run_id, event_patch)
            self.run_store.transition(
                run_id, lifecycle_status, "complete", f"approval {status}; actuator {execution.get('status', '-')}",
            )
        message = {
            "type": "approval_result",
            "event_id": event_id,
            "run_id": run_id,
            "trace_id": trace_id,
            "approval_id": pending_id,
            "approval_status": status,
            "lifecycle_status": lifecycle_status,
            "result": result,
            "operator": operator,
            "comment": comment,
            "db_persisted": db_persisted,
            "execution_id": execution.get("execution_id", ""),
            "execution_status": execution.get("status", ""),
            "execution_result": execution.get("detail", ""),
            "execution_actions": execution.get("commands", []) or [],
            "execution_db_persisted": execution_persisted,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "timeline": [approval_step, execution_step],
        }
        print(f"[Approval] {pending_id} -> {status} event={event_id or '-'} db={db_persisted} exec={execution.get('status', '-')}")
        self._remember_and_broadcast(message)
        return {"status": "ok", **message}, 200

    def pending_approvals(self) -> list:
        return self.human_loop.get_pending()

    def pending_recoveries(self, limit: int = 50) -> list:
        return self.run_store.list_manual_takeover(limit)

    def get_trace(self, run_id: str) -> dict | None:
        service = getattr(self, "trace_service", None)
        if service is None:
            service = RunTraceService(self.run_store, self.tool_executor.store)
            self.trace_service = service
        return service.get(run_id)

    def runtime_metrics(self, limit: int = 500) -> dict:
        service = getattr(self, "metrics_service", None)
        if service is None:
            service = RuntimeMetricsService(
                self.run_store, self.tool_executor.store, self.analysis_limiter
            )
            self.metrics_service = service
        return service.snapshot(limit)

    def resolve_recovery(self, data: dict) -> tuple[dict, int]:
        if not self.lifecycle_status()["accepting"]:
            return {"status": "error", "message": "runtime is shutting down"}, 503
        run_id = str(data.get("run_id") or "").strip()
        resolution = str(data.get("resolution") or "").strip().lower()
        if not run_id:
            return {"status": "error", "message": "run_id missing"}, 400
        if resolution not in {"retry_analysis", "mark_succeeded", "mark_failed", "cancel"}:
            return {"status": "error", "message": f"unsupported resolution: {resolution or '-'}"}, 400
        run = self.run_store.get(run_id)
        if not run:
            return {"status": "error", "message": f"run not found: {run_id}"}, 404
        if run.get("status") != "manual_takeover":
            return {"status": "error", "message": f"run is {run.get('status')}, not manual_takeover"}, 409

        operator = str(data.get("operator") or "operator")[:80]
        comment = str(data.get("comment") or "")[:300]
        detail = f"{operator} selected {resolution}" + (f"; {comment}" if comment else "")
        event = restore_event(run.get("event") or {}, self.settings.alarm_dir)
        if resolution == "retry_analysis":
            tool_rows = self.tool_executor.store.list_for_run(run_id)
            if tool_rows:
                return {
                    "status": "error",
                    "message": "tool execution history exists; automatic analysis replay is unsafe",
                }, 409
            event.lifecycle_status = "analyzing"
            event.timeline.append(timeline("recovery", "人工恢复", detail))
            self.run_store.transition(
                run_id, "analyzing", "analysis", detail, event=event,
                expected={"manual_takeover"},
            )
            claimed = self._claim_run(run_id, {"analyzing"})
            if claimed is None:
                return {
                    "status": "error",
                    "message": "run was claimed by another worker",
                }, 409
            self._apply_claim(event, claimed)
            self.run_store.mark_recovery_started(
                run_id, detail, owner_id=event.owner_id,
                execution_attempt=event.execution_attempt,
            )
            self.run_store.save_snapshot(
                event, owner_id=event.owner_id,
                execution_attempt=event.execution_attempt,
            )
            if not self._start_pipeline(event, self._run_agent_pipeline, "manual-retry"):
                return {"status": "error", "message": "runtime is shutting down"}, 503
            final_status = "analyzing"
        else:
            final_status = {
                "mark_succeeded": "succeeded",
                "mark_failed": "permanent_failed",
                "cancel": "cancelled",
            }[resolution]
            event.lifecycle_status = final_status
            event.timeline.append(timeline(final_status, "人工结案", detail))
            self.run_store.transition(
                run_id, final_status, "manual_resolution", detail, event=event,
                expected={"manual_takeover"},
            )
        message = {
            "type": "recovery_resolution", "run_id": run_id,
            "event_id": event.event_id, "trace_id": event.trace_id,
            "resolution": resolution, "lifecycle_status": final_status,
            "operator": operator, "comment": comment, "timeline": event.timeline,
        }
        self._remember_and_broadcast(message)
        return {"status": "ok", **message}, 200

    def recover_incomplete_runs(self) -> dict:
        """Audit unfinished runs and resume only when replay is provably safe."""
        summary = {"audited": 0, "analysis_resumed": 0, "tools_resumed": 0,
                   "finalized": 0, "manual_takeover": 0}
        if not self.lifecycle_status()["accepting"]:
            return summary
        for candidate in self.run_store.recover_expired_runs():
            summary["audited"] += 1
            run_id = candidate["run_id"]
            run = self._claim_run(run_id)
            if run is None:
                continue
            event = restore_event(run.get("event") or {}, self.settings.alarm_dir)
            self._apply_claim(event, run)
            tool_rows = self.tool_executor.store.list_for_run(run_id)
            uncertain = [row for row in tool_rows if row.get("status") in {"running", "failed"}]
            if uncertain:
                names = ",".join(f"{row['tool']}.{row['action']}:{row['status']}" for row in uncertain)
                self._mark_recovery_manual(
                    run, f"uncertain tool outcomes: {names}", event=event
                )
                summary["manual_takeover"] += 1
                continue

            if not event.run_id or not event.events:
                self._mark_recovery_manual(run, "event snapshot is incomplete", event=event)
                summary["manual_takeover"] += 1
                continue

            validation = (event.dispatch_decision or {}).get("plan_validation") or {}
            expected_actions = set(validation.get("final_plan") or [])
            successful_actions = {
                f"{row['tool']}.{row['action']}" for row in tool_rows if row.get("status") == "succeeded"
            }
            if expected_actions and expected_actions.issubset(successful_actions):
                final_status = "waiting_approval" if event.approval_status == "pending" else "succeeded"
                event.lifecycle_status = final_status
                self.run_store.transition(
                    run_id, final_status, "recovery", "all persisted tool steps already succeeded",
                    event=event, owner_id=event.owner_id,
                    execution_attempt=event.execution_attempt,
                )
                summary["finalized"] += 1
                continue

            self.run_store.mark_recovery_started(
                run_id, "startup recovery audit", owner_id=event.owner_id,
                execution_attempt=event.execution_attempt,
            )
            self.run_store.save_snapshot(
                event, owner_id=event.owner_id,
                execution_attempt=event.execution_attempt,
            )
            if expected_actions and run.get("status") == "executing":
                if self._start_pipeline(event, self._resume_tool_execution, "tool-recovery"):
                    summary["tools_resumed"] += 1
                continue

            current = run.get("status")
            if current != "analyzing":
                if current != "retryable_failed":
                    self.run_store.transition(
                        run_id, "retryable_failed", "recovery", "interrupted before side effects",
                        event=event, owner_id=event.owner_id,
                        execution_attempt=event.execution_attempt,
                    )
                self.run_store.transition(
                    run_id, "analyzing", "analysis", "safe analysis replay after restart",
                    event=event, expected={"retryable_failed"},
                    owner_id=event.owner_id,
                    execution_attempt=event.execution_attempt,
                )
            if self._start_pipeline(event, self._run_agent_pipeline, "analysis-recovery"):
                summary["analysis_resumed"] += 1
        return summary

    def _resume_tool_execution(self, event) -> None:
        lease = self._lease_guard(event)
        try:
            lease.__enter__()
            lease.ensure_owned()
            if not event.repair_trace:
                event.repair_trace = new_repair_trace(
                    "not_allowed", "repair_trace_unavailable_during_tool_recovery"
                )
            level = str((event.dispatch_decision or {}).get("final_level") or "B")
            rules = [dict(item) for item in self.dispatch.RULES.get(level, [])]
            self.dispatch.execute_plan(event, sorted(rules, key=lambda item: item["priority"]))
            append_unique_attributions(
                event,
                self.safety.failure_attributor.tool_failures(event.dispatch_actions),
            )
            failures = [
                item for item in event.dispatch_actions
                if item.get("status") in {"failed", "indeterminate"}
            ]
            if event.approval_status == "pending":
                final_status = "waiting_approval"
            elif failures:
                final_status = "manual_takeover"
            else:
                final_status = "succeeded"
            event.lifecycle_status = final_status
            event.timeline.append(timeline("recovery", "恢复执行", f"恢复结果: {final_status}"))
            self.run_store.transition(
                event.run_id, final_status, "recovery", "resumed missing tool steps",
                event=event,
                error_type=(failures[0].get("error_type", "") if failures else ""),
                error_message=(str(failures[0].get("result", "")) if failures else ""),
                owner_id=event.owner_id,
                execution_attempt=event.execution_attempt,
            )
            self.database.update_event_snapshot(event)
            self.broadcaster.publish({
                "type": "recovery_result", "run_id": event.run_id,
                "event_id": event.event_id, "trace_id": event.trace_id,
                "lifecycle_status": final_status, "actions": event.dispatch_actions,
                "timeline": event.timeline,
                "failure_attributions": event.failure_attributions,
                "repair_trace": event.repair_trace,
            })
        except StaleRunOwnerError as exc:
            print(f"[Recovery] stale worker rejected: {exc}")
        except Exception as exc:
            run = self.run_store.get(event.run_id)
            if run:
                try:
                    self._mark_recovery_manual(
                        run, f"tool recovery failed: {type(exc).__name__}: {exc}",
                        event=event,
                    )
                except StaleRunOwnerError:
                    print(f"[Recovery] stale worker error result rejected: {event.run_id}")
        finally:
            lease.__exit__(None, None, None)

    def _mark_recovery_manual(self, run: dict, detail: str, event=None) -> None:
        event = event or restore_event(run.get("event") or {}, self.settings.alarm_dir)
        event.lifecycle_status = "manual_takeover"
        event.timeline.append(timeline("manual_takeover", "人工接管", detail))
        self.run_store.transition(
            run["run_id"], "manual_takeover", "recovery", detail, event=event,
            error_type="recovery_requires_review", error_message=detail,
            owner_id=event.owner_id,
            execution_attempt=event.execution_attempt,
        )

    def recent_events(self, limit: int = 20) -> list:
        return self._recent.recent(limit)

    def health(self) -> dict:
        stats = self.database.get_stats()
        return {
            "runtime": self.lifecycle_status(),
            "llm": {
                **self.safety.health(),
                "timeout_seconds": self.settings.llm_timeout_seconds,
                "capacity": self.analysis_limiter.status(),
            },
            "notifier": self.notifier.status(),
            "database": {"status": "ok", **stats},
            "approval": {"status": "ok", "pending": len(self.pending_approvals())},
            "recovery": {"status": "ok", "manual_takeover": len(self.pending_recoveries())},
            "sop": {
                "status": "ready",
                "catalog_version": self.sop_retriever.catalog_version,
                "documents": len(self.sop_retriever.documents),
            },
            "actuator": self.actuator.status(),
            "tools": sorted(self.dispatch.tools.keys()),
            "recent_events": len(self._recent.recent(10)),
            "last_event": (self._recent.recent(1) or [{}])[0],
        }

    def set_adjacent_frame_provider(self, provider) -> None:
        """Install a trusted local/read-only frame archive provider."""
        self.evidence_tool.set_provider(provider)

    @staticmethod
    def _copy_analysis_result(target, source) -> None:
        for name in (
            "events", "llm_analysis", "llm_recommendation", "llm_status",
            "llm_error", "llm_latency_ms", "llm_json_valid", "llm_model",
            "prompt_version", "context_manifest", "failure_attributions",
            "repair_trace", "sop_retrieval", "rag_status",
        ):
            setattr(target, name, copy.deepcopy(getattr(source, name)))
        for name in ("_decision_context_text", "_decision_memory_context"):
            if hasattr(source, name):
                setattr(target, name, copy.deepcopy(getattr(source, name)))

    @staticmethod
    def _require_evidence_review(event, trace: dict, reason: str) -> None:
        reason = str(reason or "temporal_evidence_unresolved")[:120]
        recommendation = dict(getattr(event, "llm_recommendation", {}) or {})
        assessment = dict(recommendation.get("evidence_assessment") or {})
        assessment["review_required"] = True
        assessment["autonomy_allowed"] = False
        assessment["review_reason"] = reason
        assessment.setdefault("relation", "insufficient")
        recommendation["evidence_assessment"] = assessment
        event.llm_recommendation = recommendation
        trace["status"] = "manual_review"
        trace["manual_review_required"] = True
        trace["review_reason"] = reason

    def _run_bounded_evidence_replan(self, event, *, lease,
                                     first_raw_output: str) -> None:
        """Run at most one read-only frame acquisition and one re-decision."""
        trace = new_replan_trace(enabled=True)
        event.evidence_replan = trace
        append_decision_round(
            trace,
            round_index=1,
            recommendation=event.llm_recommendation or {},
            context_manifest=event.context_manifest or {},
            raw_output=first_raw_output,
        )

        recommendation = event.llm_recommendation or {}
        request = recommendation.get("evidence_request") or {}
        action = str(request.get("action") or "decide")
        assessment = recommendation.get("evidence_assessment") or {}
        relation = str(
            assessment.get("relation") or recommendation.get("evidence_relation") or ""
        ).lower()
        if not event.llm_json_valid:
            trace["status"] = "model_unavailable"
            return
        if relation == "conflict":
            self._require_evidence_review(event, trace, "multimodal_evidence_conflict")
            return
        if action == "manual_review":
            self._require_evidence_review(event, trace, "model_requested_evidence_review")
            return
        if action != "inspect_adjacent_frames":
            if relation == "insufficient":
                self._require_evidence_review(
                    event, trace, "temporal_evidence_unresolved"
                )
                return
            trace["status"] = "not_requested"
            return
        if str(getattr(self.safety, "mode", "ollama") or "") != "ollama":
            self._require_evidence_review(
                event, trace, "temporal_evidence_unavailable"
            )
            return

        lease.ensure_owned()
        receipt, supplemental_images = self.evidence_tool.execute(event)
        trace["evidence_actions"].append(receipt)
        event.timeline.append(timeline(
            "evidence_acquisition",
            "Read-only temporal evidence",
            f"{receipt.get('tool')} status={receipt.get('status')} frames={receipt.get('frame_count', 0)}",
        ))
        self.run_store.save_snapshot(
            event, owner_id=event.owner_id,
            execution_attempt=event.execution_attempt,
        )
        if receipt.get("status") != "succeeded" or not supplemental_images:
            self._require_evidence_review(
                event, trace,
                f"temporal_evidence_{receipt.get('status') or 'unavailable'}",
            )
            return

        replan_event = copy.deepcopy(event)
        replan_result = {"raw": "", "error": ""}

        def reanalyze():
            try:
                replan_result["raw"] = self.safety.reanalyze(
                    replan_event,
                    supplemental_images=supplemental_images,
                    evidence_receipt=receipt,
                )
            except Exception as exc:
                replan_result["error"] = f"{type(exc).__name__}: {exc}"[:220]

        completed = self.analysis_limiter.try_start(
            reanalyze, name=f"agent-evidence-replan-{event.run_id or event.event_id}"
        )
        if completed is None:
            self._require_evidence_review(event, trace, "evidence_replan_capacity_exhausted")
            return
        if not completed.wait(self.settings.llm_timeout_seconds):
            self._require_evidence_review(event, trace, "evidence_replan_timeout")
            return
        lease.ensure_owned()
        if (
            replan_result["error"]
            or not replan_event.llm_json_valid
            or replan_event.llm_status != "success"
        ):
            if replan_result["error"]:
                trace["replan_error"] = replan_result["error"]
            self._require_evidence_review(event, trace, "evidence_replan_model_failed")
            return

        first_level = str(recommendation.get("risk_level") or "")
        second_level = str(
            (replan_event.llm_recommendation or {}).get("risk_level") or ""
        )
        self._copy_analysis_result(event, replan_event)
        event.evidence_replan = trace
        append_decision_round(
            trace,
            round_index=2,
            recommendation=event.llm_recommendation or {},
            context_manifest=event.context_manifest or {},
            raw_output=replan_result["raw"],
        )
        event.timeline.append(timeline(
            "evidence_replan",
            "Bounded evidence re-decision",
            "second and final decision round completed",
        ))

        level_weight = {"C": 1, "B": 2, "A": 3}
        review_reason = terminal_review_reason(event.llm_recommendation or {})
        if (
            first_level in level_weight and second_level in level_weight
            and level_weight[second_level] < level_weight[first_level]
        ):
            review_reason = "replan_risk_downgrade_requires_review"
        if review_reason:
            self._require_evidence_review(event, trace, review_reason)
        else:
            trace["status"] = "resolved"
        self.run_store.save_snapshot(
            event, owner_id=event.owner_id,
            execution_attempt=event.execution_attempt,
        )

    def _run_agent_pipeline(self, event) -> None:
        lease = self._lease_guard(event)
        try:
            lease.__enter__()
            lease.ensure_owned()
            if not event.repair_trace:
                event.repair_trace = new_repair_trace()
            analysis_event = copy.deepcopy(event)

            analysis_result = {"raw": ""}

            def analyze():
                analysis_result["raw"] = self.safety.analyze(analysis_event)

            completed = self.analysis_limiter.try_start(
                analyze, name=f"agent-analysis-{event.run_id or event.event_id}"
            )
            if completed is None:
                event.llm_model = self.safety.model
                event.prompt_version = self.safety.PROMPT_VERSION
                event.llm_status = "overloaded"
                event.llm_error = "analysis_capacity_exhausted"
                event.llm_json_valid = False
                event.llm_latency_ms = 0.0
                event.llm_analysis = (
                    "【LLM状态】本地多模态分析容量已满，系统已直接启用规则兜底调度。"
                    "高危事件仍按确定性安全规则执行。"
                )
                event.llm_recommendation = {}
                event.context_manifest = self.safety.context_builder.skipped_manifest(
                    event, "analysis_capacity_exhausted"
                )
                event.repair_trace = new_repair_trace(
                    "not_allowed", "model_capacity_exhausted"
                )
                append_unique_attributions(event, [
                    self.safety.failure_attributor.runtime_model_failure(
                        "overloaded", event.llm_error
                    )
                ])
                event.timeline.append(timeline("llm_overloaded", "LLM过载", "启用规则兜底调度"))
            elif not completed.wait(self.settings.llm_timeout_seconds):
                event.llm_model = self.safety.model
                event.prompt_version = self.safety.PROMPT_VERSION
                event.llm_status = "timeout"
                event.llm_error = f"analysis_timeout_after_{self.settings.llm_timeout_seconds}s"
                event.llm_json_valid = False
                event.llm_latency_ms = float(self.settings.llm_timeout_seconds * 1000)
                event.llm_analysis = (
                    f"【LLM状态】分析超过 {self.settings.llm_timeout_seconds}s，"
                    "系统已启用规则兜底调度。高危事件仍按确定性安全规则执行。"
                )
                event.llm_recommendation = {}
                event.context_manifest = self.safety.context_builder.skipped_manifest(
                    event, "analysis_timeout"
                )
                event.repair_trace = new_repair_trace("not_allowed", "model_timeout")
                append_unique_attributions(event, [
                    self.safety.failure_attributor.runtime_model_failure(
                        "timeout", event.llm_error
                    )
                ])
                event.timeline.append(timeline("llm_timeout", "LLM超时", "启用规则兜底调度"))
            else:
                self._copy_analysis_result(event, analysis_event)
            self._run_bounded_evidence_replan(
                event, lease=lease, first_raw_output=analysis_result["raw"]
            )
            repair_status = str((event.repair_trace or {}).get("status") or "")
            repair_already_recorded = any(
                item.get("stage") in {"model_repaired", "repair_exhausted"}
                for item in event.timeline
            )
            if repair_status == "repaired" and not repair_already_recorded:
                event.timeline.append(timeline(
                    "model_repaired", "模型输出纠错", "单次Schema修复成功，继续策略校验"
                ))
            elif repair_status == "exhausted" and not repair_already_recorded:
                event.timeline.append(timeline(
                    "repair_exhausted", "纠错预算耗尽", "停止模型修复并启用规则兜底"
                ))
            rules = self.dispatch.plan(event)
            decision = event.dispatch_decision or {}
            append_unique_attributions(
                event,
                self.safety.failure_attributor.policy_findings(decision),
            )
            event.lifecycle_status = "decided"
            event.timeline.append(timeline(
                "decided",
                "调度裁决",
                f"规则 {decision.get('rule_level', '-')} + LLM {decision.get('llm_level') or '-'} -> {decision.get('final_level') or '-'}",
            ))
            self.run_store.transition(
                event.run_id, "decided", "policy", "validated deterministic tool plan",
                event=event, expected={"analyzing"}, owner_id=event.owner_id,
                execution_attempt=event.execution_attempt,
            )
            self.run_store.transition(
                event.run_id, "executing", "tools", "tool execution started",
                event=event, expected={"decided"}, owner_id=event.owner_id,
                execution_attempt=event.execution_attempt,
            )
            self.dispatch.execute_plan(event, rules)
            append_unique_attributions(
                event,
                self.safety.failure_attributor.tool_failures(event.dispatch_actions),
            )
            if event.dispatch_actions:
                actions = "；".join(f"{item.get('tool', '')}.{item.get('action', '')}" for item in event.dispatch_actions)
                event.timeline.append(timeline("tools", "工具执行", actions))
            tool_failures = [
                item for item in event.dispatch_actions
                if item.get("status") in {"failed", "indeterminate"}
            ]
            if event.approval_status == "pending":
                event.timeline.append(timeline("pending_approval", "等待审批", event.approval_id or "已生成审批工单"))
                final_status = "waiting_approval"
            elif tool_failures:
                names = ",".join(f"{item.get('tool')}.{item.get('action')}" for item in tool_failures)
                event.timeline.append(timeline("manual_takeover", "人工接管", f"工具执行异常: {names}"))
                final_status = "manual_takeover"
            else:
                final_status = "succeeded"
            event.lifecycle_status = final_status
            self.run_store.transition(
                event.run_id, final_status,
                "approval" if final_status == "waiting_approval" else "complete",
                "waiting for human approval" if final_status == "waiting_approval"
                else ("tool outcome requires manual review" if tool_failures else "run completed"),
                event=event, expected={"executing"},
                error_type=(tool_failures[0].get("error_type", "") if tool_failures else ""),
                error_message=(str(tool_failures[0].get("result", "")) if tool_failures else ""),
                owner_id=event.owner_id,
                execution_attempt=event.execution_attempt,
            )
            # database.store runs in the middle of the immutable A/B/C chain;
            # refresh that same row after all tool results and timeline steps exist.
            self.database.update_event_snapshot(event)
            message = {
                "type": "alarm_with_llm",
                "event_id": event.event_id,
                "run_id": event.run_id,
                "trace_id": event.trace_id,
                "timestamp": event.timestamp,
                "source": event.raw_json.get("source", "external"),
                "lifecycle_status": event.lifecycle_status,
                "timeline": event.timeline,
                "events": [event_payload(item) for item in event.events],
                "llm_analysis": event.llm_analysis or "",
                "llm_recommendation": event.llm_recommendation or {},
                "llm_status": event.llm_status,
                "llm_error": event.llm_error,
                "llm_latency_ms": event.llm_latency_ms,
                "llm_json_valid": event.llm_json_valid,
                "llm_model": event.llm_model,
                "prompt_version": event.prompt_version,
                "context_manifest": event.context_manifest,
                "evidence_replan": event.evidence_replan,
                "failure_attributions": event.failure_attributions,
                "repair_trace": event.repair_trace,
                "sop_retrieval": event.sop_retrieval,
                "rag_status": event.rag_status,
                "dispatch_decision": event.dispatch_decision or {},
                "approval_id": event.approval_id or "",
                "approval_status": event.approval_status or "auto",
                "actions": event.dispatch_actions,
                "trace_validation": (self.get_trace(event.run_id) or {}).get("validation", {}),
            }
            self._remember_and_broadcast(message)
        except StaleRunOwnerError as exc:
            print(f"[Agent pipeline] stale worker rejected: {exc}")
        except Exception as exc:
            import traceback

            print(f"[Agent管线] 致命错误: {exc}")
            traceback.print_exc()
            event.llm_analysis = event.llm_analysis or f"LLM分析异常: {exc}"
            event.lifecycle_status = "manual_takeover"
            event.timeline.append(timeline("manual_takeover", "人工接管", f"{type(exc).__name__}: {exc}"))
            try:
                run = self.run_store.get(event.run_id)
                if run and run.get("status") not in {"succeeded", "permanent_failed", "cancelled"}:
                    self.run_store.transition(
                        event.run_id, "manual_takeover", "error", "unhandled pipeline error",
                        event=event, error_type=type(exc).__name__, error_message=str(exc),
                        owner_id=event.owner_id,
                        execution_attempt=event.execution_attempt,
                    )
            except StaleRunOwnerError:
                print(f"[Agent pipeline] stale error result rejected: {event.run_id}")
                return
            except Exception as state_exc:
                print(f"[Agent管线] 状态持久化失败: {state_exc}")
            self.broadcaster.publish({
                "type": "alarm_with_llm",
                "event_id": event.event_id,
                "run_id": event.run_id,
                "trace_id": event.trace_id,
                "timestamp": event.timestamp,
                "lifecycle_status": event.lifecycle_status,
                "timeline": event.timeline,
                "events": [event_payload(item) for item in event.events],
                "llm_analysis": event.llm_analysis,
                "actions": event.dispatch_actions,
            })
        finally:
            lease.__exit__(None, None, None)

    def _attach_evidence(self, event, prefix: str) -> None:
        evidence = event.image_bytes
        if evidence:
            try:
                evidence = annotate_image(evidence, event.events)
            except Exception as exc:
                print(f"[标注] 画框失败: {exc}")
        event.image_bytes = evidence
        if not evidence:
            return
        path = save_evidence(self.settings.alarm_dir, evidence, prefix=prefix)
        event.image_url = f"{self._public_base()}/alarms/{path.name}"
        print(f"  截图: {path}")

    def _should_report(self, event: dict, camera_id: str = "") -> bool:
        rect = event["bbox"]
        zone = f"{int(rect['x'] / 100)}-{int(rect['y'] / 100)}"
        key = camera_id, event["type"], zone
        now = time.monotonic()
        with self._report_lock:
            cooldown = self.COOLDOWNS.get(event["type"], 5)
            if key in self._last_report and now - self._last_report[key] < cooldown:
                return False
            self._last_report[key] = now
            return True

    def _remember_and_broadcast(self, message: dict) -> None:
        self._recent.remember(message, self._new_event_id)
        self.broadcaster.publish(message)

    def _public_base(self) -> str:
        return self.settings.public_url or f"http://localhost:{self.settings.http_port}"

    @staticmethod
    def _source_label(source: str) -> str:
        return "本地YOLO" if source == "local_yolo" else "外部事件接口"

    @staticmethod
    def _new_event_id() -> str:
        return "EVT_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    @staticmethod
    def _log_incident(event, source: str) -> None:
        print(f"\n{'=' * 60}\n[{event.timestamp}] 报警 source={source}")
        for item in event.events:
            icon = {"A": "🔴", "B": "🟡", "C": "🟢"}.get(item["level"], "⚪")
            print(f"  {icon} [{item['level']}级] {item['type']} | {item['detail']}")
        print(f"{'=' * 60}\n")
