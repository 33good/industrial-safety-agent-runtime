# Agent Trace Integrity Benchmark

- Cases: 15/15 passed (100.0%)

| Case | Result | Detail |
|---|---:|---|
| complete_trace_is_valid | PASS | [] |
| filtered_ingress_trace_is_valid | PASS | [] |
| single_repaired_trace_is_valid | PASS | [] |
| missing_evidence_is_rejected | PASS | ['missing_evidence_id'] |
| snapshot_identity_mismatch_is_rejected | PASS | ['snapshot_trace_id_mismatch'] |
| fabricated_citation_is_rejected | PASS | ['selected_citation_not_retrieved', 'selected_citation_not_in_model_context'] |
| missing_tool_link_is_rejected | PASS | ['final_plan_missing_tool_execution', 'successful_run_has_unconfirmed_tool'] |
| missing_context_manifest_is_rejected | PASS | ['missing_context_schema_version', 'missing_context_builder_version', 'missing_context_hash', 'missing_model_input_hash', 'invalid_context_status', 'selected_citation_not_in_model_context', 'grounded_citation_not_in_context'] |
| missing_model_input_hash_is_rejected | PASS | ['missing_model_input_hash'] |
| missing_repair_trace_is_rejected | PASS | ['missing_repair_schema_version', 'missing_repair_policy_version', 'missing_repair_status', 'missing_repair_attempts', 'invalid_repair_attempt_budget', 'invalid_repair_status'] |
| missing_run_timing_is_rejected | PASS | ['missing_timing_schema_version', 'invalid_timing_schema_version', 'missing_end_to_end_timing', 'timing_transition_count_mismatch'] |
| second_repair_attempt_is_rejected | PASS | ['repair_attempt_budget_exceeded', 'repair_attempt_missing_prompt_version', 'repair_attempt_missing_trigger_code', 'repair_attempt_missing_input_sha256', 'repair_attempt_missing_original_output_sha256', 'repair_attempt_missing_status'] |
| malformed_failure_attribution_is_rejected | PASS | ['failure_attribution_missing_attribution_id', 'failure_attribution_missing_stage', 'failure_attribution_missing_code', 'failure_attribution_missing_resolution', 'failure_attribution_missing_status'] |
| citation_not_in_injected_context_is_rejected | PASS | ['selected_citation_not_in_model_context', 'grounded_citation_not_in_context'] |
| fabricated_final_grounding_is_rejected | PASS | ['grounded_citation_not_retrieved', 'grounded_citation_not_in_context'] |
