# Multimodal Agent Benchmark

Status: `completed`

Suite: `four_mode_ablation`
Model: `qwen2.5vl:7b`
Prompt: `safety-v2.3-conflict-aware`
SOP catalog: `2026.08-demo.1`
Cases / repeats: `16 / 3`

| Variant | Valid JSON | Model / final risk accuracy | Final non-downgrade | Candidate actions | Guardrail plan | Guardrail correction | Trace | Consistency | Citation coverage | Refusal | P50 / P95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| grounded_sop_rag | 100.0% | 54.17% / 60.42% | 64.58% | 100.0% | 100.0% | 100.0% | 100.0% | 81.25% | 94.44% | 100.0% | 1553.0 / 1871.9 ms |

## grounded_sop_rag by category

| Category | Executions | Pass | Model / final risk | Final safety | Guardrail |
|---|---:|---:|---:|---:|---:|
| normal | 48 | 56.25% | 54.17% / 60.42% | 64.58% | 100.0% |

## grounded_sop_rag by input mode

| Input mode | Executions | Pass | Model / final risk | Final safety | Guardrail |
|---|---:|---:|---:|---:|---:|
| conflict | 12 | 16.67% | 8.33% / 33.33% | 33.33% | 100.0% |
| image_json | 12 | 100.0% | 100.0% / 100.0% | 100.0% | 100.0% |
| image_only | 12 | 8.33% | 8.33% / 8.33% | 25.0% | 100.0% |
| json_only | 12 | 100.0% | 100.0% / 100.0% | 100.0% | 100.0% |

