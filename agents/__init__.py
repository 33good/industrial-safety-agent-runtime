"""
智能体系统框架
感知Agent → 安全Agent → 调度Agent → 工具执行
"""
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime


@dataclass
class AlarmEvent:
    """标准化报警事件"""
    timestamp: str
    events: list  # [{"type":"未戴安全帽", "level":"B", "bbox":{...}, "detail":"..."}]
    event_id: str = ""
    run_id: str = ""
    trace_id: str = ""
    source_event_id: str = ""
    ingest_key: str = ""
    ingest_payload_hash: str = ""
    camera_id: str = ""
    raw_json: dict = field(default_factory=dict)
    image_bytes: bytes = b""
    image_url: str = ""
    llm_analysis: Optional[str] = None
    llm_recommendation: dict = field(default_factory=dict)
    llm_status: str = "pending"
    llm_error: str = ""
    llm_latency_ms: float = 0.0
    llm_json_valid: bool = False
    llm_model: str = ""
    prompt_version: str = ""
    sop_retrieval: dict = field(default_factory=dict)
    rag_status: str = "not_run"
    dispatch_decision: dict = field(default_factory=dict)
    dispatch_actions: list = field(default_factory=list)
    approval_id: str = ""
    approval_status: str = "auto"
    lifecycle_status: str = "detected"
    timeline: list = field(default_factory=list)


class BaseAgent:
    """Agent 基类"""
    def __init__(self, name: str):
        self.name = name

    def log(self, msg: str):
        text = f"[{self.name}] {msg}"
        try:
            print(text)
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "utf-8"
            safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
            print(safe)
