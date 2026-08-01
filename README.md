# 本地视觉工业安全 Agent 系统

本项目是一个面向工业现场安全事件处置的本地多模态 Agent 系统。摄像头通过 RTSP 提供实时画面，本机 GPU 完成 PPE/人员/车辆等目标检测；稳定的视觉事件再进入感知、认知、决策、审批与执行闭环。前端以三维数字孪生大屏呈现现场视频、事件证据、Agent 裁决和处置状态。

## 系统架构

```text
RTSP 摄像头
  -> CameraStreamWorker：单次拉流，提供 MJPEG 和最新 BGR 帧
  -> LocalVisionWorker：本地 YOLO 推理、IoU 目标 ID、连续帧确认
  -> PerceptionAgent：PPE、区域入侵、人车距离、复合风险
  -> SafetyAgent：Qwen2.5-VL + 时空记忆 + 带版本引用的 SOP RAG
  -> DispatchAgent：规则与 LLM 建议融合，生成受约束处置路径
  -> HumanLoop / Database / Notifier / Reporter / Actuator
  -> WebSocket、SQLite、3D 数字孪生前端
```

视觉帧是高频数据，允许在推理压力下丢弃旧帧；只有经过跟踪、连续帧确认和冷却去重的安全事件才会触发 LLM、通知与审批。因此，模型不会因为同一人员持续出现在画面中而反复调用 Agent。

## 目录职责

```text
backend.py                  HTTP API、MJPEG 输出、服务装配
config.py                   .env 配置加载与 Settings
agents/                     感知、安全分析、调度、历史记忆、SOP 检索
services/camera_stream.py   RTSP 单次拉流与最新帧缓存
services/local_vision.py    本地 YOLO、目标 ID 和事件稳定化
services/agent_runtime.py   Agent 编排、审批、证据、事件回写
services/run_store.py       入口幂等、Run 状态机、事件快照、迁移审计与恢复数据
services/tool_executor.py   幂等工具执行、有限重试与结果持久化
services/realtime.py        WebSocket 广播
services/evidence.py        告警证据图标注与保存
benchmarks/                 Agent 策略场景、评测器与基线报告
knowledge/sop/              可版本化的项目评测规程目录
tools/                      数据库、通知、报告、审批、执行器适配
frontend/                   三维数字孪生界面
models/                     本地视觉权重，不提交到 Git
data/                       SQLite、审批、报告、执行记录
```

## 首次配置

需要 Python 3.11 或更高版本。Windows 下可直接创建隔离环境并安装完整运行依赖：

```powershell
setup.bat
```

等价的手动命令：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

GPU 推理所需的 PyTorch 请根据本机 CUDA 版本从官方渠道单独安装；通用依赖文件不锁定某个 CUDA wheel。项目使用 `.env` 保存本地设备地址、模型路径和通知密钥，`.env` 已被 Git 忽略。

```powershell
Copy-Item .env.example .env
```

干净克隆默认 `VISION_ENABLED=0`，无需私有权重即可通过演示回放和 `/alarm` 接口验证 Agent 主链路。启用实时视觉时至少配置：

```text
CAMERA_RTSP_URL=rtsp://user:password@camera-ip:554/live
VISION_MODEL_PATH=models/yolo26n_safety6_demo_best.pt
NOTIFY_WEBHOOK=...
```

不要将 RTSP 密码、钉钉 Webhook 或公网地址写回代码或提交到 Git。

## 本地模型与 YOLO26

`LocalVisionWorker` 面向 Ultralytics `YOLO` 兼容权重设计。仓库当前的本地 YOLO26 权重为：

```text
VISION_MODEL_PATH=models/yolo26n_safety6_demo_best.pt
VISION_DEVICE=0
```

模型必须至少输出以下语义类别：

```text
person
helmet
vest
```

可选类别：

```text
forklift / vehicle
fire / flame
```

类别名称与现有规则的映射在 `services/local_vision.py` 的 `DEFAULT_CLASS_MAP` 中维护。若 YOLO26 训练标签名称不同，可通过 `.env` 设置 `VISION_CLASS_MAP`，例如：

```text
VISION_CLASS_MAP={"hard_hat":1,"hi_vis_vest":2,"fork_lift":4}
```

启用视觉后默认开启 `VISION_REQUIRE_PPE=1`。如果权重缺少 `person`、`helmet` 或 `vest` 任一类别，视觉服务会显示为 `degraded`，不会把通用 COCO 模型误用于 PPE 告警。仅进行人员/车辆调试时，才应临时设为 `0`。

