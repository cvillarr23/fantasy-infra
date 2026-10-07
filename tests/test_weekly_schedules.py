"""Keep rendered weekly consumers behind publication and upstream Job deadlines."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import unittest
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
CENTRAL = ZoneInfo("America/Chicago")
CHAIN = (
    "fantasy-weekly-ingestor",
    "fantasy-draft-prep-refresher",
    "fantasy-lineup-optimizer",
    "fantasy-weekly-waiver-trigger",
    "fantasy-rumor-action-trigger",
)


class WeeklyScheduleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.overlays = {}
        for overlay in ("production", "draft-day"):
            rendered = subprocess.check_output(
                ["kubectl", "kustomize", str(ROOT / "overlays" / overlay)], text=True
            )
            cls.overlays[overlay] = {
                doc["metadata"]["name"]: doc
                for doc in yaml.safe_load_all(rendered)
                if doc and doc.get("kind") == "CronJob"
            }

    def scheduled_time(self, job, day, weekday):
        self.assertEqual(job["spec"]["timeZone"], "America/Chicago")
        minute, hour, month_day, month, weekdays = job["spec"]["schedule"].split()
        self.assertEqual((month_day, month), ("*", "*"))
        self.assertIn(weekdays, ("*", weekday))
        return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)

    def test_tuesday_consumers_start_after_upstream_deadlines_across_dst(self):
        for overlay, jobs in self.overlays.items():
            for date in ((2026, 10, 13), (2026, 11, 3)):
                with self.subTest(overlay=overlay, date=date):
                    day = datetime(*date, tzinfo=CENTRAL)
                    times = [self.scheduled_time(jobs[name], day, "2") for name in CHAIN]
                    # Current evidence includes schedules published near 20:00Z.
                    # Retain publication headroom even during daylight saving time.
                    self.assertGreaterEqual(times[0].astimezone(timezone.utc).hour, 21)
                    self.assertEqual(times[0].hour, 16)
                    self.assertEqual(times[0].astimezone(timezone.utc).hour,
                                     21 if date[1] == 10 else 22)
                    for name, start, following in zip(CHAIN, times, times[1:]):
                        deadline = jobs[name]["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]
                        self.assertGreater(following, start + timedelta(seconds=deadline), name)

    def test_cadences_and_roster_sync_are_preserved(self):
        for overlay, jobs in self.overlays.items():
            with self.subTest(overlay=overlay):
                for name in CHAIN:
                    weekday = jobs[name]["spec"]["schedule"].split()[4]
                    self.assertEqual(weekday, "*" if name in (
                        "fantasy-draft-prep-refresher", "fantasy-rumor-action-trigger"
                    ) else "2")
                pod = jobs[CHAIN[0]]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                self.assertEqual([item["name"] for item in pod["initContainers"]], ["sync-league"])
                self.assertEqual(pod["initContainers"][0]["command"],
                                 ["python", "-m", "src.ingestion.sync_league", "--season", "2026"])
                self.assertEqual(pod["containers"][0]["command"],
                                 ["python", "-m", "src.ingestion.metrics_extractor"])

    def test_trade_scan_follows_previous_evening_refresh(self):
        for overlay, jobs in self.overlays.items():
            for date in ((2026, 10, 14), (2026, 10, 16), (2026, 11, 4), (2026, 11, 6)):
                with self.subTest(overlay=overlay, date=date):
                    day = datetime(*date, tzinfo=CENTRAL)
                    scan = self.scheduled_time(jobs["trade-scan"], day, "3,5")
                    refresh = self.scheduled_time(jobs[CHAIN[1]], day - timedelta(days=1), "*")
                    deadline = jobs[CHAIN[1]]["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]
                    self.assertGreater(scan, refresh + timedelta(seconds=deadline))


if __name__ == "__main__":
    unittest.main()
