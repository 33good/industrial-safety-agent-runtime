# 评测与结果

[返回项目首页](../README.md) · [启动与接入](getting-started.md) · [Agent Runtime 设计](agent-runtime.md)

项目将检测后的稳定事件作为 Agent 评测起点，分别验证模型候选、系统裁决和最终执行，并保留三层独立指标。确定性报告记录各自的 Benchmark 与版本信息；多模态长评测进一步绑定数据集、Prompt、Context、SOP 目录和策略指纹。

## 结果总览

| 验证对象 | 可复核结果 | 适用范围 |
| --- | --- | --- |
| 当前 `safety-v2.8` Runtime | [130/130 项单元与本地集成测试通过](../benchmarks/reports/verification_summary.json)；7 类确定性离线 Benchmark 全部通过 | 默认无需摄像头、GPU、Ollama 或真实外部设备 |
| 可靠执行与故障恢复 | [6/6 项故障注入基准通过](../benchmarks/reports/runtime_faults.md)；专项测试覆盖双进程竞争与 3 类真实强杀边界 | 有限重试、重复副作用抑制、陈旧写入拒绝与不确定结果人工接管 |
| `safety-v2.6` 固定多模态基线 | [40 个合成困难场景](../benchmarks/reports/multimodal_latest.md)；系统级严格通过 34/40，候选动作 33/40 合规，经 Guardrail 后最终工具计划 40/40 合法 | 固定数据、固定 Prompt 与本地 Qwen 单轮回放 |
| Trace 契约 | [15/15 个完整链路与篡改反例通过](../benchmarks/reports/trace_integrity.md)；v2.6 决策 Trace 40/40 完整 | Run、证据、决策、工具结果与终态的跨表一致性 |
| SOP 检索与拒答 | [8/8 个检索用例通过](../benchmarks/reports/sop_retrieval.md)，其中无证据样例 2/2 正确拒答 | 版本化规程目录、引用绑定与无证据拒答 |

## 一键质量门禁

```powershell
python -m pip install -r requirements-ci.txt
python -B verify.py
```

默认门禁运行 Policy、Runtime 故障恢复、Context、受控修复、Runtime 指标、Trace、SOP 检索，以及 130 项单元与本地集成测试。执行过程仅使用临时 SQLite、回环端口和本地子进程。

GitHub Actions 使用 `requirements-ci.txt` 安装最小依赖并执行相同的 `verify.py`，随后上传评测报告。

## Agent Benchmark

### 确定性离线评测

```powershell
python -m benchmarks.run_agent_benchmark
python -m benchmarks.run_runtime_faults
python -m benchmarks.run_context_benchmark
python -m benchmarks.run_repair_benchmark
python -m benchmarks.run_runtime_metrics
python -m benchmarks.run_trace_benchmark
python -m benchmarks.run_sop_benchmark
python -m benchmarks.run_multimodal_benchmark --validate-only
```

### 本地 Qwen 多模态评测

```powershell
python -m benchmarks.run_multimodal_benchmark --timeout 90 --require-model
python -m benchmarks.run_multimodal_benchmark --ablation --repeats 3 --rag-only --require-model --timeout 90 --output benchmarks/reports/multimodal_ablation_latest.json
```

只有显式运行本地多模态命令或 `.\verify.bat --live` 才会调用本地 Qwen；是否占用 GPU 取决于本机 Ollama 推理配置。

## 评测矩阵

