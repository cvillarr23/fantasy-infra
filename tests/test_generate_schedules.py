"""Catalog schedule documentation generation tests."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("generate_schedules", ROOT / "scripts/generate-schedules.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


class CatalogScheduleTest(unittest.TestCase):
    def test_catalog_jobs_and_chains_render_without_cronjob_alerts(self):
        page = generator.render_catalog(ROOT / "tests/fixtures/job-catalog.yaml")

        self.assertIn("`projections-refresh` | `0 18 * * *` (America/Chicago)", page)
        self.assertIn("`metrics-extractor` | — |", page)
        self.assertIn("## tuesday-weekly", page)
        self.assertIn("sync-league -->|success| metrics-extractor", page)
        self.assertIn("lineup-optimizer (latest 19:15)", page)

    def test_catalog_only_inventory_does_not_render_legacy_cronjobs(self):
        page = generator.inventory(
            catalog_only=True,
            catalog_path=ROOT / "tests/fixtures/job-catalog.yaml",
        )

        self.assertIn("### Catalog jobs", page)
        self.assertNotIn("Legacy CronJobs", page)

    def test_legacy_cronjob_names_map_to_catalog_jobs_during_cutover(self):
        catalog_jobs = {
            "projections-refresh": {},
            "sync-league": {},
            "waiver-trigger": {},
            "sync-actual-scores": {},
            "trade-candidates": {},
        }

        for name in (
            "fantasy-draft-prep-refresher",
            "fantasy-weekly-ingestor",
            "fantasy-weekly-waiver-trigger",
            "fantasy-sync-actual-scores-reconcile",
            "fantasy-trade-candidates-prescan",
        ):
            with self.subTest(name=name):
                self.assertTrue(generator.is_catalog_backed_cronjob(name, catalog_jobs))

        self.assertFalse(generator.is_catalog_backed_cronjob("untracked-job", catalog_jobs))


if __name__ == "__main__":
    unittest.main()
