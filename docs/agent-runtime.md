# Agent Runtime 设计

[返回项目首页](../README.md) · [启动与接入](getting-started.md) · [评测与结果](evaluation.md)

Runtime 的设计目标是把模型能力放进一条有边界、可审计、可恢复的工业事件处置链。模型负责候选判断，确定性代码负责证据范围、风险下限、工具权限、审批路径和失败收敛。

```text
稳定事件 → 受控 Context → Qwen 候选研判 → 证据一致性裁决
         → Guardrail / Dispatch → ToolExecutor / HITL / 人工接管
         → 审批通过后进入 ActuatorTool → 状态回写与 Trace 校验
```

## Context Engineering

`ContextBuilder` 将检测事实、历史事件和 SOP 候选拆分为带 `source / trust / priority / freshness` 的上下文项，再在 `CONTEXT_TOKEN_BUDGET` 内确定性选择。

上下文分为两类：

- **必选证据**：事件元数据、规则检测基线、SOP 状态和 Memory 状态；预算不足时仍保留，并记录 overflow。
- **预算项**：SOP 引用、历史摘要和历史事件；按优先级装入，重复项与超预算项记录在 `dropped_items`。

每次构建产生版本化的 `agent-context-v1` 清单，只保存所选/丢弃项的来源、估算成本和内容摘要。模型只能引用实际进入本轮 Context 的 `citation_id`。

Qwen 调用前，系统还会计算实际模型图片副本的摘要，并与文本 Context 摘要组合成 `model_input_sha256`。Trace 因而能够区分原始证据、缩放后的模型输入和本轮文本上下文。

## 有界时序补证

Memory 和 SOP 在首轮研判前由确定性代码检索。当前唯一可为决策增加新事实的动作是只读工具 `vision.inspect_adjacent_frames`。

该工具同时校验：

- 事件来源必须为内部 `local_yolo`；
- 摄像头 ID 与事件 `frameId` 必须匹配；
- 流会话标识必须有效；
- 锚点帧仍在内存缓冲区；
- 模型不能传入路径、URL 或任意帧偏移。

工具从内存环形缓冲读取固定邻域帧。Trace 不保存原始图片，只记录帧号、相对偏移、输入摘要和工具回执摘要。

补证发生在首轮 Qwen 之后、Dispatch/Guardrail 和任何外部副作用之前，并继续受当前 Run Lease 约束。每个 `execution_attempt` 的预算为：

| 预算项 | 上限 |
| --- | ---: |
| 决策轮次 | 2 |
| 只读证据动作 | 1 |

Schema 修复预算属于整个 Run，全局最多 1 次。一次不中断的执行尝试中，主研判、单次格式修复与补证后重判合计最多产生 3 次模型请求。

第二轮沿用首轮的 Memory/SOP 数据快照和 Context 选择，只增加相邻帧证据。以下情况统一收敛到人工复核：

- 再次请求补证；
- 补证不可用；
- 重判超时或失败；
- 第二轮证据仍不足；
- 第二轮降低首轮风险。

证据复核批准只记录人工证据结论，不会自动授予高风险执行权限。

## 失败归因与受控纠错

版本化失败码区分模型空输出、Schema 非法、推理超时/过载、候选动作越权、工具重试耗尽和副作用结果不确定。持久记录包含阶段、错误码、证据摘要和处置结果。

只有工具副作用发生前的模型 JSON/Schema 错误允许进入一次纯格式修复。修复过程遵循三条约束：

1. 原始模型输出按不可信数据处理；
2. 不得新增 Context 中不存在的事实或 SOP 引用；
3. 修复结果仍需通过风险不可降级与工具白名单 Guardrail。

第二次结构化校验仍失败时，系统执行确定性规则方案。工具重试由 `ToolExecutor` 的工具级安全策略管理；重试耗尽、永久错误和 `indeterminate` 副作用进入 `manual_takeover`。

主研判与 Schema 修复分别设置固定 `num_predict` 上限。HTTP 超时控制客户端等待，生成预算控制输出规模，有界并发控制同时运行数量，三层限制共同约束本地模型资源。

## 可验证 SOP RAG

SOP 目录为每个片段保存：

- `document_id`、章节和版本；
- 生效日期和来源；
- 事件类型与检索关键词。

检索器结合事件类型、关键词和词法相似度返回候选引用；低于阈值时输出 `no_evidence`。模型仍可做视觉风险判断，但不能声称存在 SOP 依据。

模型只能引用候选集合中的 `citation_id`。解析器会将合法引用重新绑定到目录中的规范原文、章节、版本和来源。最终的 `final-sop-grounding-v1` 只接纳同时满足以下条件的规程：

1. 已被本轮检索召回；
2. 已实际进入受控 Context；
3. 与结构化事件类型精确匹配。

SOP 证据用于解释与约束，不授予工具权限，也不能降低确定性规则风险等级。

## Agent Trace 完整性

每个接入事件生成稳定的 `evidence_id`。上游没有提供 `source_event_id` 时，系统使用 `generated:{event_id}` 表示内部来源身份，但不会因此启用入口去重。

`GET /traces/{run_id}` 将 SQLite 中的 Run、事件快照、Context 清单、Memory 升级来源、失败归因、修复记录、决策与补证摘要、SOP grounding、工具执行、阶段耗时和状态迁移组装为 `agent-trace-v4`：

