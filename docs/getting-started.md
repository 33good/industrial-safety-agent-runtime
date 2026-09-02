# 启动与接入

[返回项目首页](../README.md) · [Agent Runtime 设计](agent-runtime.md) · [评测与结果](evaluation.md)

本文说明如何在本机完成环境准备、离线验证、服务启动，以及可选的 YOLO26、Qwen 和事件入口配置。

## 最短路径

只验证 Agent Runtime 与质量门禁：

```powershell
python -m pip install -r requirements-ci.txt
python -B verify.py
```

默认模型接入方式为 Ollama。请先安装 Ollama 并准备 `.env` 中配置的模型，再启动完整本地服务：

```powershell
ollama pull qwen2.5vl:7b
.\setup.bat
.\start.bat
```

启动后访问：

```text
前端        http://127.0.0.1:18080
HTTP API    http://127.0.0.1:5000
WebSocket   ws://127.0.0.1:5001
```

服务默认只绑定 `127.0.0.1`，不会自动创建公网隧道。摄像头不可达时，Runtime 会以降级状态继续提供恢复循环和 `/alarm` 事件入口。

## 环境准备

项目需要 Python 3.11 或更高版本。`setup.bat` 会创建 `.venv`、安装完整依赖，并在 `.env` 不存在时从 `.env.example` 生成本地配置。

等价的手动命令：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

GPU 推理所需的 PyTorch 请根据本机 CUDA 版本从官方渠道单独安装；通用依赖文件不锁定特定 CUDA wheel。

干净克隆默认设置 `VISION_ENABLED=0`；配置好本地 Qwen 后，无需私有 YOLO 权重即可通过 `/alarm` 验证 Agent 主链路。启用完整本地视觉链路时，在 `.env` 中配置：

```text
VISION_ENABLED=1
CAMERA_RTSP_URL=rtsp://user:password@camera-ip:554/live
VISION_MODEL_PATH=models/yolo26n_safety6_demo_best.pt
```

外部通知是独立的可选能力，需要时再配置 `NOTIFY_WEBHOOK`。

`.env`、模型权重、告警图片、运行数据库和日志均已从 Git 排除。请勿将 RTSP 密码、Webhook 或公网地址写回源码。

## 本地模型与 YOLO26

`LocalVisionWorker` 面向 Ultralytics `YOLO` 兼容权重设计。当前本地权重配置示例：

```text
VISION_MODEL_PATH=models/yolo26n_safety6_demo_best.pt
VISION_DEVICE=0
```

权重至少需要提供以下语义类别：

```text
person
helmet
vest
```

可选类别为 `forklift / vehicle` 与 `fire / flame`。类别映射由 `services/local_vision.py` 中的 `DEFAULT_CLASS_MAP` 维护；训练标签不同时，可通过 `VISION_CLASS_MAP` 显式映射：

```text
VISION_CLASS_MAP={"hard_hat":1,"hi_vis_vest":2,"fork_lift":4}
```

启用视觉后默认开启 `VISION_REQUIRE_PPE=1`。权重缺少 `person`、`helmet` 或 `vest` 任一类别时，视觉服务会进入 `degraded`，避免将通用检测模型误用于 PPE 告警。仅进行人员或车辆链路调试时，才适合临时设为 `0`。

使用 GPU 时，`/health` 中的 `services.vision.cuda_available` 应为 `true`。仅检测到显卡但 PyTorch 为 CPU 构建时，模型不会使用 GPU。

## 启动、停止与健康检查

`start.bat` 会优先使用 `.venv`，检查并启动本地 Ollama，确认配置模型可用，完成只读预检，再由单一 Supervisor 启动后端、WebSocket 和前端。依赖或模型缺失时，脚本会在启动前返回明确错误。

停止由 Supervisor 启动的本地服务：

```powershell
.\stop.bat
```

Supervisor 统一持有子进程 PID 和端口。收到 `Ctrl+C` 或终止信号后，它会依次：

1. 停止视觉事件入口并关闭新 Run 准入；
2. 在 `SHUTDOWN_DRAIN_SECONDS` 内排空在途 Pipeline 与模型请求；
3. 关闭 WebSocket；
4. 将超过排空期限的 Run 保留为可由 Lease/Fencing 恢复的持久状态。

健康检查：

```powershell
curl.exe http://127.0.0.1:5000/health
curl.exe http://127.0.0.1:5000/ready
```

启用完整视觉链路时，重点检查：

```text
services.camera.status = online
services.vision.status = online
services.vision.cuda_available = true
services.vision.model_loaded = true
services.websocket.clients >= 1
```

未放入模型权重时，前端与 `/alarm` 仍可运行；视觉服务会显示 `degraded` 并返回明确原因。

## 事件闭环