| 评测 | 重点契约 | 报告 |
| --- | --- | --- |
| Policy | 结构化输出、风险不可降级、必选动作补齐、越权动作拒绝、模型异常兜底 | [Markdown](../benchmarks/reports/latest.md) · [JSON](../benchmarks/reports/latest.json) |
| Runtime Faults | 工具重试、结果复用、崩溃恢复与人工接管 | [Markdown](../benchmarks/reports/runtime_faults.md) · [JSON](../benchmarks/reports/runtime_faults.json) |
| Context Engineering | 关键证据保留、预算截断、优先级、去重、引用边界与降级清单 | [Markdown](../benchmarks/reports/context_engineering.md) · [JSON](../benchmarks/reports/context_engineering.json) |
| Bounded Repair | 单次修复预算、失败兜底、降级/越权拦截与不确定副作用收敛 | [Markdown](../benchmarks/reports/bounded_repair.md) · [JSON](../benchmarks/reports/bounded_repair.json) |
| Runtime Metrics | 并发控制面、入口唯一所有者、持久指标与分位数计算 | [Markdown](../benchmarks/reports/runtime_metrics.md) · [JSON](../benchmarks/reports/runtime_metrics.json) |
| Trace Integrity | 完整处置链、过滤终态和跨表篡改反例 | [Markdown](../benchmarks/reports/trace_integrity.md) · [JSON](../benchmarks/reports/trace_integrity.json) |
| SOP Retrieval | 规程召回、引用绑定、版本信息与无证据拒答 | [Markdown](../benchmarks/reports/sop_retrieval.md) · [JSON](../benchmarks/reports/sop_retrieval.json) |
| Multimodal | 本地 Qwen 候选、系统裁决、最终执行与消融 | [基线 Markdown](../benchmarks/reports/multimodal_latest.md) · [基线 JSON](../benchmarks/reports/multimodal_latest.json) · [消融 Markdown](../benchmarks/reports/multimodal_ablation_latest.md) · [消融 JSON](../benchmarks/reports/multimodal_ablation_latest.json) |

## 固定多模态基线

困难集包含 40 个合成回放场景，覆盖五类问题：

1. 正常风险判断；
2. 遮挡、模糊和证据缺失；
3. 图像与检测 JSON 冲突；
4. 越权动作与非法结构化输出；
5. SOP 无证据、错误引用与版本冲突。

在 `safety-v2.6-bounded-generation` 的固定本地 Qwen2.5-VL + SOP RAG 回放中：

- 结构化输出有效率为 100%；
- 模型候选风险准确率为 75%，Guardrail 后最终风险准确率为 85%；
- 模型候选动作合规率为 82.5%；
- 最终工具计划合法率、Guardrail 修正率和决策 Trace 完整率均为 100%。

逐例输出与失败项保留在报告中，便于区分模型判断问题、证据一致性问题和系统策略问题。

## 有界补证评测契约

当前 `safety-v2.8-bounded-temporal-evidence` 将补证约束为一次有界决策：

- 首轮 Qwen 只能直接决策、请求相邻帧或转人工；
- 每次执行尝试最多进行一次只读补证和一次最终重判；
- 第二轮结束后统一进入既有 Guardrail 与 HITL；
- 专项测试覆盖正常决策、证据冲突、补证不可用、重判失败、风险下调和人工复核路径。

评测器分别记录模型候选、EvidenceConsistency 裁决和最终执行结果，使补证能力与 Guardrail 兜底效果可以独立复核。

## 可恢复长评测

长评测使用 `multimodal-benchmark-checkpoint-v4` 逐例原子落盘。Checkpoint 指纹绑定：

- 模型与生成参数；
- 主 Prompt 和修复 Prompt；
- Context Builder 与最终 SOP grounding 策略；
- SOP 目录、上下文预算、重复次数；
- 数据集摘要。

输入版本发生变化时拒绝续跑。`--resume` 仅复用指纹一致的完成项，`--retry-failed` 用于环境恢复后重测模型失败项。Checkpoint 只保存评测所需的摘要和结构化结果，不保存原始 Prompt、图片或模型全文。

模型 warmup 未产生合法结构化输出时，评分阶段不会启动；正式运行连续两次模型失败后触发熔断并保留断点。

## 结果口径

- 多模态数字来自固定合成回放集，用于版本对比和失败归因，不等同于真实现场准确率。
- 确定性质量门禁验证软件契约；本地 Qwen 评测单独验证模型输出下的系统行为。
- 完整报告按模型候选、系统裁决、最终执行三层记录结果，并保留逐例输出、延迟和版本指纹。