```text
source_event_id
  → event_id / run_id / trace_id
  → evidence_id
  → round1 / evidence receipt / round2
  → context manifest / model_input_sha256
  → failure / repair
  → prompt_version / catalog_version
  → retrieved / model-selected / final-grounded citations
  → candidate_plan / guardrail_decision
  → step_id / execution_id / idempotency_key
  → tool status / final_status
```

Trace Validator 校验以下契约：

- 跨表 ID、执行 ID 与幂等键必须一致；
- 决策轮次、补证次数和修复次数不得越界；
- 证据工具必须只读，成功补证必须带帧摘要；
- 最终 SOP 引用必须已检索、已注入且与事件精确绑定；
- 正常终态中的计划动作必须存在唯一、可关联的持久执行记录；
- `succeeded` 与 `waiting_approval` 必须满足各自的工具执行契约；
- 过滤事件使用独立的轻量 ingress Trace 契约。

任何关键关联缺失都会让 Trace Benchmark 与完整质量门禁失败。

## Runtime 指标与可观测性

`GET /metrics/runtime?limit=500` 从最近的持久化 Run、状态迁移和工具执行事实生成 `agent-runtime-metrics-v1`，避免在 Agent 主链路同步上报外部监控。

快照包含：

- 状态分布、成功率、人工接管率和恢复成功率；
- 模型降级率、修复成功率、失败阶段和错误码；
- 工具级成功率与重试次数；
- 端到端、接入到决策、决策到执行、执行到结果、审批等待、模型调用和工具调用的 P50/P95。

单 Run Trace 同时携带 `agent-run-timing-v1`。关键耗时边界缺失或迁移数量不一致时，Trace 校验失败。

Runtime 指标明确区分两个范围：SQLite 最近 Run 样本用于持久化业务指标；分析槽位的 `inflight / rejected_total` 用于当前进程容量观察。并发基准使用 12 个线程执行 32 个唯一 Run 和 20 个同 Key 重复请求，验证入口唯一所有者、控制面完整性、指标聚合和工具重试统计。

## Runtime 可靠性

### 入口身份与状态机

首次接收的业务事件分配独立的 `event_id`、`run_id` 和 `trace_id`。提供上游事件标识时：

1. Runtime 根据来源、摄像头和上游事件标识生成 `ingest_key`；
2. `agent_runs.ingest_key` 的 SQLite 部分唯一索引通过原子插入确定唯一创建者；
3. 其余请求复用原 Run；
4. 持久化载荷摘要用于拒绝同 Key 异载荷。

`RunStore` 保存事件快照、版本化状态迁移及其来源、阶段和原因。正常主链路按 `analyzing → decided → executing → waiting_approval / succeeded` 推进，异常路径由状态机收敛到对应失败或人工接管终态。

### 幂等工具执行

`ToolExecutor` 将以下事实持久化到 SQLite：

- `step_id` 与稳定的 `execution_id`；
- 工具幂等键和尝试次数；
- 执行结果与标准化错误类型。

只有能够证明安全的动作进入有限重试。成功步骤会复用已保存结果；结果不确定的外部动作不会被盲目重放，审批后的执行同样使用稳定执行 ID。

### 有界模型并发

本地 VLM 使用有界并发槽位。客户端超时后，迟到结果只能写入隔离副本，无法覆盖已经进入规则兜底的主事件；槽位会在后台请求真实结束后释放。容量耗尽的新事件记录为 `overloaded`，并直接执行确定性规则方案。

### Lease 与 Fencing Token

活动 Run 由 `owner_id + lease_until + execution_attempt` 标识当前执行者。

- Worker 通过 SQLite 条件更新原子抢占 Run；
- 成功抢占后 `execution_attempt` 单调递增，并作为 Fencing Token；
- 心跳仅允许当前 Token 续租；
- 状态迁移、事件快照和工具结果写入同时校验 Owner、Token、租约与活动状态；
- 过期 Worker 恢复运行后无法覆盖新 Worker 的结果。

工具幂等键不包含 Fencing Token，因此新 Worker 可以复用已经确认成功的动作，不会因执行权换届生成新的副作用身份。

### 恢复与结果收敛

进程启动时审计未完成 Run，后台周期扫描租约过期任务。恢复策略由已经持久化的工具事实决定：

| 已知事实 | 恢复动作 |
| --- | --- |
| 尚无工具副作用 | 安全重放分析 |
| 部分工具步骤已确认成功 | 复用成功结果并补齐剩余步骤 |
| 全部工具成功，Run 尚未完成 | 对账后迁移到完成态 |
| 外部动作失败或结果不确定 | 进入 `manual_takeover`，由恢复 API 审计结案 |

`retry_analysis` 仅适用于没有工具执行历史的 Run。整体语义依靠稳定幂等键、持久化事实和人工接管实现故障后的可恢复收敛。

### 已验证边界

入口专项回归覆盖：

- 同一事件 20 次顺序重复提交；
- 20 线程并发提交；
- 20 个独立 SQLite 连接竞争；
- 过滤结果复用与来源/摄像头隔离；
- 同 Key 异 JSON、异图片载荷冲突；
- 无幂等键兼容路径。

多进程专项测试让两个独立操作系统进程竞争同一 Run，并在三个边界终止 Worker：

1. Run 已创建、分析尚未开始；
2. 副作用已发生、结果尚未落库；
3. 工具结果已落库、Run 尚未完成。

测试验证唯一执行权、陈旧写入拒绝、成功步骤复用和不确定结果人工接管。当前部署形态为单机共享 SQLite，并支持同主机多进程 Worker 的租约与持久恢复。
