# safety-v2.7 Evidence Conflict Validation

Status: completed

Model: qwen2.5vl:7b via local Ollama 0.32.5
Prompt: safety-v2.7-separated-evidence
Cases / trials: 8 conflict + 8 control / 48 scored trials
Images: mixed offline synthetic and local replay fixtures; no live camera

## Result

Classification: C - low conflict detection, low system false-positive rate.

| Layer | Conflict Recall | Control False Positive | Precision |
|---|---:|---:|---:|
| Qwen declaration | 37.5% | 12.5% | 75.0% |
| EvidenceConsistency | 37.5% | 0.0% | 100.0% |
| Conflict review policy | 37.5% | 0.0% | 100.0% |

v2.7 observed 37.5% explicit/system conflict recall versus old v2.6 0% conflict-uncertainty acknowledgement. The schema changed, so this is not a strict apples-to-apples accuracy comparison.

## Core metrics

- Paired conflict detail completeness: 100.0%; strict all-field completeness: 0.0%.
- Normal autonomy preservation: 100.0%; unnecessary conflict review: 0.0%.
- Conflict risk non-downgrade: 100.0%.
- Three-run consistency, evidence path / full decision: 87.5% / 81.25%.
- Structured validity / repair rate: 100.0% / 0.0%.
- Latency P50/P95: 5890.9 / 8758.1 ms.
- Decision-trace completeness: 100.0% conflict / 100.0% control.

## Conflict cases

| Case | Model | System | Review | Relations |
|---|---:|---:|---:|---|
| conflict_fire_image_vehicle_json | 0/3 | 0/3 | 0/3 | insufficient, insufficient, consistent |
| conflict_vehicle_image_fire_json | 3/3 | 3/3 | 3/3 | conflict, conflict, conflict |
| conflict_traffic_image_vehicle_json | 3/3 | 3/3 | 3/3 | conflict, conflict, conflict |
| conflict_ppe_image_vehicle_json | 0/3 | 0/3 | 0/3 | insufficient, insufficient, insufficient |
| conflict_vehicle_image_ppe_json | 0/3 | 0/3 | 0/3 | insufficient, insufficient, insufficient |
| conflict_fire_image_ppe_json | 0/3 | 0/3 | 0/3 | insufficient, insufficient, consistent |
| conflict_blank_vehicle_json | 3/3 | 3/3 | 3/3 | conflict, conflict, conflict |
| conflict_ppe_image_fire_json | 0/3 | 0/3 | 0/3 | insufficient, insufficient, insufficient |

## Failed-case attribution

- conflict_fire_image_vehicle_json - 0/3 conflict detections: Qwen did not recover fire evidence and returned insufficient/consistent with mostly empty separated visual observations.
- conflict_ppe_image_vehicle_json - 0/3 conflict detections: Qwen emitted generic placeholder observations, so the PPE-versus-vehicle mismatch was not represented.
- conflict_vehicle_image_ppe_json - 0/3 conflict detections: Qwen followed structured PPE evidence but did not articulate the vehicle-image mismatch.
- conflict_fire_image_ppe_json - 0/3 and unstable relation: Qwen returned insufficient, insufficient, then consistent; one run saw FIRE text but still did not declare conflict.
- conflict_ppe_image_fire_json - 0/3 conflict detections: Qwen incorrectly declared image_only despite JSON; EvidenceConsistency returned insufficient and the A rule baseline preserved risk.
- degraded_traffic_json_only - Qwen false conflict in 3/3 controls: Qwen treated missing video and JSON as conflict; EvidenceConsistency converted all to detections_only and prevented review false positives.
- degraded_ppe_image_only - No conflict false positive, final C instead of grader B in 3/3: Image-only VQA risk-recognition miss, not a conflict-governance error.
- conflict_vehicle_image_fire_json - 3/3 detected; candidate risk A/B/A: Conflict stable but candidate risk unstable; deterministic A baseline restored final A.
- conflict_traffic_image_vehicle_json - 3/3 detected, final B versus grader A: Mismatch found but scene severity underestimated; B stayed above structured C baseline.
- conflict_blank_vehicle_json - 3/3 detected, final B versus grader C: Blank-frame mismatch caused conservative upgrade and reduced risk accuracy.

## Safety contract

- Structured deterministic risk baseline held in 24/24 conflict trials.
- Missing-modality controls caused zero system conflict flags and zero conflict-review triggers.
- Model benchmark executed no tools and sent zero actuator commands.
- Frozen checks passed 7/7 evidence tests and 42/42 stability tests.
- Review closure calls internal Actuator.review with commands=[]; zero external commands does not mean zero method calls.
- Trace coverage is decision trace, not persisted end-to-end Runtime trace.

## Decision

Do not run the full 40-case benchmark yet. Recall is 37.5%. No Prompt or code was changed.

The JSON report contains all 48 trial records and bound hashes.
