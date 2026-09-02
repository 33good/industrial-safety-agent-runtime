# 可信多模态工业安全 Agent Runtime 与数字孪生系统

[![agent-quality-gates](https://github.com/33good/industrial-safety-agent-runtime/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/33good/industrial-safety-agent-runtime/actions/workflows/ci.yml)

> Event-driven multimodal safety Agent Runtime with bounded reasoning, deterministic guardrails and recoverable execution.

这是一个面向工业安全事件处置的**有界单 Agent Runtime**。实时画面先由本地 YOLO 完成目标跟踪、多次观测确认和事件去重，稳定的结构化告警进入 Agent；存在绑定截图时再由 Qwen2.5-VL 联合研判。系统以 SQLite 保存 Run、证据、决策、工具结果和恢复状态，并通过 WebSocket 将处置结果回写到 Three.js 数字孪生界面。

项目采用明确的模型与规则分工：LLM 负责生成带依据的候选判断，确定性规则、Guardrail 与 HITL 负责风险下限、工具权限和最终执行路径，从而形成可解释、可约束、可恢复的事件处置闭环。

## 可复核结果

| 验证对象 | 可复核结果 | 验证范围 |
| --- | --- | --- |
| 当前 `safety-v2.8` Runtime | [130/130 项单元与本地集成测试通过](benchmarks/reports/verification_summary.json)；7 类确定性离线 Benchmark 全部通过 | 默认不需要摄像头、GPU、Ollama 或真实外部设备 |
| 可靠执行与故障恢复 | [6/6 项故障注入基准通过](benchmarks/reports/runtime_faults.md)；专项测试覆盖双进程竞争与 3 类真实强杀边界 | 有限重试、重复副作用抑制、陈旧写入拒绝与不确定结果人工接管 |
| `safety-v2.6` 固定基线 | [40 个合成场景](benchmarks/reports/multimodal_latest.md)；系统级严格通过 34/40，候选动作 33/40 合规，经 Guardrail 后最终工具计划 40/40 合法 | 固定数据、固定 Prompt 与本地 Qwen 单轮回放 |
| Trace 契约 | [15/15 个完整链路与篡改反例通过](benchmarks/reports/trace_integrity.md)；v2.6 决策 Trace 40/40 完整 | Run、证据、决策、工具结果与终态的跨表一致性 |
| SOP 检索与拒答 | [8/8 个检索用例通过](benchmarks/reports/sop_retrieval.md)，其中无证据样例 2/2 正确拒答 | 版本化规程目录、引用绑定与无证据拒答 |

所有数字均绑定固定数据、版本指纹和可复核报告；逐例结果、消融配置与评测口径见 [Agent Benchmark](#agent-benchmark)。

## 系统架构

```mermaid
flowchart LR
    A["RTSP 视频流"] --> B["YOLO 跟踪<br/>多次观测确认与去重"]
    X["外部结构化告警"] --> C["稳定安全事件"]
    B --> C

    subgraph R["SQLite 持久化 Runtime · 状态机 / Lease / Fencing / Trace"]
        direction LR
        D["ContextBuilder<br/>作用域记忆 + SOP RAG"] --> E["Qwen2.5-VL<br/>候选研判"]
        E --> F["EvidenceConsistency<br/>+ Bounded Evidence Policy"]
        F -.->|至多一次只读补证| N["相邻帧"]
        N -.-> E
        F --> G["规则风险下限<br/>Guardrail"]
        G --> H{"确定性执行策略"}
        H --> I["HITL 审批"]
        H --> J["ToolExecutor"]
        H --> K["人工接管"]
        I --> J
    end

    C --> D
    J --> M["WebSocket<br/>Three.js 数字孪生"]
    K --> M
```

视觉帧是允许丢弃的高频数据，稳定事件才是 Agent Runtime 的业务输入；因此同一目标持续出现在画面中不会反复触发模型、通知或审批。

## 核心能力

- **事件语义稳定**：本地 YOLO 负责目标关联、多次观测确认和冷却去重，Agent 只消费带来源与证据身份的稳定事件。
- **模型权限受控**：Qwen2.5-VL 只生成候选研判；结构化校验、风险不可降级、SOP 引用绑定、工具白名单和 HITL 由确定性代码执行。
- **上下文可追溯**：ContextBuilder 在固定预算内选择规则证据、作用域事件记忆和版本化 SOP，记录选入、丢弃、引用及输入摘要。
- **执行可恢复**：入口幂等、持久化状态机、Lease/Heartbeat/Fencing Token、成功步骤复用和人工接管共同约束失败恢复。

## 一键离线验证

```powershell
python -m pip install -r requirements-ci.txt
python -B verify.py
```

默认门禁会运行 Policy、故障恢复、Context、受控修复、Runtime 指标、Trace、SOP 检索以及 130 项单元与本地集成测试；它会使用本地回环端口和临时 SQLite 子进程，但不会调用 Qwen、GPU、摄像头、Webhook 或工业设备。只有显式添加 `--live` 才会运行本地多模态模型评测。

## 部署与安全策略

- 当前运行形态为单机共享 SQLite，支持同主机多进程 Lease、Heartbeat 与 Fencing Token。
- 模型超时、过载、结构化输出失败或证据冲突时自动收缩自治权，由规则兜底或转人工复核。
- 外部执行与通知默认使用安全的本地适配器；模型权重、凭据、告警图片和运行数据库不进入 Git。
- SOP RAG 使用版本化规程目录，提供检索来源、引用绑定和无证据拒答。

第三方前端依赖与三维资产的作者、来源和许可证见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)，各组件按原许可证使用。

## 目录职责

```text
backend.py                  HTTP API、MJPEG 输出、服务装配
config.py                   .env 配置加载与 Settings
agents/                     感知、安全分析、调度、历史记忆、SOP 检索
agents/context_builder.py   上下文预算、优先级、去重、来源清单与输入指纹
agents/evidence_consistency.py 多模态证据关系与自治权收缩策略
agents/failure_attribution.py 失败归因、修复预算与人工接管策略
services/camera_stream.py   RTSP 单次拉流与最新帧缓存
services/local_vision.py    本地 YOLO、目标 ID 和事件稳定化
services/agent_runtime.py   Agent 编排、审批、证据、事件回写
services/run_store.py       入口幂等、Run 状态机、事件快照、迁移审计与恢复数据
services/run_lease.py       Worker 租约心跳与 fencing 所有权检查
services/tool_executor.py   幂等工具执行、有限重试与结果持久化
services/runtime_metrics.py 持久化事实聚合、阶段耗时与 P50/P95 指标投影
services/realtime.py        WebSocket 广播
services/evidence.py        告警证据图标注与保存
benchmarks/                 Agent 策略场景、评测器与基线报告
knowledge/sop/              可版本化的项目评测规程目录
tools/                      数据库、通知、报告、审批、执行器适配
frontend/                   三维数字孪生界面
serve.py                    本地 Supervisor、动态端口注入与子进程生命周期
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

干净克隆默认 `VISION_ENABLED=0`，无需私有权重即可通过 `/alarm` 接口验证 Agent 主链路。启用实时视觉时至少配置：

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

脚本会优先使用 `.venv`，确保本地 Ollama 可用，执行只读预检，再由单一 Supervisor 启动后端、WebSocket 和前端。服务默认只绑定 `127.0.0.1`；摄像头不可达时以 `degraded` 状态继续运行恢复循环和 `/alarm` 事件入口，不再自动创建公网隧道。

Supervisor 统一持有子进程 PID 和服务端口。收到 `Ctrl+C`/终止信号后会先停止视觉入口，再关闭新 Run 准入并在 `SHUTDOWN_DRAIN_SECONDS` 内排空在途 Agent Pipeline 与模型请求，最后关闭 WebSocket；超过期限的 Run 保留持久化状态并由 Lease/Fencing 恢复，而不是静默丢失。

打开：

```text
http://127.0.0.1:18080
```

健康检查：

```powershell
curl.exe http://127.0.0.1:5000/health
curl.exe http://127.0.0.1:5000/ready
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

GitHub Actions 使用 `requirements-ci.txt` 安装最小测试依赖并执行同一 `verify.py`，自动上传评测报告；完整本地运行依赖维护在 `requirements.txt`。

如果未放入模型权重，系统仍能显示 RTSP 视频和前端界面，但 `services.vision.status` 会为 `degraded`，并给出明确原因。

## 事件闭环

```text
检测框 -> 目标 ID -> 连续帧稳定化 -> PerceptionAgent
-> Qwen2.5-VL 风险分析与历史上下文
-> DispatchAgent 策略裁决
-> A 类事件人工审批
-> 执行器回写、数据库审计、通知与前端同步
```

事件证据保存到 `alarms/`，审批数据保存到 `data/pending/`，执行结果保存到 `data/executions/`，完整事件状态存储在 `data/alarms.db`。事件型 Memory 另使用同库的 `alarm_memory_facts`：每次报警按 `camera_id + event_family + 200px zone` 去重保存记忆事实，同一报警中的安全帽和背心标签不会被重复计数。只有同摄像头、同事件族、同区域且处于时间窗内的历史事实才能触发升级；缺少摄像头身份的旧记录会保留，但不会参与自动升级。

## API 与实时接口

```text
GET  /health              服务、摄像头、模型和工具状态
GET  /ready               核心服务就绪状态与降级依赖
GET  /camera/stream       前端 MJPEG 实时视频
GET  /camera/status       摄像头状态
GET  /recent_alarms       最近事件
GET  /approval/pending    待审批工单
GET  /recovery/pending    待人工接管的 Run
GET  /traces/{run_id}     查询并校验单个 Run 的完整 Agent Trace
GET  /metrics/runtime     最近 Run 的可靠性、工具和阶段延迟指标
POST /approval/approve    审批通过
POST /approval/reject     审批驳回
POST /recovery/resolve    人工重试分析或审计结案
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
python -m benchmarks.run_context_benchmark
python -m benchmarks.run_repair_benchmark
python -m benchmarks.run_runtime_metrics
python -m benchmarks.run_trace_benchmark
python -m benchmarks.run_sop_benchmark
python -m benchmarks.run_multimodal_benchmark --validate-only
python -m benchmarks.run_multimodal_benchmark --timeout 90 --require-model
python -m benchmarks.run_multimodal_benchmark --ablation --repeats 3 --rag-only --require-model --timeout 90 --output benchmarks/reports/multimodal_ablation_latest.json
```

报告生成到：

```text
benchmarks/reports/latest.json
benchmarks/reports/latest.md
benchmarks/reports/runtime_faults.json
benchmarks/reports/runtime_faults.md
benchmarks/reports/context_engineering.json
benchmarks/reports/context_engineering.md
benchmarks/reports/bounded_repair.json
benchmarks/reports/bounded_repair.md
benchmarks/reports/runtime_metrics.json
benchmarks/reports/runtime_metrics.md
benchmarks/reports/trace_integrity.json
benchmarks/reports/trace_integrity.md
benchmarks/reports/sop_retrieval.json
benchmarks/reports/sop_retrieval.md
benchmarks/reports/multimodal_latest.json
benchmarks/reports/multimodal_latest.md
benchmarks/reports/multimodal_ablation_latest.json
benchmarks/reports/multimodal_ablation_latest.md
```

八套评测分别覆盖确定性 Policy、Runtime 故障恢复、Context Engineering、受控纠错、Runtime 可观测与并发、Trace 完整性、SOP 检索/拒答，以及真实本地 Qwen2.5-VL 输出。Context 基准验证关键规则证据不丢失、预算截断、SOP/记忆优先级、重复上下文去重、引用注入边界和故障降级清单；纠错基准验证单次修复预算、失败后规则兜底、降级/越权拦截及不确定副作用转人工；可观测基准验证并发控制面无丢失、重复入口唯一所有者、持久指标一致性与分位数计算；Trace 基准覆盖完整处置链、过滤终态，以及缺失证据、跨表 ID 错配、伪造引用、修复预算越界、缺失阶段耗时和工具执行断链等反例。任一关键关联缺失都会令质量门禁失败。

多模态困难集包含 40 个场景，覆盖正常风险、遮挡/模糊/证据缺失、图像与检测 JSON 冲突、越权动作/非法输出，以及 SOP 无证据/错误引用/版本冲突。`safety-v2.6-bounded-generation` 的本地 Qwen2.5-VL + SOP RAG 固定回放中，结构化输出有效率为 100%，模型候选/Guardrail 后最终风险准确率为 75%/85%，候选动作合规率为 82.5%，最终工具计划合法率、Guardrail 修正率和决策 Trace 完整率均为 100%。

当前 `safety-v2.8-bounded-temporal-evidence` 将证据补充约束为有界决策：Qwen 首轮仅能选择直接决策、读取相邻帧或转人工；每次执行尝试最多执行一次只读补证和一次最终重判，随后统一进入既有 Guardrail/HITL。专项测试覆盖正常决策、证据冲突、补证不可用、重判失败、风险下调和人工复核等路径。

长评测使用 `multimodal-benchmark-checkpoint-v4` 逐例原子落盘，checkpoint 指纹同时绑定模型、主/修复 Prompt、Context Builder、最终 SOP grounding 策略、SOP 目录、上下文预算、重复次数和数据集摘要；任一输入版本变化都拒绝续跑，避免混合不同实验。`--resume` 仅复用指纹一致的已完成行，`--retry-failed` 可在环境恢复后重测模型失败行；checkpoint 不保存原始 Prompt、图像或模型全文。模型 warmup 若未产生合法结构化输出，评分阶段不会启动；正式运行连续两次模型失败也会熔断并保留断点。

完整报告按模型候选、系统裁决与最终执行三层分别记录结果，并保留逐例输出、失败归因、延迟及版本指纹，便于复跑和定位改进空间。

## Context Engineering

`ContextBuilder` 不再把检测 JSON、历史文本和全部 SOP 检索结果直接拼接进 Prompt。它先将内容拆成带 `source/trust/priority/freshness` 的上下文项，再在 `CONTEXT_TOKEN_BUDGET`（默认 1200，采用保守估算）内确定性选择：事件元数据、规则检测基线、SOP 状态和记忆状态属于必选证据，即使预算不足也保留并显式记录 overflow；SOP 引用、历史摘要和历史事件按优先级装入，重复项或超预算项会携带原因进入 `dropped_items`。

每次构建都会产生 `agent-context-v1` 清单，只保存所选/丢弃项的来源、估算成本与内容摘要，不复制原始 SOP 或历史文本。模型只能引用真正进入本轮上下文的 `citation_id`，不能把“检索到但被预算裁掉”的候选冒充为依据。Qwen 调用前还会对实际送入模型的图像副本计算摘要，并与文本上下文摘要组合成 `model_input_sha256`；因此 Trace 可以区分原始证据图、缩放后的模型输入和本轮文本上下文。多模态评测器会记录关键上下文保留率、截断率、上下文 P50/P95 估算量以及输入指纹完整性。

## 有界时序补证

Memory 和 SOP 在首轮研判前已经由确定性代码检索，因此没有被重新包装成“模型主动调用的工具”。当前唯一能为决策增加新事实的动作是 `vision.inspect_adjacent_frames`：它只接受内部 `local_yolo` 事件，并同时校验摄像头 ID、事件 `frameId`、未公开的流会话标识以及锚点仍在缓冲区，随后读取内存环形缓冲中的固定邻域帧。外部 `/alarm` 即使伪造相同的摄像头和帧号也无法读取本地帧；模型不能传入路径、URL 或任意偏移，动作也不会产生外部副作用。原始图片不写入 Trace，持久记录仅包含帧号、相对偏移、输入摘要和工具回执摘要。

补证发生在首轮 Qwen 研判之后、Dispatch/Guardrail 和任何工具副作用之前，并仍受 Run Lease 约束。每个 `execution_attempt` 的预算固定为最多 2 个决策轮次、1 次证据动作和全局最多 1 次 Schema 修复；因此一次不中断的执行尝试理论上最多产生 3 次模型请求，不存在无限 ReAct。进程在补证阶段崩溃后，恢复尝试可能重新执行无副作用的模型分析或帧读取，当前不把该预算误称为跨崩溃的 Run 级上限。第二轮复用首轮的 Memory/SOP 数据快照及同一组选中项，只增加相邻帧；再次请求补证、补证不可用、重判超时/失败、证据仍不足或第二轮降低首轮风险时，一律收敛到人工复核。证据复核批准只记录人工结论，不会被解释为高风险 Actuator 授权。

评测器将模型候选、证据裁决和最终执行结果分层记录，使补证机制本身与模型效果可以独立复核；对应实验配置与逐例结果统一保存在 `benchmarks/reports/`。

## 失败归因与受控纠错

系统使用版本化失败码区分模型空输出、Schema 非法、推理超时/过载、候选动作越权、工具重试耗尽和副作用状态不确定。这里只记录可审计的阶段、错误码、证据摘要和处置结果，不保存或伪造模型思维链。

只有发生在任何工具副作用之前的模型 JSON/Schema 错误允许修复，并且整个 Run 最多调用一次纯格式修复 Prompt。修复输入将原始模型输出视为不可信数据，不能新增上下文中不存在的事实或 SOP 引用；修复结果仍须通过风险不可降级和工具白名单 Guardrail。第二次仍无效时直接执行确定性规则。工具重试由 `ToolExecutor` 的工具级安全策略管理；重试耗尽、永久错误和 `indeterminate` 副作用均不交给模型重规划，而是进入 `manual_takeover`。

本地 Ollama 主研判和 Schema 修复还分别设置固定 `num_predict` 上限，防止客户端超时后服务端继续无界生成并长期占用 GPU；修复预算小于主研判预算。HTTP 超时、生成预算和有界并发分别约束等待时间、单次输出规模和同时运行数量，三者不能互相替代。

## 可验证 SOP RAG

SOP 目录为每个片段保存 `document_id`、章节、版本、生效日期和来源。检索器结合事件类型、关键词和词法相似度返回候选引用；低于阈值时输出 `no_evidence`，模型仍可进行视觉风险判断，但必须拒绝提供 SOP 依据。

模型只能引用候选中的 `citation_id`。解析器会拦截虚构编号，并将通过校验的引用重新绑定到目录中的规范原文、章节、版本和来源，避免仅凭模型生成的文字伪造规程依据。模型候选引用与最终系统依据分开保存：`final-sop-grounding-v1` 只接纳已经检索、确实进入本轮受控 Context、且与结构化事件类型精确匹配的规程；因此模型漏引不能让最终规则裁决失去依据，模型偏好的词法近似结果也不能自动升级为最终依据。SOP 证据只参与解释，不授予工具权限，也不能降低规则风险等级。

## Agent Trace 完整性

每个接入事件都会生成稳定的证据包标识 `evidence_id`；上游未提供 `source_event_id` 时使用 `generated:{event_id}` 作为内部来源标识，但不会因此启用入口去重。`GET /traces/{run_id}` 将 SQLite 中的 Run、事件快照、Context 清单、Memory 升级来源、失败归因、修复记录、两轮决策摘要、补证请求/回执、检索候选/模型引用/最终 grounding、模型候选计划、Guardrail 裁决、工具执行记录、阶段耗时和状态迁移组装为版本化的 `agent-trace-v4` 文档。

校验器会检查 `source_event_id → event_id/run_id/trace_id → evidence_id → round1 → evidence receipt/round2 → context/model_input_sha256 → failure/repair → prompt_version/catalog_version → retrieved/model-selected/final-grounded citation → candidate_plan/guardrail_decision → step_id/execution_id/idempotency_key → tool status → final_status` 的跨表一致性，并拒绝决策轮次或补证次数越界、使用非只读证据工具、补证成功却缺少帧摘要、人工复核未被执行、一个 Run 出现两次模型修复、最终引用未被检索/注入或缺少事件精确绑定。正常终态要求最终计划中的每个工具都有唯一且可关联的持久执行记录；`succeeded` 和 `waiting_approval` 还要求所有计划动作均已确认成功。过滤事件使用较轻的 ingress Trace 契约，不伪造不存在的模型或工具步骤。

## Runtime 指标与可观测性

`GET /metrics/runtime?limit=500` 从最近至多 500 个持久化 Run、状态迁移和工具执行事实生成 `agent-runtime-metrics-v1` 快照，不在 Agent 主链路同步上报外部监控。快照包含状态分布、成功率、人工接管率、恢复成功率、模型降级率与修复成功率、失败阶段/错误码、工具级成功率和重试次数，并分别统计端到端、接入到决策、决策到执行、执行到结果、审批等待、模型调用和工具调用的 P50/P95。单 Run Trace 同时携带 `agent-run-timing-v1`，缺少关键耗时边界或迁移数量不一致会被校验器拒绝。

指标口径是“有界的 SQLite 最近 Run 样本”，分析槽位的 `inflight/rejected_total` 则是当前进程生命周期计数；接口会显式返回两者范围，不将本地样本包装为分布式 SLA。`run_runtime_metrics` 使用 12 个线程执行 32 个唯一 Run 和 20 个同 Key 重复请求，验证控制面无丢失、唯一入口所有者、指标聚合与工具重试统计，同时只记录观察到的吞吐和延迟，不设置依赖机器性能的虚假通过阈值。该结果不代表 Qwen、YOLO 或外部通知吞吐。

## Runtime 可靠性

每个首次接收的业务事件分配独立的 `event_id`、`run_id` 与 `trace_id`。提供上游事件标识时，`agent_runs.ingest_key` 的 SQLite 部分唯一索引通过原子 `INSERT ... ON CONFLICT DO NOTHING` 保证并发请求只有一个创建者；其余请求复用原 Run。载荷摘要同时持久化，用于拒绝同 Key 异载荷。`RunStore` 继续持久化事件快照以及 `analyzing → decided → executing → waiting_approval/succeeded` 状态迁移，并审计迁移来源、目标、阶段、原因和版本。

工具执行由统一 `ToolExecutor` 管理，并将 `step_id`、`execution_id`、幂等键、尝试次数、结果和错误类型持久化到 SQLite。仅对能够证明安全的动作进行有限重试；通知、审批和报告等外部副作用不会在结果不确定时盲目重放。同一动作成功后会复用已保存结果，审批后的执行也使用稳定执行 ID 防止重复处置。

本地 VLM 分析使用有界并发槽位。请求超时后，迟到结果只能写入隔离副本，不会覆盖已进入规则兜底的主事件；对应槽位要等后台请求真实结束后才释放，因此连续模型卡顿不会无限创建推理线程。容量耗尽的新事件会记录为 `overloaded` 并直接执行确定性规则。

每个活动 Run 由 `owner_id + lease_until + execution_attempt` 标识当前执行者。Worker 必须通过 SQLite 条件更新原子抢占，成功后 `execution_attempt` 单调递增并作为 fencing token；心跳只允许当前 token 续租。状态迁移、事件快照与工具结果落库都会同时校验 owner、token、租约和活动状态，过期 Worker 恢复运行后无法覆盖新 Worker 的结果。工具幂等键不包含 token，因此新 Worker 可以复用已确认成功的动作，而不会因换届重复产生副作用。

进程启动时会审计未完成 Run，后台也会周期扫描租约过期任务，避免服务恰好在旧租约到期前重启而漏掉恢复：无工具副作用的任务可安全重放分析；部分成功的工具步骤会复用既有结果并补齐未执行步骤；全部工具成功但状态未落盘时直接对账完成；结果不确定或失败的外部动作进入 `manual_takeover`，通过恢复 API 审计结案。`retry_analysis` 仅允许用于没有工具执行历史的 Run。

当前实现聚焦单机共享 SQLite 下的入口去重、同主机多进程 Worker 租约和持久恢复。入口回归覆盖 20 次顺序重复提交、20 线程并发提交、20 个独立 SQLite 连接竞争、过滤结果复用、来源/摄像头隔离、异 JSON/图片载荷冲突及无幂等键兼容路径。多进程专项测试进一步让两个独立操作系统进程竞争同一 Run，并在“Run 已创建”“副作用已发生但结果未落库”“工具结果已落库但 Run 未完成”三个边界真实终止 Worker，验证唯一执行权、陈旧写入拒绝、成功步骤复用和不确定结果人工接管。
