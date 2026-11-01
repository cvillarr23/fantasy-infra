"""Alert rules removed from base/telemetry are deleted from Grafana, not just unprovisioned."""

from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def rendered_telemetry():
    out = subprocess.check_output(["kubectl", "kustomize", str(ROOT / "base" / "telemetry")], text=True)
    return [doc for doc in yaml.safe_load_all(out) if doc]


def provisioned(docs):
    """Parsed Grafana provisioning files from every grafana_alert ConfigMap."""
    files = []
    for doc in docs:
        if doc["kind"] == "ConfigMap" and doc["metadata"].get("labels", {}).get("grafana_alert") == "1":
            files += [yaml.safe_load(text) for text in doc["data"].values()]
    return files


class DeletedAlertRulesTest(unittest.TestCase):
    def test_cut_over_missed_schedule_rules_are_deleted(self):
        files = provisioned(rendered_telemetry())
        live = {r["uid"] for f in files for g in f.get("groups", []) for r in g["rules"]}
        deleted = {r["uid"] for f in files for r in f.get("deleteRules", [])}
        self.assertEqual(deleted, {"fantasy-cronjob-missed-schedule-rumor",
                                   "fantasy-cronjob-missed-schedule-sweeper",
                                   "fantasy-cronjob-missed-schedule-rapid"})
        self.assertFalse(live & deleted, "a rule cannot be both provisioned and deleted")


if __name__ == "__main__":
    unittest.main()
