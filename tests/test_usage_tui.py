import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from collector import scan
from tests.test_collector import assistant, write_jsonl
from usage_tui import (
    allocate_integer,
    build_snapshot,
    canonical_model,
    render,
    render_audit,
)


class UsageTuiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self.db = self.root / "usage.db"
        self.stats = self.root / "stats-cache.json"

    def tearDown(self):
        self.temporary.cleanup()

    def build_hybrid_snapshot(self):
        write_jsonl(
            self.projects / "session.jsonl",
            [
                assistant(
                    "msg-1",
                    "2026-01-01T10:00:00Z",
                    input_tokens=5,
                    output_tokens=5,
                    cache_read=90,
                )
            ],
        )
        scan(projects_dir=self.projects, db_path=self.db, verbose=False)
        self.stats.write_text(
            json.dumps(
                {
                    "firstSessionDate": "2026-01-01T10:00:00Z",
                    "lastComputedDate": "2026-01-02",
                    "modelUsage": {
                        "claude-opus-4-6": {
                            "inputTokens": 10,
                            "outputTokens": 10,
                            "cacheReadInputTokens": 280,
                            "cacheCreationInputTokens": 0,
                        }
                    },
                    "dailyModelTokens": [
                        {
                            "date": "2026-01-01",
                            "tokensByModel": {"claude-opus-4-6": 30},
                        },
                        {
                            "date": "2026-01-02",
                            "tokensByModel": {"claude-opus-4-6": 30},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return build_snapshot(self.db, self.stats)

    def test_snapshot_separates_exact_and_aggregate_allocation(self):
        snapshot = self.build_hybrid_snapshot()

        self.assertEqual(snapshot.lifetime, 300)
        self.assertEqual(snapshot.exact_tokens, 100)
        self.assertEqual(snapshot.reconstructed_tokens, 200)
        self.assertAlmostEqual(snapshot.exact_percent, 100.0 / 3.0)
        self.assertEqual(snapshot.daily["2026-01-01"], 180)
        self.assertEqual(snapshot.daily["2026-01-02"], 120)
        self.assertEqual(snapshot.raw_date_range, ("2026-01-01", "2026-01-01"))
        self.assertEqual(snapshot.stats_date_range, ("2026-01-01", "2026-01-02"))

    def test_render_discloses_approximation(self):
        snapshot = self.build_hybrid_snapshot()

        output = render(
            snapshot,
            mode="weekly",
            width=120,
            height=30,
            colors=False,
            today=date(2026, 1, 3),
        )

        self.assertIn("Peak ≈", output)
        self.assertIn("33.3% exact turns", output)
        self.assertIn("66.7% aggregate allocation", output)

    def test_audit_reports_per_model_provenance(self):
        snapshot = self.build_hybrid_snapshot()

        output = render_audit(snapshot, self.db, self.stats)

        self.assertIn("Exact turn tokens:", output)
        self.assertIn("claude-opus-4-6", output)
        self.assertIn("This is token activity, not subscription-quota", output)

    def test_raw_only_snapshot_is_fully_exact(self):
        write_jsonl(
            self.projects / "session.jsonl",
            [assistant("msg-1", "2026-02-01T10:00:00Z", input_tokens=42)],
        )
        scan(projects_dir=self.projects, db_path=self.db, verbose=False)

        snapshot = build_snapshot(self.db, self.root / "missing-stats.json")

        self.assertEqual(snapshot.lifetime, 42)
        self.assertEqual(snapshot.exact_tokens, 42)
        self.assertEqual(snapshot.reconstructed_tokens, 0)
        self.assertEqual(snapshot.exact_percent, 100.0)

    def test_model_versions_are_canonicalized(self):
        self.assertEqual(canonical_model("claude-opus-4-5-20251101"), "claude-opus-4-5")
        self.assertEqual(canonical_model("claude-opus-4-6"), "claude-opus-4-6")

    def test_integer_allocation_preserves_total(self):
        result = allocate_integer(11, {"a": 1, "b": 2})
        self.assertEqual(sum(result.values()), 11)
        self.assertGreater(result["b"], result["a"])


if __name__ == "__main__":
    unittest.main()
