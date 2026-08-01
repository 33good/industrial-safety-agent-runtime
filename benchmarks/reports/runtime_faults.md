# Agent Runtime Fault-Injection Benchmark

- Cases: 6/6 passed (100.0%)

| Case | Result | Detail |
|---|---:|---|
| transient_retry_recovers | PASS | status=succeeded,attempts=2 |
| duplicate_side_effect_suppressed | PASS | reused=True,handler_calls=2 |
| permanent_failure_not_retried | PASS | status=failed,attempts=1 |
| indeterminate_tool_requires_human | PASS | status=manual_takeover |
| completed_side_effect_reconciled | PASS | status=succeeded |
| vlm_overload_is_bounded | PASS | rejected_total=1 |
