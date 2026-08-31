# Context Engineering Benchmark

- Cases: 10/10 passed (100.0%)

| Case | Result | Detail |
|---|---:|---|
| critical_rule_evidence_is_never_dropped | PASS | required=4 selected=4 |
| all_rule_detections_survive_legacy_count_boundaries | PASS | detections=12 overflow=764 |
| untrusted_detection_text_is_bounded_and_audited | PASS | [{'item_id': 'detection:0:5fad60156f6c', 'field': 'detail', 'reason': 'untrusted_text_length_limit', 'original_sha256': '5783df8e9629fad09140c672e6541b5be1b1c37565438e963f6b020743bcaf8c', 'retained_characters': 240}, {'item_id': 'detection:0:5fad60156f6c', 'field': 'confidence', 'reason': 'non_numeric_value_removed', 'original_sha256': '5c0dad8f8b02047499636b2c0dcffcee58a7b3c5946e2ade85c781d9ad6f592b'}, {'item_id': 'detection:0:5fad60156f6c', 'field': 'bbox', 'reason': 'non_coordinate_fields_removed', 'original_sha256': '98479f0271efc629ae3567041206f8c43a58875d9914d9bf21a44ea12df6b3aa'}] |
| optional_context_respects_token_budget | PASS | tokens=224/256 dropped=4 |
| sop_is_ranked_before_historical_memory | PASS | [('sop_citation', 80), ('memory_event', 50)] |
| duplicate_context_is_removed_with_reason | PASS | dropped=2 |
| manifest_does_not_copy_raw_context | PASS | manifest stores hashes and provenance, not raw evidence text |
| context_hash_is_deterministic | PASS | 5f5db1dce979073273b1340001c80ca330216b02c0029c41bcf7c1c3a8e26db3 |
| citation_scope_matches_injected_context | PASS | ['PPE-001#4.2-helmet@1.2'] |
| runtime_fallback_records_explicit_skip | PASS | analysis_capacity_exhausted |
