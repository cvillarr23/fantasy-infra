"""Keep unattended IR publication explicitly off in every rendered publisher."""

from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
FLAG = "AUTO_PUBLISH_IR_MOVES"
CONFIG = "fantasy-league-config"
ANNOTATION = "fantasy-infra/auto-publish-ir-moves"
PUBLISHERS = (
    ("Deployment", "fantasy-core-pipeline", "gateway"),
    ("CronJob", "fantasy-weekly-waiver-trigger", "waiver-trigger"),
    ("CronJob", "fantasy-rumor-action-trigger", "rumor-action-trigger"),
)


class AutoPublishIrMovesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.overlays = {}
        for overlay in ("production", "draft-day"):
            rendered = subprocess.check_output(
                ["kubectl", "kustomize", str(ROOT / "overlays" / overlay)], text=True
            )
            cls.overlays[overlay] = {
                (doc["kind"], doc["metadata"]["name"]): doc
                for doc in yaml.safe_load_all(rendered)
                if doc
            }

    def test_shared_flag_is_explicitly_off(self):
        for overlay, docs in self.overlays.items():
            with self.subTest(overlay=overlay):
                self.assertEqual(docs[("ConfigMap", CONFIG)]["data"][FLAG], "false")
                self.assertNotIn("ACTIVITY_DEPENDENCY_HOLD_SECONDS",
                                 docs[("ConfigMap", CONFIG)]["data"])
                for (kind, name), doc in docs.items():
                    if kind == "ConfigMap" and FLAG in doc.get("data", {}):
                        self.assertEqual(doc["data"][FLAG], "false", name)

    def test_every_publisher_inherits_shared_flag_without_overrides(self):
        for overlay, docs in self.overlays.items():
            for kind, name, container_name in PUBLISHERS:
                with self.subTest(overlay=overlay, publisher=name):
                    workload = docs[(kind, name)]
                    template = (
                        workload["spec"]["template"] if kind == "Deployment"
                        else workload["spec"]["jobTemplate"]["spec"]["template"]
                    )
                    container = next(
                        item for item in template["spec"]["containers"]
                        if item["name"] == container_name
                    )
                    # Restrict envFrom too: a later source could override the shared flag.
                    self.assertEqual(container["envFrom"], [
                        {"configMapRef": {"name": CONFIG}}
                    ])
                    self.assertNotIn(FLAG, {item["name"] for item in container.get("env", [])})
                    self.assertNotIn("ACTIVITY_DEPENDENCY_HOLD_SECONDS", {
                        item["name"] for item in container.get("env", [])
                    })
                    if kind == "Deployment":
                        self.assertEqual(template["metadata"]["annotations"][ANNOTATION],
                                         docs[("ConfigMap", CONFIG)]["data"][FLAG])


if __name__ == "__main__":
    unittest.main()
