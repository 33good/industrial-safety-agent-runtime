# 本地视觉模型

将 PPE 检测模型放在本目录，并通过 `VISION_MODEL_PATH` 指向模型文件。仓库当前使用：

```text
models/yolo26n_safety6_demo_best.pt
```

模型权重不会提交到 Git。干净克隆默认设置 `VISION_ENABLED=0`，可先运行 Agent 演示与离线评测；准备好兼容权重后再在本地 `.env` 中启用视觉链路。

模型至少需要包含 `person` 类。要启用现有安全帽、反光背心、车辆和火焰规则，类别名称应包含下列任一别名：

```text
person / worker
helmet / hardhat / safety_helmet
vest / safety_vest / reflective_vest
vehicle / forklift / car / truck
fire / flame
```

不同模型的类别名可通过环境变量 `VISION_CLASS_MAP` 覆盖，例如：

```text
VISION_CLASS_MAP={"hard_hat":1,"hi_vis":2}
```

通用 COCO 模型不具备安全帽和反光背心识别能力，不能替代 PPE 专用权重。

默认情况下，系统会校验模型同时具备 `person`、`helmet` 和 `vest` 三类；缺少任一类别时，视觉服务会以降级状态启动而不会生成错误 PPE 告警。仅做人员/车辆实验时可设置：

```text
VISION_REQUIRE_PPE=0
```
