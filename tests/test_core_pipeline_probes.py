"""Keep the FastAPI gateway's probes tolerant of a CPU-saturated node."""

from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class CorePipelineProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateways = {}
        for overlay in ("production", "draft-day"):
            rendered = subprocess.check_output(
                ["kubectl", "kustomize", str(ROOT / "overlays" / overlay)], text=True
            )
            (deployment,) = [
                doc for doc in yaml.safe_load_all(rendered)
                if doc and doc.get("kind") == "Deployment"
                and doc["metadata"]["name"] == "fantasy-core-pipeline"
            ]
            (cls.gateways[overlay],) = deployment["spec"]["template"]["spec"]["containers"]

    def test_liveness_checks_no_dependencies(self):
        # /v1/health checks Postgres and RabbitMQ; a blip in either must not restart the API.
        for overlay, gateway in self.gateways.items():
            with self.subTest(overlay=overlay):
                self.assertEqual(gateway["livenessProbe"]["httpGet"]["path"], "/v1/livez")
                self.assertEqual(gateway["readinessProbe"]["httpGet"]["path"], "/v1/health")

    def test_probe_timeouts_survive_a_busy_node(self):
        # The node is shared with dev workloads; a 1s default timeout starved the probe.
        for overlay, gateway in self.gateways.items():
            with self.subTest(overlay=overlay):
                self.assertGreaterEqual(gateway["livenessProbe"]["timeoutSeconds"], 5)
                self.assertGreaterEqual(gateway["readinessProbe"]["timeoutSeconds"], 3)

    def test_cpu_request_buys_a_fair_share_under_contention(self):
        for overlay, gateway in self.gateways.items():
            with self.subTest(overlay=overlay):
                cpu = gateway["resources"]["requests"]["cpu"]
                self.assertGreaterEqual(int(cpu.rstrip("m")), 250)


if __name__ == "__main__":
    unittest.main()
