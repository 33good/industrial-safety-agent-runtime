# Runtime Observability & Concurrency Benchmark

- Cases: 6/6 passed (100.0%)
- Observed control-plane throughput: 38.09 ops/s
- Unique write P50/P95: 35.307/1107.776 ms
- Duplicate ingress P50/P95: 0.511/103.08 ms

> Measures concurrent SQLite control-plane writes, ingress uniqueness and durable metrics aggregation. It does not measure Qwen, detector or network-tool throughput.

| Case | Result | Detail |
|---|---:|---|
| concurrent_unique_runs_are_not_lost | PASS | created=32/32 |
| concurrent_duplicate_ingress_has_one_owner | PASS | created=1 run_ids=1 |
| durable_run_projection_is_consistent | PASS | runs=33 statuses={'analyzing': 1, 'succeeded': 32} |
| tool_retry_metrics_match_durable_rows | PASS | {'execution_count': 8, 'status_counts': {'failed': 2, 'succeeded': 6}, 'success_rate_pct': 75.0, 'total_attempts': 10, 'retry_attempts': 2, 'retried_execution_count': 2, 'by_action': {'database.store': {'execution_count': 8, 'status_counts': {'failed': 2, 'succeeded': 6}, 'success_rate_pct': 75.0, 'retry_attempts': 2, 'latency_ms': {'count': 8, 'min': 14.056, 'mean': 15.283, 'p50': 14.957, 'p95': 16.771, 'max': 17.274}}}} |
| stage_percentiles_are_well_formed | PASS | end_to_end={'count': 32, 'min': 20.46, 'mean': 258.054, 'p50': 25.706, 'p95': 1099.945, 'max': 1228.381} |
| capacity_scope_is_explicit | PASS | {'max_inflight': 2, 'inflight': 0, 'available': 2, 'rejected_total': 0} |
