import unittest

from benchmarks.run_runtime_metrics import build_report
from services.runtime_metrics import summarize_distribution


class RuntimeMetricsTests(unittest.TestCase):
    def test_distribution_uses_interpolated_percentiles(self):
        summary = summarize_distribution([0, 10, 20, 30, None])
        self.assertEqual(summary["count"], 4)
        self.assertEqual(summary["p50"], 15.0)
        self.assertEqual(summary["p95"], 28.5)
        self.assertEqual(summary["mean"], 15.0)

    def test_runtime_observability_benchmark_has_no_failed_cases(self):
        report = build_report()
        self.assertEqual(report["summary"]["failed"], 0, report["results"])
        self.assertIn("does not measure Qwen", report["scope"])
        self.assertEqual(
            report["metrics_snapshot"]["scope"]["source"],
            "durable_sqlite_projection",
        )


if __name__ == "__main__":
    unittest.main()
