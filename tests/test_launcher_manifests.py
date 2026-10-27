"""The launcher Deployment, RBAC, catalog and broker topology render as the ADR specifies."""

import json
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(overlay):
    out = subprocess.check_output(["kubectl", "kustomize", str(ROOT / "overlays" / overlay)], text=True)
    return [doc for doc in yaml.safe_load_all(out) if doc]


def one(docs, kind, name):
    (doc,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return doc


def env(container):
    return {e["name"]: e.get("value") for e in container["env"]}


class LauncherManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = {overlay: render(overlay) for overlay in ("production", "draft-day")}

    def test_deployment_shape(self):
        for overlay, docs in self.rendered.items():
            with self.subTest(overlay=overlay):
                spec = one(docs, "Deployment", "fantasy-job-launcher")["spec"]
                self.assertEqual(spec["replicas"], 1)
                self.assertEqual(spec["strategy"]["type"], "Recreate")
                (container,) = spec["template"]["spec"]["containers"]
                self.assertEqual(container["command"], ["python", "-m", "src.launcher"])
                self.assertEqual(container["resources"]["requests"], {"cpu": "50m", "memory": "128Mi"})
                self.assertEqual(spec["template"]["spec"]["serviceAccountName"], "fantasy-job-launcher")

    def test_image_placeholders_are_replaced_with_digests(self):
        for overlay, docs in self.rendered.items():
            with self.subTest(overlay=overlay):
                (container,) = one(docs, "Deployment", "fantasy-job-launcher")["spec"]["template"]["spec"]["containers"]
                values = env(container)
                self.assertIn("fantasy-core-pipeline@sha256:", values["CORE_IMAGE"])
                self.assertIn("fantasy-browser-worker@sha256:", values["WORKER_IMAGE"])
                self.assertEqual(values["CORE_IMAGE"], container["image"])

    def test_launcher_has_the_env_launched_pods_inherit(self):
        # k8s.BUNDLES in fantasy-core-pipeline copies these into every pod it creates.
        (container,) = one(self.rendered["production"], "Deployment", "fantasy-job-launcher")["spec"]["template"]["spec"]["containers"]
        names = set(env(container))
        for required in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "AMQP_URL", "REDIS_HOST",
                         "REDIS_PORT", "CORE_API_BASE_URL", "OTEL_EXPORTER_OTLP_ENDPOINT",
                         "WORKER_OTEL_EXPORTER_OTLP_ENDPOINT", "JOB_CATALOG_PATH"):
            self.assertIn(required, names)

    def test_catalog_is_empty_and_mounted_where_the_launcher_reads_it(self):
        docs = self.rendered["production"]
        deployment = one(docs, "Deployment", "fantasy-job-launcher")["spec"]["template"]["spec"]
        (container,) = deployment["containers"]
        self.assertEqual(env(container)["JOB_CATALOG_PATH"], "/etc/fantasy-job-catalog/catalog.yaml")
        (mount,) = container["volumeMounts"]
        self.assertEqual(mount["mountPath"], "/etc/fantasy-job-catalog")
        (volume,) = deployment["volumes"]
        config_map = one(docs, "ConfigMap", volume["configMap"]["name"])
        catalog = yaml.safe_load(config_map["data"]["catalog.yaml"])
        # Nothing may double-run with the live CronJobs until a cutover PR adds jobs.
        self.assertEqual(catalog["jobs"], {})
        self.assertFalse(catalog.get("chains"))

    def test_rbac_is_namespaced_and_minimal(self):
        docs = self.rendered["production"]
        role = one(docs, "Role", "fantasy-job-launcher")
        rules = {(tuple(r["apiGroups"]), tuple(r["resources"])): sorted(r["verbs"]) for r in role["rules"]}
        self.assertEqual(rules, {
            (("batch",), ("jobs",)): ["create", "delete", "get", "list", "watch"],
            (("",), ("pods",)): ["get", "list"],
        })
        binding = one(docs, "RoleBinding", "fantasy-job-launcher")
        self.assertEqual(binding["subjects"][0]["name"], "fantasy-job-launcher")
        self.assertEqual(binding["roleRef"]["name"], "fantasy-job-launcher")

    def test_broker_declares_the_launcher_topology(self):
        definitions = json.loads(
            one(self.rendered["production"], "ConfigMap", "fantasy-broker-definitions")["data"]["definitions.template.json"]
        )
        queues = {q["name"]: q for q in definitions["queues"]}
        for name in ("job_runs", "job_runs.dlq"):
            self.assertTrue(queues[name]["durable"])
        (events,) = [e for e in definitions["exchanges"] if e["name"] == "job_events"]
        self.assertEqual((events["type"], events["durable"]), ("topic", True))
        self.assertIn(
            {"source": "dlx", "vhost": "/", "destination": "job_runs.dlq", "destination_type": "queue",
             "routing_key": "job_runs", "arguments": {}},
            definitions["bindings"],
        )
        policies = {p["name"]: p for p in definitions["policies"]}
        runs = policies["fantasy-job-runs"]
        self.assertEqual(runs["pattern"], "^job_runs$")
        self.assertEqual(runs["definition"], {"dead-letter-exchange": "dlx", "message-ttl": 6 * 3600 * 1000})
        # RabbitMQ applies one policy per queue, so the generic one must not also match.
        self.assertNotRegex("job_runs", policies["fantasy-dead-letter"]["pattern"])
        self.assertNotRegex("job_runs.dlq", policies["fantasy-job-runs"]["pattern"])

    def test_alerts_are_provisioned(self):
        names = {d["metadata"]["name"] for d in self.rendered["production"] if d["kind"] == "ConfigMap"}
        for alert in ("heartbeat", "dlq", "chain-max-duration", "run-failed-critical", "run-failed-warning"):
            self.assertIn(f"fantasy-launcher-{alert}-alert", names)


if __name__ == "__main__":
    unittest.main()
