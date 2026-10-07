"""Exercise the deployed refresher supervisor without a database or upstream APIs."""

import logging
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "base/core-pipeline/draft-prep-refresher-cronjob.yaml"
STAGES = (
    "src.ingestion.sync_projections",
    "src.ingestion.sync_kicker_dst",
    "src.optimization.vorp_matrix",
)


class DraftPrepRefresherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = yaml.safe_load(MANIFEST.read_text())
        cls.container = cls.manifest["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        cls.code = compile(cls.container["args"][0], str(MANIFEST), "exec")

    def run_supervisor(self, week=5, failure=None, clock=None, season="2026", resolver_error=None, budgets=None):
        resolver = Mock(return_value=week, side_effect=resolver_error)
        module = types.ModuleType("src.nfl_schedule")
        module.resolve_upcoming_week = resolver
        calls = []

        def run(command, *, check, timeout):
            if budgets is not None:
                budgets.append(timeout)
            self.assertTrue(check)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 600)
            self.assertEqual(command[:3], [sys.executable, "-u", "-m"])
            self.assertEqual(command[4:6], ["--season", "2026"])
            self.assertEqual(command[6], "--week")
            key = (int(command[7]), command[3])
            calls.append(key)
            if failure:
                failure(key, command)
            # A successful CLI, including one writing zero rows, exits zero.
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(sys.modules, {module.__name__: module}), \
                patch.dict(os.environ, {"CURRENT_YEAR": season}), \
                patch("subprocess.run", side_effect=run), \
                patch("time.monotonic", side_effect=clock or (lambda: 0)), \
                self.assertLogs("DraftPrepRefresher", level=logging.INFO) as logs:
            with self.assertRaises(SystemExit) as result:
                exec(self.code, {})
        return result.exception.code, calls, resolver, "\n".join(logs.output)

    def test_weeks_are_complete_and_ordered_with_one_resolution(self):
        status, calls, resolver, logs = self.run_supervisor()
        self.assertEqual(status, 0)
        self.assertEqual(calls, [(week, stage) for week in range(5, 9) for stage in STAGES])
        resolver.assert_called_once_with(2026)
        self.assertIn("completed_weeks=[5, 6, 7, 8] failed_weeks=[]", logs)

    def test_regular_season_cap(self):
        for first in (16, 17, 18):
            with self.subTest(first=first):
                status, calls, _, _ = self.run_supervisor(week=first)
                self.assertEqual(status, 0)
                self.assertEqual(calls, [(week, stage) for week in range(first, min(first + 3, 18) + 1) for stage in STAGES])

    def test_offseason_is_an_explicit_no_op(self):
        status, calls, _, logs = self.run_supervisor(week=None)
        self.assertEqual(status, 0)
        self.assertEqual(calls, [])
        self.assertIn("regular season is over", logs)

    def test_invalid_configuration_and_resolution_fail_before_ingestion(self):
        # A resolver failure must fail the Job, never fall back to some other week.
        for kwargs in ({"week": 0}, {"week": -1}, {"week": 19}, {"season": "invalid"},
                       {"resolver_error": RuntimeError("schedule unavailable")}):
            with self.subTest(kwargs=kwargs):
                status, calls, _, logs = self.run_supervisor(**kwargs)
                self.assertEqual(status, 1)
                self.assertEqual(calls, [])
                self.assertIn("Unable to resolve refresh season/week", logs)

    def test_target_week_failure_stops_horizon_for_every_stage(self):
        for stage in STAGES:
            for error in (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                with self.subTest(stage=stage, error=error):
                    def fail(key, command):
                        if key == (5, stage):
                            if error is subprocess.CalledProcessError:
                                raise error(1, command)
                            if error is subprocess.TimeoutExpired:
                                raise error(command, 600)
                            raise error("cannot start process")
                    status, calls, _, logs = self.run_supervisor(failure=fail)
                    self.assertEqual(status, 1)
                    self.assertEqual(calls, [(5, step) for step in STAGES[:STAGES.index(stage) + 1]])
                    self.assertIn("completed_weeks=[] failed_weeks=[5]", logs)

    def test_each_future_week_failure_skips_dependents_and_continues(self):
        for failed_week in (6, 7, 8):
            for stage in STAGES:
                for timeout in (False, True):
                    with self.subTest(week=failed_week, stage=stage, timeout=timeout):
                        def fail(key, command):
                            if key == (failed_week, stage):
                                if timeout:
                                    raise subprocess.TimeoutExpired(command, 600)
                                raise subprocess.CalledProcessError(2, command)
                        status, calls, _, logs = self.run_supervisor(failure=fail)
                        expected = []
                        for week in range(5, 9):
                            steps = STAGES[:STAGES.index(stage) + 1] if week == failed_week else STAGES
                            expected.extend((week, step) for step in steps)
                        self.assertEqual(status, 1)
                        self.assertEqual(calls, expected)
                        self.assertIn(f"failed_weeks=[{failed_week}]", logs)

    def test_multiple_future_failures_are_reported_together(self):
        def fail(key, command):
            if key in ((6, STAGES[0]), (7, STAGES[1])):
                raise subprocess.CalledProcessError(1, command)
        status, calls, _, logs = self.run_supervisor(failure=fail)
        self.assertEqual(status, 1)
        self.assertEqual(calls[-3:], [(8, stage) for stage in STAGES])
        self.assertIn("completed_weeks=[5, 8] failed_weeks=[6, 7]", logs)

    def test_timeout_budget_is_shared_by_stages_and_reset_per_week(self):
        # Each clock observation advances 10 seconds, including logging timestamps.
        tick = iter(range(0, 1000, 10))
        budgets = []
        status, calls, _, _ = self.run_supervisor(clock=lambda: next(tick), budgets=budgets)
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 12)
        self.assertEqual(budgets, [590, 570, 550] * 4)

    def test_exhausted_week_budget_does_not_launch_next_stage(self):
        status, calls, _, logs = self.run_supervisor(clock=iter([0, 0, 600, 600, 600]).__next__)
        self.assertEqual(status, 1)
        self.assertEqual(calls, [(5, STAGES[0])])
        self.assertIn(f"week=5 stage={STAGES[1]}", logs)
        self.assertIn("failed_weeks=[5]", logs)

    def test_rendered_overlays_preserve_runtime_and_dependencies(self):
        for overlay in ("production", "draft-day"):
            with self.subTest(overlay=overlay):
                rendered = subprocess.check_output(["kubectl", "kustomize", str(ROOT / "overlays" / overlay)], text=True)
                job = next(doc for doc in yaml.safe_load_all(rendered)
                           if doc and doc.get("kind") == "CronJob"
                           and doc["metadata"]["name"] == "fantasy-draft-prep-refresher")
                self.assertEqual(job["spec"]["schedule"], "0 18 * * *")
                self.assertEqual(job["spec"]["timeZone"], "America/Chicago")
                self.assertEqual(job["spec"]["concurrencyPolicy"], "Forbid")
                spec = job["spec"]["jobTemplate"]["spec"]
                self.assertEqual(spec["activeDeadlineSeconds"], 3600)
                self.assertEqual(spec["backoffLimit"], 0)
                pod = spec["template"]["spec"]
                self.assertEqual(pod["restartPolicy"], "Never")
                self.assertFalse(pod.get("initContainers"))
                self.assertEqual(len(pod["containers"]), 1)
                container = pod["containers"][0]
                self.assertEqual(container["command"], ["python", "-u", "-c"])
                self.assertEqual(container["args"], self.container["args"])
                self.assertEqual(container["resources"], {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                })
                self.assertEqual(container["envFrom"], [{"configMapRef": {"name": "fantasy-league-config"}}])
                env = {entry["name"]: entry for entry in container["env"]}
                for name, secret, key in (("DB_PASSWORD", "database-credentials", "postgres-password"),
                                          ("ESPN_S2", "espn-session-secrets", "espn_s2"),
                                          ("ESPN_SWID", "espn-session-secrets", "swid")):
                    self.assertEqual(env[name]["valueFrom"]["secretKeyRef"], {"name": secret, "key": key})
                self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"], "http://tempo.default.svc.cluster.local:4318")
                # Draft-day inherits production's image transform.
                image_config = yaml.safe_load((ROOT / "overlays/production/kustomization.yaml").read_text())
                image = next(item for item in image_config["images"]
                             if item["name"] == "ghcr.io/cvillarr23/fantasy-core-pipeline")
                self.assertEqual(container["image"], image["name"] + "@" + image["digest"])


if __name__ == "__main__":
    unittest.main()
