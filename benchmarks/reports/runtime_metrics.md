# Runtime Observability & Concurrency Benchmark

- Cases: 6/6 passed (100.0%)
- Observed control-plane throughput: 39.74 ops/s
- Unique write P50/P95: 32.202/337.673 ms
- Duplicate ingress P50/P95: 0.452/46.106 ms

> Measures concurrent SQLite control-plane writes, ingress uniqueness and durable metrics aggregation. It does not measure Qwen, detector or network-tool throughput.

| Case | Result | Detail |
|---|---:|---|
| concurrent_unique_runs_are_not_lost | PASS | created=32/32 |
| concurrent_duplicate_ingress_has_one_owner | PASS | created=1 run_ids=1 |
| durable_run_projection_is_consistent | PASS | runs=33 statuses={'analyzing': 1, 'succeeded': 32} |
| tool_retry_metrics_match_durable_rows | PASS | {'execution_count': 8, 'status_counts': {'failed': 2, 'succeeded': 6}, 'success_rate_pct': 75.0, 'total_attempts': 10, 'retry_attempts': 2, 'retried_execution_count': 2, 'by_action': {'database.store': {'execution_count': 8, 'status_counts': {'failed': 2, 'succeeded': 6}, 'success_rate_pct': 75.0, 'retry_attempts': 2, 'latency_ms': {'count': 8, 'min': 13.983, 'mean': 14.217, 'p50': 14.027, 'p95': 14.822, 'max': 14.86}}}} |
| stage_percentiles_are_well_formed | PASS | end_to_end={'count': 32, 'min': 21.216, 'mean': 118.673, 'p50': 24.002, 'p95': 329.119, 'max': 1168.976} |
| capacity_scope_is_explicit | PASS | {'max_inflight': 2, 'inflight': 0, 'available': 2, 'rejected_total': 0} |
