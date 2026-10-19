"""Keep rendered weekly consumers behind publication and upstream Job deadlines."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
import unittest
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
CENTRAL = ZoneInfo("America/Chicago")
CHAIN = (
    "fantasy-weekly-ingestor",
    "fantasy-sync-actual-scores",
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
                    refresh = self.scheduled_time(jobs["fantasy-draft-prep-refresher"], day - timedelta(days=1), "*")
                    deadline = jobs["fantasy-draft-prep-refresher"]["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]
                    self.assertGreater(scan, refresh + timedelta(seconds=deadline))

    def test_score_reconciliation_precedes_friday_trade_scan(self):
        for overlay, jobs in self.overlays.items():
            for date in ((2026, 10, 16), (2026, 11, 6)):
                with self.subTest(overlay=overlay, date=date):
                    day = datetime(*date, tzinfo=CENTRAL)
                    job = jobs["fantasy-sync-actual-scores-reconcile"]
                    start = self.scheduled_time(job, day, "5")
                    end = start + timedelta(
                        seconds=job["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"])
                    self.assertLess(end, self.scheduled_time(jobs["trade-scan"], day, "3,5"))
                    # Also finishes before that evening's refresher.
                    refresh = self.scheduled_time(jobs["fantasy-draft-prep-refresher"], day, "*")
                    self.assertLess(end, refresh)

    def test_score_sync_env_secrets_and_safety(self):
        secret_refs = {
            "DB_PASSWORD": ("database-credentials", "postgres-password"),
            "ESPN_S2": ("espn-session-secrets", "espn_s2"),
            "ESPN_SWID": ("espn-session-secrets", "swid"),
        }
        for overlay, jobs in self.overlays.items():
            for name in ("fantasy-sync-actual-scores", "fantasy-sync-actual-scores-reconcile"):
                with self.subTest(overlay=overlay, name=name):
                    spec = jobs[name]["spec"]
                    self.assertEqual(spec["concurrencyPolicy"], "Forbid")
                    job = spec["jobTemplate"]["spec"]
                    self.assertEqual(job["backoffLimit"], 1)
                    self.assertEqual(job["activeDeadlineSeconds"], 1800)
                    pod = job["template"]["spec"]
                    self.assertEqual(pod["restartPolicy"], "Never")
                    (container,) = pod["containers"]
                    self.assertEqual(container["command"], [
                        "python", "-m", "src.ingestion.sync_actual_scores", "--season", "2026"])
                    self.assertNotIn("args", container)
                    self.assertRegex(container["image"],
                                     r"^ghcr\.io/cvillarr23/fantasy-core-pipeline@sha256:[0-9a-f]{64}$")
                    self.assertEqual(container["envFrom"],
                                     [{"configMapRef": {"name": "fantasy-league-config"}}])
                    env = {item["name"]: item for item in container["env"]}
                    for var, (secret, key) in secret_refs.items():
                        self.assertNotIn("value", env[var])
                        self.assertEqual(env[var]["valueFrom"]["secretKeyRef"],
                                         {"name": secret, "key": key})
                    self.assertEqual(env["REDIS_HOST"]["value"],
                                     "fantasy-cache.default.svc.cluster.local")
                    self.assertFalse(any(re.search(r"espn_s2|swid", part, re.I)
                                         for part in container["command"]))

    def test_roster_refreshing_jobs_get_espn_session_cookies(self):
        # These refresh rosters through sync_league first; without the cookies that refresh
        # is skipped with a warning and they decide on a stale roster snapshot.
        secret_refs = {
            "ESPN_S2": ("espn-session-secrets", "espn_s2"),
            "ESPN_SWID": ("espn-session-secrets", "swid"),
        }
        for overlay, jobs in self.overlays.items():
            for name in ("fantasy-lineup-optimizer", "fantasy-weekly-waiver-trigger",
                         "fantasy-rumor-action-trigger"):
                with self.subTest(overlay=overlay, name=name):
                    (container,) = jobs[name]["spec"]["jobTemplate"]["spec"]["template"][
                        "spec"]["containers"]
                    env = {item["name"]: item for item in container["env"]}
                    for var, (secret, key) in secret_refs.items():
                        self.assertNotIn("value", env[var])
                        self.assertEqual(env[var]["valueFrom"]["secretKeyRef"],
                                         {"name": secret, "key": key})


if __name__ == "__main__":
    unittest.main()