| Case | Mode | Round | Result | Candidate / final / expected | RAG | Citations | Latency |
|---|---|---:|---|---|---|---|---:|
| normal_helmet | conflict | 1 | FAIL | C / C / B | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1634.3 ms |
| normal_helmet | conflict | 2 | FAIL | C / C / B | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1556.6 ms |
| normal_helmet | conflict | 3 | FAIL | C / C / B | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1459.6 ms |
| normal_helmet | image_json | 1 | PASS | B / B / B | grounded | PPE-001#4.2-helmet@1.2 | 1624.2 ms |
| normal_helmet | image_json | 2 | PASS | B / B / B | grounded | PPE-001#4.2-helmet@1.2 | 1387.7 ms |
| normal_helmet | image_json | 3 | PASS | B / B / B | grounded | PPE-001#4.2-helmet@1.2 | 1606.0 ms |
| normal_helmet | image_only | 1 | FAIL | C / C / B | no_evidence | - | 1470.2 ms |
| normal_helmet | image_only | 2 | FAIL | C / C / B | no_evidence | - | 1111.6 ms |
| normal_helmet | image_only | 3 | FAIL | C / C / B | no_evidence | - | 1403.5 ms |
| normal_helmet | json_only | 1 | PASS | B / B / B | grounded | PPE-001#4.2-helmet@1.2 | 1307.4 ms |
| normal_helmet | json_only | 2 | PASS | B / B / B | grounded | PPE-001#4.2-helmet@1.2 | 1491.6 ms |
| normal_helmet | json_only | 3 | PASS | B / B / B | grounded | PPE-001#4.2-helmet@1.2 | 1253.9 ms |
| normal_vehicle | conflict | 1 | PASS | C / A / A | grounded | FIRE-003#6.1-initial-response@1.1 | 1389.0 ms |
| normal_vehicle | conflict | 2 | FAIL | C / A / A | citation_missing | - | 1544.0 ms |
| normal_vehicle | conflict | 3 | FAIL | C / A / A | citation_missing | - | 1311.6 ms |
| normal_vehicle | image_json | 1 | PASS | C / C / C | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1653.5 ms |
| normal_vehicle | image_json | 2 | PASS | C / C / C | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1591.3 ms |
| normal_vehicle | image_json | 3 | PASS | C / C / C | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1585.4 ms |
| normal_vehicle | image_only | 1 | FAIL | B / B / C | no_evidence | - | 1527.4 ms |
| normal_vehicle | image_only | 2 | PASS | C / C / C | no_evidence | - | 1661.8 ms |
| normal_vehicle | image_only | 3 | FAIL | B / B / C | no_evidence | - | 1514.4 ms |
| normal_vehicle | json_only | 1 | PASS | C / C / C | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1740.2 ms |
| normal_vehicle | json_only | 2 | PASS | C / C / C | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1703.5 ms |
| normal_vehicle | json_only | 3 | PASS | C / C / C | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1347.8 ms |
| normal_fire | conflict | 1 | FAIL | C / C / A | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1382.1 ms |
| normal_fire | conflict | 2 | FAIL | C / C / A | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1357.5 ms |
| normal_fire | conflict | 3 | FAIL | C / C / A | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1340.9 ms |
| normal_fire | image_json | 1 | PASS | A / A / A | grounded | FIRE-003#6.1-initial-response@1.1 | 1426.7 ms |
| normal_fire | image_json | 2 | PASS | A / A / A | grounded | FIRE-003#6.1-initial-response@1.1 | 1538.4 ms |
| normal_fire | image_json | 3 | PASS | A / A / A | grounded | FIRE-003#6.1-initial-response@1.1 | 1722.7 ms |
| normal_fire | image_only | 1 | FAIL | C / C / A | no_evidence | - | 1311.0 ms |
| normal_fire | image_only | 2 | FAIL | C / C / A | no_evidence | - | 1312.0 ms |
| normal_fire | image_only | 3 | FAIL | C / C / A | no_evidence | - | 1324.2 ms |
| normal_fire | json_only | 1 | PASS | A / A / A | grounded | FIRE-003#6.1-initial-response@1.1 | 1553.0 ms |
| normal_fire | json_only | 2 | PASS | A / A / A | grounded | FIRE-003#6.1-initial-response@1.1 | 1513.0 ms |
| normal_fire | json_only | 3 | PASS | A / A / A | grounded | FIRE-003#6.1-initial-response@1.1 | 1433.8 ms |
| normal_person_vehicle | conflict | 1 | PASS | A / A / A | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1871.9 ms |
| normal_person_vehicle | conflict | 2 | FAIL | B / B / A | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1654.3 ms |
| normal_person_vehicle | conflict | 3 | FAIL | B / B / A | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1639.0 ms |
| normal_person_vehicle | image_json | 1 | PASS | A / A / A | grounded | TRAFFIC-002#5.1-separation@2.0 | 1750.5 ms |
| normal_person_vehicle | image_json | 2 | PASS | A / A / A | grounded | TRAFFIC-002#5.1-separation@2.0 | 1734.1 ms |
| normal_person_vehicle | image_json | 3 | PASS | A / A / A | grounded | TRAFFIC-002#5.1-separation@2.0 | 1740.9 ms |
| normal_person_vehicle | image_only | 1 | FAIL | B / B / A | no_evidence | - | 1737.7 ms |
| normal_person_vehicle | image_only | 2 | FAIL | B / B / A | no_evidence | - | 1618.6 ms |
| normal_person_vehicle | image_only | 3 | FAIL | B / B / A | no_evidence | - | 1611.1 ms |
| normal_person_vehicle | json_only | 1 | PASS | A / A / A | grounded | TRAFFIC-002#5.1-separation@2.0 | 1813.8 ms |
| normal_person_vehicle | json_only | 2 | PASS | A / A / A | grounded | TRAFFIC-002#5.1-separation@2.0 | 2426.6 ms |
| normal_person_vehicle | json_only | 3 | PASS | A / A / A | grounded | TRAFFIC-002#5.1-separation@2.0 | 2344.6 ms |

Measures model structure, risk decisions, guarded plans, grounded citations, refusal, repeat consistency and decision-stage trace completeness. Generated replay images do not measure real-world detector accuracy; full execution Trace is covered separately.