当前 Python 环境必须使用 CUDA 版 PyTorch，`/health` 中的 `services.vision.cuda_available` 应为 `true`；仅检测到 RTX 显卡但 PyTorch 为 CPU 版时，模型不会使用 GPU。

## 启动与验证

```powershell
start.bat
```

脚本会优先使用 `.venv`，检查本地 Qwen/Ollama 后启动后端、WebSocket 和前端。摄像头、公网隧道与通知 Webhook 均为可选能力；缺失时保留演示回放和通用事件 API，不阻塞 Agent Runtime。

打开：

```text
http://localhost:8080
```

健康检查：

```powershell
curl.exe http://127.0.0.1:5000/health
```

重点检查：

```text
services.camera.status = online
services.vision.status = online
services.vision.cuda_available = true
services.vision.model_loaded = true
services.websocket.clients >= 1
```

无需模型、摄像头或网络即可运行完整离线质量门禁：

```powershell
verify.bat
```

已启动本地 Qwen 时，可额外运行真实多模态对照：

```powershell
verify.bat --live
```

GitHub Actions 使用 `requirements-ci.txt` 安装最小测试依赖并执行同一 `verify.py`，自动上传评测报告；完整本地演示依赖维护在 `requirements.txt`。

如果未放入模型权重，系统仍能显示 RTSP 视频和前端界面，但 `services.vision.status` 会为 `degraded`，并给出明确原因。

## 事件闭环

```text
检测框 -> 目标 ID -> 连续帧稳定化 -> PerceptionAgent
-> Qwen2.5-VL 风险分析与历史上下文
-> DispatchAgent 策略裁决
-> A 类事件人工审批
-> 执行器回写、数据库审计、通知与前端同步
```

事件证据保存到 `alarms/`，审批数据保存到 `data/pending/`，执行结果保存到 `data/executions/`，完整事件状态存储在 `data/alarms.db`。

## API 与实时接口

```text
GET  /health              服务、摄像头、模型和工具状态
GET  /camera/stream       前端 MJPEG 实时视频
GET  /camera/status       摄像头状态
GET  /recent_alarms       最近事件
GET  /approval/pending    待审批工单
GET  /recovery/pending    待人工接管的 Run
POST /approval/approve    审批通过
POST /approval/reject     审批驳回
POST /recovery/resolve    人工重试分析或审计结案
POST /demo/trigger        回放样例，复用完整 Agent 管线
POST /alarm               通用外部事件接入接口，支持入口幂等
WS   :5001                前端告警、研判、审批状态广播
```

`POST /alarm` 是与硬件厂商无关的调试/集成入口；本地摄像头主链路由 `LocalVisionWorker` 直接调用 `AgentRuntime.ingest_detection()`。外部系统可在请求体提供 `source_event_id`，或发送同值的 `Idempotency-Key` 请求头。Runtime 根据服务端确定的 `source`、规范化 `camera_id` 和 `source_event_id` 生成入口幂等键：首次请求返回 `reused: false`，重复请求返回原有 `event_id/run_id/trace_id` 和 `reused: true`，不会再次保存证据、广播告警或启动 Agent 线程。同一幂等键对应不同载荷时返回 HTTP 409；不提供幂等标识时保持原有兼容行为。

```powershell
curl.exe -X POST http://127.0.0.1:5000/alarm `
  -H "Content-Type: application/json" `
  -H "Idempotency-Key: camera-01-alarm-20260801-001" `
  -d '{"body":{"cameraId":"camera-01","objInfo":[{"targetType":0,"targetId":301,"confidence":94,"posRect":{"x":360,"y":520,"width":90,"height":210}}]}}'
