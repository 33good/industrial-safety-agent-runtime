# Multimodal Agent Benchmark

Status: `completed`

Model: `qwen2.5vl:7b`
Prompt: `safety-v2.2-grounded-sop`
SOP catalog: `2026.08-demo.1`

| Variant | Valid JSON | Risk accuracy | Non-downgrade | Candidate actions | Guardrail plan | Citation coverage | Citation precision | Refusal accuracy | Mean latency |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| no_rag | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 100.0% | 1501.9 ms |
| grounded_sop_rag | 100.0% | 100.0% | 100.0% | 80.0% | 100.0% | 100.0% | 100.0% | 100.0% | 1862.5 ms |

## no_rag

| Case | Result | Risk | RAG | Citations | Latency |
|---|---|---|---|---|---:|
| ppe_helmet | PASS | B / B | disabled | - | 1509.5 ms |
| person_vehicle | PASS | A / A | disabled | - | 1648.5 ms |
| fire | PASS | A / A | disabled | - | 1497.1 ms |
| vehicle_only | PASS | C / C | disabled | - | 1108.1 ms |
| unknown_sop | PASS | A / A | disabled | - | 1746.2 ms |

## grounded_sop_rag

| Case | Result | Risk | RAG | Citations | Latency |
|---|---|---|---|---|---:|
| ppe_helmet | PASS | B / B | grounded | PPE-001#4.2-helmet@1.2 | 1900.0 ms |
| person_vehicle | PASS | A / A | grounded | TRAFFIC-002#5.1-separation@2.0 | 1807.4 ms |
| fire | PASS | A / A | grounded | FIRE-003#6.1-initial-response@1.1 | 2003.3 ms |
| vehicle_only | PASS | C / C | grounded | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | 1537.2 ms |
| unknown_sop | PASS | A / A | no_evidence | - | 2064.4 ms |

Measures local model structure, policy-facing risk output, grounded citations, refusal and latency. Generated replay images are not a substitute for a real-world vision accuracy dataset.
