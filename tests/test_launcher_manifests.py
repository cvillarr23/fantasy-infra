"""The launcher Deployment, RBAC, catalog and broker topology render as the ADR specifies."""

import json
import unittest

import yaml

from tests.chart_render import render


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


TUESDAY_STEPS = ["sync-league", "metrics-extractor", "sync-actual-scores", "espn-ownership-sync",
                 "projections-refresh", "lineup-optimizer", "waiver-trigger", "rumor-action-trigger"]
TRADE_JOBS = ["trade-candidates", "trade-scan", "trade-inbox", "trade-submit"]
# Started and cancelled by the draft API (core-pipeline#350), never scheduled.
DRAFT_JOBS = ["draft-listener", "practice-draft-listener", "draft-connector", "practice-draft-connector"]


class LauncherManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = {environment: render(environment) for environment in ("production", "draft-day")}

    def test_deployment_shape(self):
        for environment, docs in self.rendered.items():
            with self.subTest(environment=environment):
                spec = one(docs, "Deployment", "fantasy-job-launcher")["spec"]
                # Leader election over a Lease makes the second replica a standby.
                self.assertEqual(spec["replicas"], 2)
                self.assertEqual(spec["strategy"]["type"], "RollingUpdate")
                self.assertEqual(spec["strategy"]["rollingUpdate"], {"maxSurge": 1, "maxUnavailable": 0})
                (container,) = spec["template"]["spec"]["containers"]
                self.assertEqual(container["command"], ["python", "-m", "src.launcher"])
                self.assertEqual(container["resources"]["requests"], {"cpu": "50m", "memory": "192Mi"})
                self.assertEqual(spec["template"]["spec"]["serviceAccountName"], "fantasy-job-launcher")

    def test_image_placeholders_are_replaced_with_digests(self):
        for environment, docs in self.rendered.items():
            with self.subTest(environment=environment):
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
        self.assertEqual(set(jobs), {"rumor-syncer", "rapid-status-syncer",
                                     "espn-session-healthcheck", "rumor-digest-bot", *TUESDAY_STEPS, *TRADE_JOBS,
                                     *DRAFT_JOBS, "live-games"})
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
            # The ownership snapshot is best-effort: a failed ESPN fetch blocks nothing downstream.
            "espn-ownership-sync": [("sync-league", "finish")],
            "projections-refresh": [("sync-actual-scores", "finish"), ("espn-ownership-sync", "finish")],
            "lineup-optimizer": [("projections-refresh", "finish")],
            "waiver-trigger": [("lineup-optimizer", "finish")],
            "rumor-action-trigger": [("waiver-trigger", "finish")],
        })
        self.assertEqual({name: step.get("latest") for name, step in steps.items() if step.get("latest")},
                         {"lineup-optimizer": "19:15", "waiver-trigger": "20:30",
                          "rumor-action-trigger": "21:45"})

    def test_trade_jobs_never_auto_retry(self):
        for environment, docs in self.rendered.items():
            with self.subTest(environment=environment):
                content = catalog(docs)
                self.assertEqual(content["defaults"]["max_attempts"], "1")
                for name in TRADE_JOBS:
                    self.assertNotIn("max_attempts", content["jobs"][name], name)

    def test_every_job_runs_from_the_catalog(self):
        for environment, docs in self.rendered.items():
            with self.subTest(environment=environment):
                self.assertEqual([d["metadata"]["name"] for d in docs if d["kind"] == "CronJob"], [])

    def test_cutover_profiles_match_what_their_cronjobs_carried(self):
        profiles = catalog(self.rendered["production"])["profiles"]
        for name in ("worker-session-check", "worker-llm"):
            self.assertIn("db", profiles[name]["secrets"])
        self.assertEqual(set(profiles["worker-session-check"]["secrets"]), {"db", "espn", "notify", "league-config"})
        self.assertNotIn("espn", profiles["worker-llm"]["secrets"])
        self.assertIn("llm", profiles["worker-llm"]["secrets"])
        jobs = catalog(self.rendered["production"])["jobs"]
        for name in (*TUESDAY_STEPS, "trade-candidates"):
            self.assertEqual(jobs[name]["profile"], "core", name)
        # The scan is the only browser bot that calls an LLM.
        self.assertEqual(jobs["trade-inbox"]["profile"], "worker")
        self.assertNotIn("llm", profiles["worker"]["secrets"])
        self.assertEqual(jobs["trade-scan"]["profile"], "worker-scan")
        self.assertIn("llm", profiles["worker-scan"]["secrets"])
        self.assertEqual(jobs["trade-submit"]["profile"], "worker-submit")
        self.assertNotIn("llm", profiles["worker-submit"]["secrets"])
        # The digest and the inbox publish job_events facts (the amqp bundle's AMQP_URL).
        self.assertEqual(jobs["rumor-digest-bot"]["profile"], "worker-llm")
        for name in ("worker-llm", "worker"):
            self.assertIn("amqp", profiles[name]["secrets"], name)

    def test_only_the_profiles_whose_bots_call_core_name_the_core_url_bundle(self):
        # The trade bots (inbox, scan, submit) fetch from the core API; the session check, the
        # digest and the draft connector never do.
        profiles = catalog(self.rendered["production"])["profiles"]
        named = {name for name, profile in profiles.items() if "core-url" in profile["secrets"]}
        self.assertEqual(named, {"worker", "worker-scan", "worker-submit"})

    def test_only_the_frequent_syncers_run_inline_with_the_launchers_env(self):
        # Inline runs get the launcher's env, not their profile's secrets.
        for environment, docs in self.rendered.items():
            with self.subTest(environment=environment):
                jobs = catalog(docs)["jobs"]
                inline = {name for name, job in jobs.items() if job.get("executor") == "inline"}
                self.assertEqual(inline, {"rumor-syncer", "rapid-status-syncer"})
                (container,) = one(docs, "Deployment", "fantasy-job-launcher")["spec"]["template"]["spec"]["containers"]
                for name in ("DB_HOST", "DB_PASSWORD", "REDIS_HOST", "AMQP_URL"):
                    self.assertIn(name, env(container), name)

    def test_rumor_actions_run_on_digest_events(self):
        for environment, docs in self.rendered.items():
            with self.subTest(environment=environment):
                job = catalog(docs)["jobs"]["rumor-action-trigger"]
                self.assertEqual(job["on_event"], [{"key": "rumor.signal_created", "debounce": "30m"}])
                # A dead pod holds the job for its deadline plus the watcher's 10m orphan grace,
                # which must stay under the shortest wait queued behind it: an event's 30m.
                self.assertEqual(job["deadline"], "15m")

    def test_gateway_mounts_the_same_catalog(self):
        # The Jobs API reads the catalog from the gateway pod; without it manual runs answer 503.
        for environment, docs in self.rendered.items():
            with self.subTest(environment=environment):
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

    def test_draft_jobs_are_cap_exempt_services_and_practice_runs_retire_on_a_lapsed_heartbeat(self):
        content = catalog(self.rendered["production"])
        self.assertEqual(content["global"]["max_service_deadline"], "8h")
        jobs = content["jobs"]
        for name in DRAFT_JOBS:
            with self.subTest(job=name):
                job = jobs[name]
                self.assertEqual(job["class"], "service")
                self.assertNotIn("schedules", job)
                # The draft API finds a run again by this param.
                self.assertIn("league_id", job["params"])
        for name in ("practice-draft-listener", "practice-draft-connector"):
            self.assertEqual(jobs[name]["health"], {"key": "draft:{league_id}:health", "grace": "10m"})
            self.assertEqual(jobs[name]["alert"], "none")
        for name in ("draft-listener", "draft-connector"):
            self.assertNotIn("health", jobs[name])
            self.assertEqual(jobs[name]["alert"], "critical")
        self.assertEqual(jobs["practice-draft-connector"]["param_env"]["room_url"], "DRAFT_ROOM_URL")
        self.assertEqual(jobs["draft-listener"]["param_env"],
                         {"mode": "DRAFT_MODE", "pick_clock_seconds": "DRAFT_PICK_CLOCK_SECONDS",
                          "auto_draft_delay_seconds": "AUTO_DRAFT_DELAY_SECONDS"})
        # The connector's pods carried only the ESPN cookies, league ids and Redis.
        self.assertEqual(set(content["profiles"]["worker-draft"]["secrets"]), {"espn", "redis", "league-config"})
        self.assertNotIn("serviceAccount", content["profiles"]["worker-draft"])

    def test_live_games_is_a_scheduled_service_that_retires_on_a_lapsed_heartbeat(self):
        job = catalog(self.rendered["production"])["jobs"]["live-games"]
        self.assertEqual(job["class"], "service")
        self.assertEqual(job["command"], ["python", "-m", "src.ingestion.live_games"])
        self.assertEqual(job["health"], {"key": "live:health", "grace": "10m"})
        self.assertEqual(job["deadline"], "8h")
        # One run per game window: the poller exits itself, so the schedule only starts it.
        self.assertTrue(all(s["tz"] == "America/Chicago" and s["seasons"] == ["regular"]
                            for s in job["schedules"]))
        self.assertEqual({s["cron"] for s in job["schedules"]},
                         {"30 18 * * 4", "30 8,15 * * 0", "30 17 * * 1"})

    def test_gateway_only_reads_jobs_and_pods(self):
        # The launcher creates and deletes draft Jobs now; the gateway reads their phase and logs.
        for environment, docs in self.rendered.items():
            with self.subTest(environment=environment):
                role = one(docs, "Role", "draft-sync-orchestrator")
                rules = {(tuple(r["apiGroups"]), tuple(r["resources"])): sorted(r["verbs"]) for r in role["rules"]}
                self.assertEqual(rules, {
                    (("batch",), ("jobs",)): ["get", "list"],
                    (("",), ("pods",)): ["get", "list", "watch"],
                    (("",), ("pods/log",)): ["get"],
                })
                (gateway,) = one(docs, "Deployment", "fantasy-core-pipeline")["spec"]["template"]["spec"]["containers"]
                for name in ("DRAFT_SYNC_IMAGE", "BROWSER_WORKER_IMAGE", "DRAFT_SYNC_ENV_SECRET", "DRAFT_SYNC_ENV_CONFIGMAP"):
                    self.assertNotIn(name, env(gateway))

    def test_rbac_is_namespaced_and_minimal(self):
        docs = self.rendered["production"]
        role = one(docs, "Role", "fantasy-job-launcher")
        rules = {
            (tuple(r["apiGroups"]), tuple(r["resources"]), tuple(r.get("resourceNames", ()))): sorted(r["verbs"])
            for r in role["rules"]
        }
        self.assertEqual(rules, {
            (("batch",), ("jobs",), ()): ["create", "delete", "get", "list", "watch"],
            (("",), ("pods",), ()): ["get", "list"],
            # Leader election: only the launcher's own Lease can be read or renewed.
            (("coordination.k8s.io",), ("leases",), ()): ["create"],
            (("coordination.k8s.io",), ("leases",), ("fantasy-job-launcher",)): ["get", "update"],
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
