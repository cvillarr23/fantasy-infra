"""Run the rendered waiver trigger script against a stub python: the evaluator must only
run for a resolved week, and a resolver failure must fail the Job."""

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
STUB = """#!/bin/sh
echo "$*" >> "$STUB_CALLS"
if [ "$1" = "-c" ]; then
  printf '%s\\n' "$STUB_WEEK"
  exit "$STUB_RESOLVE_EXIT"
fi
exit 0
"""


class WeeklyWaiverWeekTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rendered = subprocess.check_output(
            ["kubectl", "kustomize", str(ROOT / "overlays" / "production")], text=True
        )
        job = next(doc for doc in yaml.safe_load_all(rendered)
                   if doc and doc.get("kind") == "CronJob"
                   and doc["metadata"]["name"] == "fantasy-weekly-waiver-trigger")
        container = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["/bin/sh", "-c"]
        cls.script = container["args"][0]

    def run_script(self, week="", resolve_exit=0):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "python"
            stub.write_text(STUB)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            calls = Path(tmp) / "calls"
            calls.touch()
            env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "STUB_CALLS": str(calls),
                   "STUB_WEEK": week, "STUB_RESOLVE_EXIT": str(resolve_exit),
                   "CURRENT_YEAR": "2026", "ESPN_TEAM_ID": "8"}
            result = subprocess.run(["/bin/sh", "-c", self.script], env=env,
                                    capture_output=True, text=True, timeout=30)
            return result, calls.read_text().splitlines()

    def test_resolved_week_runs_the_evaluator_for_that_week(self):
        result, calls = self.run_script(week="5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn("src.nfl_schedule import resolve_upcoming_week", calls[0])
        self.assertEqual(calls[1], "-m src.optimization.waiver_evaluator --team-id 8 --season 2026 --week 5")

    def test_resolver_failure_fails_the_job_without_evaluating(self):
        result, calls = self.run_script(week="", resolve_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)

    def test_season_over_is_a_clean_no_op(self):
        result, calls = self.run_script(week="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("regular season is over", result.stdout)


if __name__ == "__main__":
    unittest.main()
