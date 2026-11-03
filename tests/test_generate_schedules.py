"""Catalog schedule documentation generation tests."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("generate_schedules", ROOT / "scripts/generate-schedules.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


class CatalogScheduleTest(unittest.TestCase):
    def test_catalog_jobs_and_chains_render(self):
        page = generator.render_catalog(ROOT / "tests/fixtures/job-catalog.yaml")

        self.assertIn("`projections-refresh` | `0 18 * * *` (America/Chicago)", page)
        self.assertIn("`metrics-extractor` | — |", page)
        self.assertIn("### Chain: tuesday-weekly", page)
        self.assertIn("sync-league -->|success| metrics-extractor", page)
        self.assertIn("lineup-optimizer (latest 19:15)", page)
        self.assertIn("`rumor-action-trigger` | `45 21 * * *` (America/Chicago)<br/>"
                      "on `rumor.signal_created` (debounce 30m) |", page)

    def test_chain_edge_without_on_defaults_to_success(self):
        page = generator.render_catalog(ROOT / "tests/fixtures/job-catalog-defaults.yaml")

        self.assertIn("sync-league -->|success| metrics-extractor", page)

    def test_unscheduled_chain_is_labelled_manual_only(self):
        page = generator.render_catalog(ROOT / "tests/fixtures/job-catalog-defaults.yaml")

        self.assertIn("### Chain: manual-refresh\n\nSchedule: Manual only\n", page)
        self.assertNotIn("Event driven", page)


if __name__ == "__main__":
    unittest.main()
