"""Launcher run_log alerts are generated from the catalog and committed in sync with it."""

import importlib.util
from pathlib import Path
import tempfile
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
    def test_jobs_split_by_alert_level_with_defaults(self):
        catalog = {
            "defaults": {"alert": "warning", "max_attempts": "2"},
            "jobs": {
                "a": {"alert": "critical", "max_attempts": "3"},
                "b": {},
                "c": {"alert": "none"},
                "d": {"alert": "warning", "max_attempts": "1"},
            },
        }
        self.assertEqual(generator.jobs_at_level(catalog, "critical"), [("a", 3)])
        self.assertEqual(generator.jobs_at_level(catalog, "warning"), [("b", 2), ("d", 1)])

    def test_empty_catalog_lists_no_jobs(self):
        self.assertEqual(generator.jobs_at_level({"jobs": {}}, "critical"), [])
        self.assertEqual(generator.jobs_at_level({}, "warning"), [])

    def test_null_defaults_and_job_bodies_fall_back(self):
        # BaseLoader reads `defaults:` and a bare `job:` as '' rather than None or a mapping.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.yaml"
            path.write_text("version: 1\ndefaults:\njobs:\n  bare:\n  full: {alert: critical, max_attempts: 3}\n")
            catalog = generator._load_catalog(path)
            self.assertEqual(generator.jobs_at_level(catalog, "warning"), [("bare", 1)])
            self.assertEqual(generator.jobs_at_level(catalog, "critical"), [("full", 3)])
            generator.render(path, "warning")

    def test_yaml_1_1_booleans_stay_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.yaml"
            path.write_text("jobs:\n  on: {alert: critical}\n  no: {}\n")
            catalog = generator._load_catalog(path)
            self.assertEqual(generator.jobs_at_level(catalog, "critical"), [("on", 1)])
            self.assertEqual(generator.jobs_at_level(catalog, "warning"), [("no", 1)])

    def test_job_names_are_quoted_safely(self):
        self.assertIn("ARRAY['it''s']::text[]", generator._job_attempts([("it's", 1)]))

    def test_rendered_sql_names_the_level_jobs(self):
        sql = rule_sql(generator.render(ROOT / "tests/fixtures/job-catalog.yaml", "critical"))
        self.assertIn("ARRAY['lineup-optimizer', 'waiver-trigger']::text[], ARRAY[3, 3]::int[]", sql)
        self.assertNotIn("'sync-league'", sql)

    def test_committed_rules_match_the_catalog(self):
        for level in generator.LEVELS:
            with self.subTest(level=level):
                self.assertEqual(
                    generator.output_path(level).read_text(), generator.render(generator.CATALOG, level)
                )


if __name__ == "__main__":
    unittest.main()
