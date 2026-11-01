"""Keep rendered weekly consumers behind publication and upstream run deadlines."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import unittest
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
CENTRAL = ZoneInfo("America/Chicago")
ACTING_STEPS = ("lineup-optimizer", "waiver-trigger", "rumor-action-trigger")
DURATION = {"m": 60, "h": 3600}


def seconds(duration):
    return int(duration[:-1]) * DURATION[duration[-1]]


def launcher_catalog(docs):
    """The catalog ConfigMap the launcher mounts. BaseLoader keeps a chain edge's `on` key a
    string, as core's loader does."""
    (volume,) = docs[("Deployment", "fantasy-job-launcher")]["spec"]["template"]["spec"]["volumes"]
    data = docs[("ConfigMap", volume["configMap"]["name"])]["data"]["catalog.yaml"]
    return yaml.load(data, Loader=yaml.BaseLoader)


class Slot:
    """One catalog schedule, with the run limits of the job it belongs to."""

    def __init__(self, schedule, deadline="0m", attempts=1):
        self.cron = schedule["cron"]
        self.tz = schedule["tz"]
        self.catch_up = timedelta(seconds=seconds(schedule["catch_up"]))
        self.run_limit = timedelta(seconds=seconds(deadline) * attempts)

    def latest_finish(self, start):
        """When a run of this slot is over at the latest: a start delayed to the end of its
        catch_up window, then every attempt reaching its deadline."""
        return start + self.catch_up + self.run_limit


def job_slot(catalog, name, cron):
    job = catalog["jobs"][name]
    (schedule,) = [s for s in job["schedules"] if s["cron"] == cron]
    return Slot(schedule, job.get("deadline") or catalog["defaults"]["deadline"],
                int(job.get("max_attempts") or catalog["defaults"]["max_attempts"]))


def schedule_slots(catalog):
    return {
        "projections-refresh": job_slot(catalog, "projections-refresh", "0 18 * * *"),
        "reconcile": job_slot(catalog, "sync-actual-scores", "0 10 * * 5"),
        "trade-candidates": job_slot(catalog, "trade-candidates", "0 7 * * *"),
        "trade-candidates-prescan": job_slot(catalog, "trade-candidates", "30 12 * * 3,5"),
        "trade-scan": job_slot(catalog, "trade-scan", "0 13 * * 3,5"),
    }


class WeeklyScheduleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.overlays = {}
        cls.catalogs = {}
        for overlay in ("production", "draft-day"):
            rendered = subprocess.check_output(
                ["kubectl", "kustomize", str(ROOT / "overlays" / overlay)], text=True
            )
            docs = {(doc["kind"], doc["metadata"]["name"]): doc
                    for doc in yaml.safe_load_all(rendered) if doc}
            catalog = launcher_catalog(docs)
            cls.overlays[overlay] = schedule_slots(catalog)
            cls.catalogs[overlay] = catalog

    def scheduled_time(self, slot, day, weekday):
        self.assertEqual(slot.tz, "America/Chicago")
        minute, hour, month_day, month, weekdays = slot.cron.split()
        self.assertEqual((month_day, month), ("*", "*"))
        self.assertIn(weekdays, ("*", weekday))
        return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)

    def test_tuesday_chain_starts_after_publication_across_dst(self):
        for overlay, catalog in self.catalogs.items():
            chain = Slot(catalog["chains"]["tuesday-weekly"]["schedule"])
            for date in ((2026, 10, 13), (2026, 11, 3)):
                with self.subTest(overlay=overlay, date=date):
                    start = self.scheduled_time(chain, datetime(*date, tzinfo=CENTRAL), "2")
                    # Current evidence includes schedules published near 20:00Z.
                    # Retain publication headroom even during daylight saving time.
                    self.assertEqual(start.hour, 16)
                    self.assertEqual(start.astimezone(timezone.utc).hour,
                                     21 if date[1] == 10 else 22)

    def test_daily_cadences_and_roster_sync_are_preserved(self):
        for overlay, catalog in self.catalogs.items():
            with self.subTest(overlay=overlay):
                jobs = catalog["jobs"]
                # The refresh and rumor actions also run every other day; on Tuesday the
                # launcher skips these slots while the chain runs them.
                self.assertEqual([s["cron"] for s in jobs["projections-refresh"]["schedules"]],
                                 ["0 18 * * *"])
                self.assertEqual([s["cron"] for s in jobs["rumor-action-trigger"]["schedules"]],
                                 ["45 21 * * *"])
                # Scores run as a chain step on Tuesday and on their own only for Friday's reconcile.
                self.assertEqual([s["cron"] for s in jobs["sync-actual-scores"]["schedules"]],
                                 ["0 10 * * 5"])
                for name in ("sync-league", "metrics-extractor", "lineup-optimizer", "waiver-trigger"):
                    self.assertNotIn("schedules", jobs[name], name)
                # The roster sync is the chain's first step, so metrics and decisions follow it.
                (first,) = [name for name, step in catalog["chains"]["tuesday-weekly"]["steps"].items()
                            if not step]
                self.assertEqual(first, "sync-league")
                self.assertEqual(jobs["sync-league"]["command"],
                                 ["python", "-m", "src.ingestion.sync_league", "--season", "$(CURRENT_YEAR)"])
                self.assertEqual(jobs["metrics-extractor"]["command"],
                                 ["python", "-m", "src.ingestion.metrics_extractor"])

    def test_trade_scan_follows_previous_evening_refresh(self):
        for overlay, slots in self.overlays.items():
            for date in ((2026, 10, 14), (2026, 10, 16), (2026, 11, 4), (2026, 11, 6)):
                with self.subTest(overlay=overlay, date=date):
                    day = datetime(*date, tzinfo=CENTRAL)
                    scan = self.scheduled_time(slots["trade-scan"], day, "3,5")
                    refresh = self.scheduled_time(slots["projections-refresh"], day - timedelta(days=1), "*")
                    self.assertGreater(scan, slots["projections-refresh"].latest_finish(refresh))

    def test_score_reconciliation_precedes_friday_trade_jobs(self):
        for overlay, slots in self.overlays.items():
            for date in ((2026, 10, 16), (2026, 11, 6)):
                with self.subTest(overlay=overlay, date=date):
                    day = datetime(*date, tzinfo=CENTRAL)
                    reconcile = slots["reconcile"]
                    end = reconcile.latest_finish(self.scheduled_time(reconcile, day, "5"))
                    self.assertLess(end, self.scheduled_time(slots["trade-candidates-prescan"], day, "3,5"))
                    # Also finishes before that evening's refresh.
                    self.assertLess(end, self.scheduled_time(slots["projections-refresh"], day, "*"))

    def test_trade_candidates_finish_before_consumers_across_dst(self):
        for overlay, slots in self.overlays.items():
            for date in ((2026, 10, 14), (2026, 10, 16), (2026, 11, 4), (2026, 11, 6)):
                with self.subTest(overlay=overlay, date=date):
                    day = datetime(*date, tzinfo=CENTRAL)
                    daily = self.scheduled_time(slots["trade-candidates"], day, "*")
                    prescan = self.scheduled_time(slots["trade-candidates-prescan"], day, "3,5")
                    self.assertEqual((daily.hour, daily.minute), (7, 0))
                    self.assertEqual((prescan.hour, prescan.minute), (12, 30))
                    refresh = self.scheduled_time(slots["projections-refresh"], day - timedelta(days=1), "*")
                    self.assertGreater(daily, slots["projections-refresh"].latest_finish(refresh))
                    self.assertLess(slots["trade-candidates-prescan"].latest_finish(prescan),
                                    self.scheduled_time(slots["trade-scan"], day, "3,5"))

    def test_score_reconcile_keeps_its_limits_and_env(self):
        for overlay, catalog in self.catalogs.items():
            with self.subTest(overlay=overlay):
                job = catalog["jobs"]["sync-actual-scores"]
                self.assertEqual(job["command"], ["python", "-m", "src.ingestion.sync_actual_scores",
                                                  "--season", "$(CURRENT_YEAR)"])
                self.assertEqual((job["deadline"], job["max_attempts"]), ("30m", "2"))
                # db, espn and redis (k8s.BUNDLES in core) carry the credentials the sync reads;
                # league-config carries CURRENT_YEAR.
                secrets = catalog["profiles"][job["profile"]]["secrets"]
                self.assertLessEqual({"db", "espn", "redis", "league-config"}, set(secrets))

    def test_acting_jobs_get_espn_session_cookies_and_the_trigger_api_key(self):
        # These refresh rosters through sync_league first; without the ESPN cookies that
        # refresh is skipped with a warning and they decide on a stale roster snapshot. They
        # publish through the gateway's /v1/trigger/* routes, which reject requests without
        # X-API-Key; a missing key only surfaces once a run proposes an action. The `espn` and
        # `trigger-api` bundles (k8s.BUNDLES in core) carry both.
        for overlay, catalog in self.catalogs.items():
            for name in ACTING_STEPS:
                with self.subTest(overlay=overlay, name=name):
                    profile = catalog["profiles"][catalog["jobs"][name]["profile"]]
                    self.assertIn("espn", profile["secrets"])
                    self.assertIn("trigger-api", profile["secrets"])

if __name__ == "__main__":
    unittest.main()