```

该最小载荷表示检测到一名未关联到安全帽/反光背心的人员，会触发 B 级 PPE 事件。即使请求没有形成安全事件或被冷却策略过滤，只要提供了幂等标识，系统也会保存终态为 `filtered` 的轻量 Run；重复请求仍返回同一组 ID 和 `reused: true`。未提供幂等标识的过滤请求不会创建 Run，以维持旧接口行为。

## Agent Benchmark

检测器只负责把连续视频转换成稳定事件；项目的主要质量门禁放在事件之后的 Agent 决策链。第一版基准覆盖结构化输出处理、风险不可降级、保守升级、必选动作补齐、越权动作拒绝和模型异常规则兜底。

```powershell
python -m benchmarks.run_agent_benchmark
python -m benchmarks.run_runtime_faults
python -m benchmarks.run_sop_benchmark
python -m benchmarks.run_multimodal_benchmark --timeout 90 --require-model
```

报告生成到：

```text
benchmarks/reports/latest.json
benchmarks/reports/latest.md
benchmarks/reports/runtime_faults.json
benchmarks/reports/runtime_faults.md
benchmarks/reports/sop_retrieval.json
benchmarks/reports/sop_retrieval.md
benchmarks/reports/multimodal_latest.json
benchmarks/reports/multimodal_latest.md
```

四套评测分别覆盖确定性 Policy、Runtime 故障恢复、SOP 检索/拒答，以及真实本地 Qwen2.5-VL 输出。最近一次本机多模态评测中，无 RAG 与 SOP RAG 两组系统级场景均为 5/5；两组结构化输出、风险等级准确率、不可降级率和 Guardrail 最终计划有效率均为 100%。SOP RAG 组的引用覆盖率、引用精确率和无证据拒答率均为 100%，无 RAG 组引用覆盖率为 0%。

多模态数据集当前只有 5 个核心回放场景，输入是生成回放图与结构化检测结果，因此这些数字用于回归和 Agent 决策对照，不代表真实现场 YOLO/VQA 精度，也不能外推为生产 SLA。延迟报告执行模型预热并记录 P50/P95，但单轮小样本不用于宣称性能提升。

## 可验证 SOP RAG

SOP 目录为每个片段保存 `document_id`、章节、版本、生效日期和来源。检索器结合事件类型、关键词和词法相似度返回候选引用；低于阈值时输出 `no_evidence`，模型仍可进行视觉风险判断，但必须拒绝提供 SOP 依据。

模型只能引用候选中的 `citation_id`。解析器会拦截虚构编号，并将通过校验的引用重新绑定到目录中的规范原文、章节、版本和来源，避免仅凭模型生成的文字伪造规程依据。SOP 证据只参与解释，不授予工具权限，也不能降低规则风险等级。

## Runtime 可靠性边界

每个首次接收的业务事件分配独立的 `event_id`、`run_id` 与 `trace_id`。提供上游事件标识时，`agent_runs.ingest_key` 的 SQLite 部分唯一索引通过原子 `INSERT ... ON CONFLICT DO NOTHING` 保证并发请求只有一个创建者；其余请求复用原 Run。载荷摘要同时持久化，用于拒绝同 Key 异载荷。`RunStore` 继续持久化事件快照以及 `analyzing → decided → executing → waiting_approval/succeeded` 状态迁移，并审计迁移来源、目标、阶段、原因和版本。

工具执行由统一 `ToolExecutor` 管理，并将 `step_id`、`execution_id`、幂等键、尝试次数、结果和错误类型持久化到 SQLite。仅对能够证明安全的动作进行有限重试；通知、审批和报告等外部副作用不会在结果不确定时盲目重放。同一动作成功后会复用已保存结果，审批后的执行也使用稳定执行 ID 防止重复处置。

本地 VLM 分析使用有界并发槽位。请求超时后，迟到结果只能写入隔离副本，不会覆盖已进入规则兜底的主事件；对应槽位要等后台请求真实结束后才释放，因此连续模型卡顿不会无限创建推理线程。容量耗尽的新事件会记录为 `overloaded` 并直接执行确定性规则。

进程启动时会审计未完成 Run：无工具副作用的任务可安全重放分析；部分成功的工具步骤会复用既有结果并补齐未执行步骤；全部工具成功但状态未落盘时直接对账完成；结果不确定或失败的外部动作进入 `manual_takeover`，通过恢复 API 审计结案。`retry_analysis` 仅允许用于没有工具执行历史的 Run。

当前实现提供单节点 SQLite 下的入口去重和持久恢复语义，不宣称分布式 exactly-once。线程并发回归覆盖20次顺序重复提交、20线程并发提交、20个独立 SQLite 连接竞争、过滤结果复用、来源/摄像头隔离、异 JSON/图片载荷冲突及无幂等键兼容路径，并验证每条工具副作用链只执行一次。这里的独立连接仍运行在同一操作系统进程中，不等同于真实多进程验证。多副本租约、fencing token、跨服务事务和外部接收方幂等仍属于后续生产化工作。

## 当前与后续

当前仓库只保留通用 RTSP、本地 YOLO26、通用事件入口和 Agent Runtime 主线。下一阶段先增加多进程租约、fencing token 与真实强杀恢复验证，再补齐 Trace 自动校验，随后扩充匿名化真实场景集和多轮重复评测。
