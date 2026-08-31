# SOP Retrieval Benchmark

Catalog: `2026.08.1`

| Metric | Value |
|---|---:|
| cases | 8 |
| passed | 8 |
| retrieval_hit_at_1_pct | 100.0 |
| retrieval_hit_at_k_pct | 100.0 |
| mean_reciprocal_rank | 1.0 |
| no_evidence_refusal_accuracy_pct | 100.0 |
| citation_traceability_pct | 100.0 |

| Case | Status | Expected | Actual |
|---|---|---|---|
| helmet_exact | PASS | PPE-001#4.2-helmet@1.2 | PPE-001#4.2-helmet@1.2 |
| helmet_paraphrase | PASS | PPE-001#4.2-helmet@1.2 | PPE-001#4.2-helmet@1.2 |
| vest_exact | PASS | PPE-001#4.3-vest@1.2 | PPE-001#4.3-vest@1.2 |
| person_vehicle | PASS | TRAFFIC-002#5.1-separation@2.0 | TRAFFIC-002#5.1-separation@2.0 |
| fire | PASS | FIRE-003#6.1-initial-response@1.1 | FIRE-003#6.1-initial-response@1.1 |
| vehicle_only | PASS | TRAFFIC-002#3.4-vehicle-monitoring@2.0 | TRAFFIC-002#3.4-vehicle-monitoring@2.0 |
| unknown_chemical | PASS | REFUSE | REFUSE |
| unknown_electrical | PASS | REFUSE | REFUSE |

Deterministic retrieval/citation/refusal only; does not measure LLM quality.