```text
检测框 → 目标 ID → 多次观测确认与事件去重 → 稳定安全事件
       → ContextBuilder + Qwen2.5-VL 候选研判
       → EvidenceConsistency + Dispatch + Guardrail
       → HITL / ToolExecutor / 人工接管
       → SQLite 审计 + WebSocket 数字孪生回写
```

事件证据保存到 `alarms/`，审批数据保存到 `data/pending/`，执行结果保存到 `data/executions/`，完整事件状态保存在 `data/alarms.db`。

事件型 Memory 使用同库的 `alarm_memory_facts`，按 `camera_id + event_family + 200px zone` 对历史事实去重。只有同摄像头、同事件族、同区域且位于时间窗内的历史事实可以参与规则升级；缺少摄像头身份的旧记录会保留，但不参与自动升级。

## API 与实时接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 服务、摄像头、模型和工具状态 |
| `GET` | `/ready` | 核心服务就绪状态与降级依赖 |
| `GET` | `/camera/stream` | 前端 MJPEG 视频流 |
| `GET` | `/camera/status` | 摄像头状态 |
| `GET` | `/recent_alarms` | 最近事件 |
| `GET` | `/approval/pending` | 待审批工单 |
| `GET` | `/recovery/pending` | 待人工接管的 Run |
| `GET` | `/traces/{run_id}` | 查询并校验完整 Agent Trace |
| `GET` | `/metrics/runtime` | Runtime、工具和阶段延迟指标 |
| `POST` | `/approval/approve` | 审批通过 |
| `POST` | `/approval/reject` | 审批驳回 |
| `POST` | `/recovery/resolve` | 人工重试分析或审计结案 |
| `POST` | `/alarm` | 通用外部事件入口，支持入口幂等 |
| `WS` | `:5001` | 告警、研判和审批状态广播 |

本地摄像头主链路由 `LocalVisionWorker` 直接调用 `AgentRuntime.ingest_detection()`；`POST /alarm` 则提供与硬件厂商无关的调试和系统集成入口。

### 幂等事件提交

外部系统可在请求体提供 `source_event_id`，或发送同值的 `Idempotency-Key` 请求头。Runtime 根据服务端确定的来源、规范化摄像头 ID 和上游事件标识生成入口幂等键。

```powershell
curl.exe -X POST http://127.0.0.1:5000/alarm `
  -H "Content-Type: application/json" `
  -H "Idempotency-Key: camera-01-alarm-20260801-001" `
  -d '{"body":{"cameraId":"camera-01","objInfo":[{"targetType":0,"targetId":301,"confidence":94,"posRect":{"x":360,"y":520,"width":90,"height":210}}]}}'
```

该载荷表示检测到一名未关联安全帽或反光背心的人员，会形成 B 级 PPE 事件。接口语义如下：

- 首次请求返回 `reused: false`；
- 重复请求复用原 `event_id / run_id / trace_id`，返回 `reused: true`；
- 同一幂等键对应不同 JSON 或图片载荷时返回 HTTP 409；
- 带幂等标识的过滤结果也保存为轻量终态 Run，避免稍后重复提交转化为新事件；
- 不提供幂等标识时保持兼容行为。

## 目录职责

| 路径 | 职责 |
| --- | --- |
| `backend.py` | HTTP API、MJPEG 输出与服务装配 |
| `config.py` | `.env` 配置加载与 Settings |
| `agents/` | 感知、安全分析、调度、历史记忆与 SOP 检索 |
| `agents/context_builder.py` | 上下文预算、优先级、去重、来源清单与输入指纹 |
| `agents/evidence_consistency.py` | 多模态证据关系与自治权收缩策略 |
| `agents/failure_attribution.py` | 失败归因、修复预算与人工接管策略 |
| `services/agent_runtime.py` | Agent 编排、审批、证据与状态回写 |
| `services/run_store.py` | 入口幂等、状态机、迁移审计与恢复数据 |
| `services/run_lease.py` | Worker 租约、心跳与 Fencing 所有权检查 |
| `services/tool_executor.py` | 幂等工具执行、有限重试与结果持久化 |
| `services/runtime_metrics.py` | 持久化事实聚合、阶段耗时与分位数指标 |
| `services/camera_stream.py` | RTSP 单次拉流与最新帧缓存 |
| `services/local_vision.py` | 本地 YOLO、目标 ID 与事件稳定化 |
| `services/realtime.py` | WebSocket 广播 |
| `services/evidence.py` | 告警证据图标注与保存 |
| `benchmarks/` | 场景数据、评测器与基线报告 |
| `knowledge/sop/` | 可版本化的项目评测规程目录 |
| `tools/` | 数据库、通知、报告、审批与执行适配器 |
| `frontend/` | Three.js 数字孪生界面 |
| `serve.py` | Supervisor、端口注入与子进程生命周期 |

第三方前端依赖与三维资产的作者、来源和许可证见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
