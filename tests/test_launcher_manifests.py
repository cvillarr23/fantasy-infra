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


def catalog(docs):
    (volume,) = one(docs, "Deployment", "fantasy-job-launcher")["spec"]["template"]["spec"]["volumes"]
    # BaseLoader keeps every scalar a string, like core's loader: YAML 1.1 reads a chain edge's
    # `on` key as True.
    return yaml.load(one(docs, "ConfigMap", volume["configMap"]["name"])["data"]["catalog.yaml"],
                     Loader=yaml.BaseLoader)


def entrypoint(command):
    """`python -m <module>` or `node <script>`, without the arguments: the catalog passes
    `$(CURRENT_YEAR)` where a CronJob hardcoded the season."""
    return command[:3] if command[1:2] == ["-m"] else command[:2]


def cronjob_entrypoints(docs):
    """Every container and init container entrypoint the rendered CronJobs run, by CronJob."""
    entrypoints = {}
    for cronjob in (d for d in docs if d["kind"] == "CronJob"):
        pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        for container in pod["containers"] + pod.get("initContainers", []):
            entrypoints.setdefault(cronjob["metadata"]["name"], []).append(entrypoint(container["command"]))
    return entrypoints


TUESDAY_STEPS = ["sync-league", "metrics-extractor", "sync-actual-scores", "projections-refresh",
                 "lineup-optimizer", "waiver-trigger", "rumor-action-trigger"]
# Batch C moves the Friday score reconcile onto the catalog's sync-actual-scores job; until
# then that CronJob and the chain step share an entrypoint on different days.
SHARED_UNTIL_BATCH_C = {"sync-actual-scores": "fantasy-sync-actual-scores-reconcile"}


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

    def test_catalog_holds_the_cutover_jobs_and_is_mounted_where_the_launcher_reads_it(self):
        docs = self.rendered["production"]
        deployment = one(docs, "Deployment", "fantasy-job-launcher")["spec"]["template"]["spec"]
        (container,) = deployment["containers"]
        self.assertEqual(env(container)["JOB_CATALOG_PATH"], "/etc/fantasy-job-catalog/catalog.yaml")
        (mount,) = container["volumeMounts"]
        self.assertEqual(mount["mountPath"], "/etc/fantasy-job-catalog")
        jobs = catalog(docs)["jobs"]
        self.assertEqual(set(jobs), {"rumor-syncer", "rapid-status-syncer", "stale-draft-job-sweeper",
                                     "espn-session-healthcheck", "rumor-digest-bot", *TUESDAY_STEPS})
        self.assertEqual(set(catalog(docs)["chains"]), {"tuesday-weekly"})

    def test_tuesday_chain_orders_the_weekly_jobs(self):
        chain = catalog(self.rendered["production"])["chains"]["tuesday-weekly"]
        self.assertEqual(chain["schedule"], {"cron": "0 16 * * 2", "tz": "America/Chicago",
                                             "catch_up": "2h", "seasons": ["regular"]})
        steps = chain["steps"]
        self.assertEqual(list(steps), TUESDAY_STEPS)
        self.assertEqual(steps["sync-league"], {})
        edges = {name: [(d["step"], d["on"]) for d in step["after"]]
                 for name, step in steps.items() if name != "sync-league"}
        self.assertEqual(edges, {
            # Scores only update weekly_stats rows the ingestor creates, so data steps need success.
            "metrics-extractor": [("sync-league", "success")],
            "sync-actual-scores": [("metrics-extractor", "success")],
            # The refresh and the acting steps run even when ingestion fails, as the CronJobs did.
            "projections-refresh": [("sync-actual-scores", "finish")],
            "lineup-optimizer": [("projections-refresh", "finish")],
            "waiver-trigger": [("lineup-optimizer", "finish")],
            "rumor-action-trigger": [("waiver-trigger", "finish")],
        })
        self.assertEqual({name: step.get("latest") for name, step in steps.items() if step.get("latest")},
                         {"lineup-optimizer": "19:15", "waiver-trigger": "20:30",
                          "rumor-action-trigger": "21:45"})

    def test_no_catalog_job_still_has_a_cronjob(self):
        # A command both scheduled here and in a CronJob runs twice per slot.
        for overlay, docs in self.rendered.items():
            with self.subTest(overlay=overlay):
                for name, job in catalog(docs)["jobs"].items():
                    still_run_by = {cronjob for cronjob, entrypoints in cronjob_entrypoints(docs).items()
                                    if entrypoint(job["command"]) in entrypoints}
                    expected = {SHARED_UNTIL_BATCH_C[name]} if name in SHARED_UNTIL_BATCH_C else set()
                    self.assertEqual(still_run_by, expected, name)

    def test_cutover_profiles_match_what_their_cronjobs_carried(self):
        profiles = catalog(self.rendered["production"])["profiles"]
        # The sweeper lists and deletes draft Jobs through this SA's draft-sync-orchestrator Role.
        self.assertEqual(profiles["core-ops"]["serviceAccount"], "fantasy-core-pipeline")
        for name in ("core-ops", "worker-session-check", "worker-llm"):
            self.assertIn("db", profiles[name]["secrets"])
        self.assertEqual(set(profiles["worker-session-check"]["secrets"]), {"db", "espn", "notify", "league-config"})
        self.assertNotIn("espn", profiles["worker-llm"]["secrets"])
        self.assertIn("llm", profiles["worker-llm"]["secrets"])
        jobs = catalog(self.rendered["production"])["jobs"]
        for name in TUESDAY_STEPS:
            self.assertEqual(jobs[name]["profile"], "core", name)

    def test_gateway_mounts_the_same_catalog(self):
        # The Jobs API reads the catalog from the gateway pod; without it manual runs answer 503.
        for overlay, docs in self.rendered.items():
            with self.subTest(overlay=overlay):
                pod = one(docs, "Deployment", "fantasy-core-pipeline")["spec"]["template"]["spec"]
                (gateway,) = pod["containers"]
                self.assertEqual(env(gateway)["JOB_CATALOG_PATH"], "/etc/fantasy-job-catalog/catalog.yaml")
                (mount,) = [m for m in gateway["volumeMounts"] if m["name"] == "job-catalog"]
                self.assertEqual(mount["mountPath"], "/etc/fantasy-job-catalog")
                launcher = one(docs, "Deployment", "fantasy-job-launcher")["spec"]["template"]["spec"]
                (gateway_volume,) = [v for v in pod["volumes"] if v["name"] == "job-catalog"]
                self.assertEqual(gateway_volume["configMap"], launcher["volumes"][0]["configMap"])

    def test_core_profile_has_the_bundles_cronjobs_rely_on(self):
        # CURRENT_YEAR comes from league-config and publishing from amqp (k8s.BUNDLES in core).
        docs = self.rendered["production"]
        (volume,) = one(docs, "Deployment", "fantasy-job-launcher")["spec"]["template"]["spec"]["volumes"]
        catalog = yaml.safe_load(one(docs, "ConfigMap", volume["configMap"]["name"])["data"]["catalog.yaml"])
        for bundle in ("db", "espn", "redis", "trigger-api", "amqp", "league-config"):
            self.assertIn(bundle, catalog["profiles"]["core"]["secrets"])

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
        for alert in ("heartbeat", "dlq", "queue-depth-unknown", "job-runs-backlog", "chain-max-duration",
                      "run-failed-critical", "run-failed-warning"):
            self.assertIn(f"fantasy-launcher-{alert}-alert", names)


if __name__ == "__main__":
    unittest.main()
