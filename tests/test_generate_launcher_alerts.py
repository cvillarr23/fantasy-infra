"""Launcher run_log alerts are generated from the catalog and committed in sync with it."""

import importlib.util
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("generate_launcher_alerts", ROOT / "scripts/generate-launcher-alerts.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


def rule_sql(rendered):
    (body,) = yaml.safe_load(rendered)["data"].values()
    return yaml.safe_load(body)["groups"][0]["rules"][0]["data"][0]["model"]["rawSql"]


class GenerateLauncherAlertsTest(unittest.TestCase):
    def test_jobs_split_by_alert_level_with_default(self):
        catalog = {
            "defaults": {"alert": "warning"},
            "jobs": {"a": {"alert": "critical"}, "b": {}, "c": {"alert": "none"}, "d": {"alert": "warning"}},
        }
        self.assertEqual(generator.jobs_at_level(catalog, "critical"), ["a"])
        self.assertEqual(generator.jobs_at_level(catalog, "warning"), ["b", "d"])

    def test_empty_catalog_lists_no_jobs(self):
        self.assertEqual(generator.jobs_at_level({"jobs": {}}, "critical"), [])
        self.assertEqual(generator.jobs_at_level({}, "warning"), [])

    def test_job_names_are_quoted_safely(self):
        self.assertEqual(generator._job_array(["it's"]), "ARRAY['it''s']::text[]")

    def test_rendered_sql_names_the_level_jobs(self):
        sql = rule_sql(generator.render(ROOT / "tests/fixtures/job-catalog.yaml", "critical"))
        self.assertIn("'lineup-optimizer'", sql)
        self.assertNotIn("'sync-league'", sql)

    def test_committed_rules_match_the_catalog(self):
        for level in generator.LEVELS:
            with self.subTest(level=level):
                self.assertEqual(
                    generator.output_path(level).read_text(), generator.render(generator.CATALOG, level)
                )


if __name__ == "__main__":
    unittest.main()
