"""The Helm chart renders a self-contained install with its defaults, and the parity check catches drift."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "fantasy"
NAMESPACE = "fantasy"

spec = importlib.util.spec_from_file_location("compare_renders", ROOT / "scripts" / "compare-renders.py")
compare_renders = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_renders)


def helm(*args):
    return subprocess.run(["helm", "template", "fantasy", str(CHART), "--namespace", NAMESPACE, *args],
                          capture_output=True, text=True)


def render(*args):
    result = helm(*args)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def one(docs, kind, name):
    (doc,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return doc


def containers(doc):
    pod = doc["spec"]["jobTargetRef"]["template"] if doc["kind"] == "ScaledJob" else doc["spec"]["template"]
    return pod["spec"]["containers"]


def env(doc, container=0):
    return {e["name"]: e.get("value") for e in containers(doc)[container].get("env", [])}


class SelfHostDefaultsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.text = yaml.safe_dump_all(cls.docs)

    def test_nothing_from_the_original_homelab(self):
        kinds = {doc["kind"] for doc in self.docs}
        self.assertNotIn("ExternalSecret", kinds)
        self.assertNotIn("Ingress", kinds)
        self.assertFalse([d for d in self.docs if d["metadata"].get("namespace") == "argocd"])
        self.assertFalse([d for d in self.docs if "grafana_alert" in d["metadata"].get("labels", {})
                          or "grafana_dashboard" in d["metadata"].get("labels", {})])
        for homelab in ("roo-net", "ts.net", "10.152.", "tempo.", "ghcr-pull-secret"):
            self.assertNotIn(homelab, self.text)

    def test_every_service_address_uses_the_release_namespace(self):
        self.assertNotIn(".default.svc", self.text)
        self.assertIn(f"fantasy-db.{NAMESPACE}.svc.cluster.local", self.text)

    def test_jobs_are_created_in_the_release_namespace(self):
        self.assertEqual(env(one(self.docs, "Deployment", "fantasy-job-launcher"))["K8S_NAMESPACE"], NAMESPACE)
        self.assertEqual(env(one(self.docs, "Deployment", "fantasy-core-pipeline"))["K8S_NAMESPACE"], NAMESPACE)
        self.assertEqual(one(self.docs, "ScaledJob", "fantasy-browser-worker-scaler")["metadata"]["namespace"], NAMESPACE)

    def test_writes_to_espn_start_off(self):
        self.assertEqual(one(self.docs, "ConfigMap", "fantasy-league-config")["data"]["DRY_RUN"], "true")
        worker = env(one(self.docs, "ScaledJob", "fantasy-browser-worker-scaler"))
        self.assertEqual((worker["TRADE_DRY_RUN"], worker["TRADE_SUBMIT_ENABLED"]), ("true", "false"))

    def test_no_trace_export_without_an_endpoint(self):
        for name in ("fantasy-job-launcher", "fantasy-core-pipeline"):
            self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", env(one(self.docs, "Deployment", name)))

    def test_images_follow_latest_without_a_digest(self):
        launcher = one(self.docs, "Deployment", "fantasy-job-launcher")
        self.assertEqual(containers(launcher)[0]["image"], "ghcr.io/cvillarr23/fantasy-core-pipeline:latest")
        self.assertEqual(env(launcher)["WORKER_IMAGE"], "ghcr.io/cvillarr23/fantasy-browser-worker:latest")

    def test_bundled_catalog_is_the_infra_catalog(self):
        catalog = one(self.docs, "ConfigMap", "fantasy-job-catalog")["data"]["catalog.yaml"]
        self.assertEqual(catalog, (ROOT / "base" / "launcher" / "catalog.yaml").read_text())


class OptionsTest(unittest.TestCase):
    def test_an_image_updater_tag_pins_the_digest_like_a_digest_does(self):
        core, worker, web = ("sha256:" + c * 64 for c in "abc")
        docs = render("--set", f"images.core.tag=latest@{core}", "--set", f"images.worker.tag=latest@{worker}",
                      "--set", f"images.webUi.tag=latest@{web}")
        launcher = one(docs, "Deployment", "fantasy-job-launcher")
        self.assertEqual(containers(launcher)[0]["image"], f"ghcr.io/cvillarr23/fantasy-core-pipeline@{core}")
        self.assertEqual(env(launcher)["CORE_IMAGE"], f"ghcr.io/cvillarr23/fantasy-core-pipeline@{core}")
        self.assertEqual(env(launcher)["WORKER_IMAGE"], f"ghcr.io/cvillarr23/fantasy-browser-worker@{worker}")
        self.assertEqual(containers(one(docs, "ScaledJob", "fantasy-browser-worker-scaler"))[0]["image"],
                         f"ghcr.io/cvillarr23/fantasy-browser-worker@{worker}")
        self.assertEqual(containers(one(docs, "Deployment", "fantasy-web-ui"))[0]["image"],
                         f"ghcr.io/cvillarr23/fantasy-web-ui@{web}")

    def test_a_custom_catalog_replaces_the_bundled_one_and_rolls_the_launcher(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as catalog:
            catalog.write("version: 1\njobs: {}\n")
            catalog.flush()
            custom = render("--set-file", f"launcher.catalog={catalog.name}")
        bundled = render()
        self.assertEqual(one(custom, "ConfigMap", "fantasy-job-catalog")["data"]["catalog.yaml"], "version: 1\njobs: {}\n")
        checksum = lambda docs: one(docs, "Deployment", "fantasy-job-launcher")["spec"]["template"]["metadata"]["annotations"]["checksum/catalog"]
        self.assertNotEqual(checksum(custom), checksum(bundled))

    def test_ingress_routes_to_oauth2_proxy(self):
        docs = render("--set", "ingress.enabled=true", "--set", "ingress.host=draft.example.com",
                      "--set", "ingress.className=nginx")
        (rule,) = one(docs, "Ingress", "fantasy")["spec"]["rules"]
        self.assertEqual(rule["host"], "draft.example.com")
        self.assertEqual(rule["http"]["paths"][0]["backend"]["service"], {"name": "oauth2-proxy", "port": {"number": 4180}})

    def test_ingress_needs_a_host(self):
        self.assertIn("ingress.host is required", helm("--set", "ingress.enabled=true").stderr)

    def test_external_postgres(self):
        self.assertIn("postgres.host is required", helm("--set", "postgres.enabled=false").stderr)
        docs = render("--set", "postgres.enabled=false", "--set", "postgres.host=db.example.internal")
        self.assertFalse([d for d in docs if d["metadata"]["name"] == "fantasy-db"])
        self.assertEqual(env(one(docs, "Deployment", "fantasy-job-launcher"))["DB_HOST"], "db.example.internal")

    def test_external_secrets_need_a_store(self):
        self.assertIn("storeRef.name is required", helm("--set", "secrets.provider=externalSecrets").stderr)

    def test_unknown_values_are_rejected(self):
        self.assertIn("additional properties 'tailscal' not allowed", helm("--set", "tailscal.enabled=true").stderr)


class CompareRendersTest(unittest.TestCase):
    def test_reports_missing_extra_and_changed_objects(self):
        key = lambda name: ("ConfigMap", "default", name)
        expected = {key("a"): {"data": {"x": "1"}}, key("b"): {}}
        actual = {key("a"): {"data": {"x": "2"}}, key("c"): {}}
        problems = compare_renders.diff(expected, actual)
        self.assertEqual([p.splitlines()[0] for p in problems], [
            "missing from the chart: ConfigMap default/b",
            "only in the chart: ConfigMap default/c",
            "differs: ConfigMap default/a",
        ])

    def test_identical_renders_pass(self):
        docs = {("Service", "", "fantasy-db"): {"spec": {"clusterIP": "None"}}}
        self.assertEqual(compare_renders.diff(docs, docs), [])

    def test_checksum_annotations_are_ignored(self):
        doc = {"metadata": {}, "spec": {"template": {"metadata": {"annotations": {"checksum/catalog": "abc"}}}}}
        self.assertEqual(compare_renders._without_checksums(doc), {"metadata": {}, "spec": {"template": {"metadata": {}}}})


if __name__ == "__main__":
    unittest.main()
