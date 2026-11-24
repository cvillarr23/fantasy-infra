"""Keep unattended IR publication explicitly off in every rendered publisher."""

import unittest

import yaml

from tests.chart_render import render_text

FLAG = "AUTO_PUBLISH_IR_MOVES"
CONFIG = "fantasy-league-config"
ANNOTATION = "fantasy-infra/auto-publish-ir-moves"
PUBLISHERS = (
    ("Deployment", "fantasy-core-pipeline", "gateway"),
)
# Launched by fantasy-job-launcher from the catalog, which names env bundles, not containers.
CATALOG_PUBLISHERS = ("waiver-trigger", "rumor-action-trigger")
# Env bundles that add envFrom sources (k8s.BUNDLES in core): league-config imports
# fantasy-league-config, notify imports fantasy-notify-config and ntfy-credentials.
ENV_FROM_BUNDLES = {"league-config", "notify"}


class AutoPublishIrMovesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environments = {}
        for environment in ("production", "draft-day"):
            rendered = render_text(environment)
            cls.environments[environment] = {
                (doc["kind"], doc["metadata"]["name"]): doc
                for doc in yaml.safe_load_all(rendered)
                if doc
            }

    def test_shared_flag_is_explicitly_off(self):
        for environment, docs in self.environments.items():
            with self.subTest(environment=environment):
                self.assertEqual(docs[("ConfigMap", CONFIG)]["data"][FLAG], "false")
                self.assertNotIn("ACTIVITY_DEPENDENCY_HOLD_SECONDS",
                                 docs[("ConfigMap", CONFIG)]["data"])
                for (kind, name), doc in docs.items():
                    if kind == "ConfigMap" and FLAG in doc.get("data", {}):
                        self.assertEqual(doc["data"][FLAG], "false", name)

    def test_every_publisher_inherits_shared_flag_without_overrides(self):
        for environment, docs in self.environments.items():
            for kind, name, container_name in PUBLISHERS:
                with self.subTest(environment=environment, publisher=name):
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

    def test_every_catalog_publisher_inherits_shared_flag_without_overrides(self):
        for environment, docs in self.environments.items():
            catalog = _launcher_catalog(docs)
            for name in CATALOG_PUBLISHERS:
                with self.subTest(environment=environment, publisher=name):
                    profile = catalog["profiles"][catalog["jobs"][name]["profile"]]
                    # Restrict envFrom too: a later source could override the shared flag.
                    self.assertEqual(ENV_FROM_BUNDLES & set(profile["secrets"]), {"league-config"})
                    self.assertNotIn(FLAG, catalog["jobs"][name]["command"])


def _launcher_catalog(docs):
    (volume,) = docs[("Deployment", "fantasy-job-launcher")]["spec"]["template"]["spec"]["volumes"]
    return yaml.safe_load(docs[("ConfigMap", volume["configMap"]["name"])]["data"]["catalog.yaml"])


if __name__ == "__main__":
    unittest.main()
