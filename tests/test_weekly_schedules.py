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


class Slot(dict):
    """A catalog schedule shaped like the CronJob fields scheduled_time reads."""

    def __init__(self, schedule, deadline):
        super().__init__(spec={"schedule": schedule["cron"], "timeZone": schedule["tz"],
                               "jobTemplate": {"spec": {"activeDeadlineSeconds": deadline}}})


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
            jobs = {name: doc for (kind, name), doc in docs.items() if kind == "CronJob"}
            # The daily refresh moved to the launcher; the trade jobs still order against it.
            refresh = catalog["jobs"]["projections-refresh"]
            (schedule,) = refresh["schedules"]
            jobs["projections-refresh"] = Slot(schedule, seconds(
                refresh.get("deadline") or catalog["defaults"]["deadline"]))
            cls.overlays[overlay] = jobs
            cls.catalogs[overlay] = catalog

    def scheduled_time(self, job, day, weekday):
        self.assertEqual(job["spec"]["timeZone"], "America/Chicago")
        minute, hour, month_day, month, weekdays = job["spec"]["schedule"].split()
        self.assertEqual((month_day, month), ("*", "*"))
        self.assertIn(weekdays, ("*", weekday))
        return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)

    def test_tuesday_chain_starts_after_publication_across_dst(self):
        for overlay, catalog in self.catalogs.items():
            schedule = catalog["chains"]["tuesday-weekly"]["schedule"]
            chain = Slot(schedule, 0)
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
                for name in ("sync-league", "metrics-extractor", "sync-actual-scores",
                             "lineup-optimizer", "waiver-trigger"):
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
        for overlay, jobs in self.overlays.items():
            for date in ((2026, 10, 14), (2026, 10, 16), (2026, 11, 4), (2026, 11, 6)):
                with self.subTest(overlay=overlay, date=date):
                    day = datetime(*date, tzinfo=CENTRAL)
                    scan = self.scheduled_time(jobs["trade-scan"], day, "3,5")
                    refresh = self.scheduled_time(jobs["projections-refresh"], day - timedelta(days=1), "*")
                    deadline = jobs["projections-refresh"]["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]
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
                    refresh = self.scheduled_time(jobs["projections-refresh"], day, "*")
                    self.assertLess(end, refresh)

    def test_trade_candidates_finish_before_consumers_across_dst(self):
        for overlay, jobs in self.overlays.items():
            self.assertIn("fantasy-trade-candidates", jobs)
            self.assertIn("fantasy-trade-candidates-prescan", jobs)
            for date in ((2026, 10, 14), (2026, 10, 16), (2026, 11, 4), (2026, 11, 6)):
                with self.subTest(overlay=overlay, date=date):
                    day = datetime(*date, tzinfo=CENTRAL)
                    daily = self.scheduled_time(jobs["fantasy-trade-candidates"], day, "*")
                    prescan_job = jobs["fantasy-trade-candidates-prescan"]
                    prescan = self.scheduled_time(prescan_job, day, "3,5")
                    self.assertEqual((daily.hour, daily.minute), (7, 0))
                    self.assertEqual((prescan.hour, prescan.minute), (12, 30))
                    refresh_job = jobs["projections-refresh"]
                    refresh = self.scheduled_time(refresh_job, day - timedelta(days=1), "*")
                    refresh_end = refresh + timedelta(seconds=refresh_job["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"])
                    self.assertGreater(daily, refresh_end)
                    latest_finish = prescan + timedelta(
                        seconds=prescan_job["spec"]["startingDeadlineSeconds"]
                        + prescan_job["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"])
                    self.assertLess(latest_finish, self.scheduled_time(jobs["trade-scan"], day, "3,5"))

    def test_score_sync_env_secrets_and_safety(self):
        secret_refs = {
            "DB_PASSWORD": ("database-credentials", "postgres-password"),
            "ESPN_S2": ("espn-session-secrets", "espn_s2"),
            "ESPN_SWID": ("espn-session-secrets", "swid"),
        }
        for overlay, jobs in self.overlays.items():
            for name in ("fantasy-sync-actual-scores-reconcile",):
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
