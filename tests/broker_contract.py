"""Exercise the rendered broker topology with the real Python producer and Node consumer."""

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pika
import psycopg2.pool
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = Path(
    os.environ.get("FANTASY_WORKER_ROOT", ROOT.parents[1] / "fantasy-browser-worker")
).resolve()
CORE_ROOT = Path(
    os.environ.get("FANTASY_CORE_ROOT", ROOT.parents[1] / "fantasy-core-pipeline")
).resolve()
PASSWORD = "contract-test-password"
os.environ.setdefault("DB_PASSWORD", "test")
logging.getLogger("pika").setLevel(logging.WARNING)
logging.disable(logging.CRITICAL)


def command(*args, **kwargs):
    return subprocess.run(
        args, check=True, text=True, capture_output=True, **kwargs
    ).stdout.strip()


def rendered_definitions(directory):
    manifests = yaml.safe_load_all(
        command("kubectl", "kustomize", str(ROOT / "overlays/production"))
    )
    configmap = next(
        item
        for item in manifests
        if item.get("kind") == "ConfigMap"
        and item.get("metadata", {}).get("name") == "fantasy-broker-definitions"
    )
    data = configmap["data"]
    template = directory / "definitions.template.json"
    renderer = directory / "render-definitions.py"
    output = directory / "definitions.json"
    template.write_text(data["definitions.template.json"])
    renderer.write_text(data["render-definitions.py"])
    (directory / "load_definitions.conf").write_text(data["load_definitions.conf"])
    env = {
        **os.environ,
        "RABBITMQ_DEFAULT_USER": "contract",
        "RABBITMQ_DEFAULT_PASS": PASSWORD,
        "BROKER_TEMPLATE_PATH": str(template),
        "BROKER_DEFINITIONS_PATH": str(output),
    }
    command(sys.executable, str(renderer), env=env)
    return json.loads(output.read_text())


def wait_for_connection(url):
    for _ in range(60):
        try:
            return pika.BlockingConnection(pika.URLParameters(url))
        except pika.exceptions.AMQPConnectionError:
            time.sleep(0.5)
    raise TimeoutError("RabbitMQ did not accept connections")


def wait_for_line(process, expected, seen, lines):
    if any(expected in line for line in seen):
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Node consumer exited with {process.returncode}")
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(line)
        if expected in line:
            return
    raise TimeoutError(f"Node consumer did not report {expected}")


def publish_alert(url, player_id):
    os.environ["AMQP_URL"] = url
    psycopg2.pool.ThreadedConnectionPool = MagicMock(return_value=MagicMock())
    sys.path.insert(0, str(CORE_ROOT))
    from src.ingestion import sync_players

    sync_players.AMQP_URL = url
    row = {
        "player_id": player_id,
        "name": "Contract Test Player",
        "team": "CHI",
        "injury_status": "Out",
        "is_transition": True,
    }
    sync_players.publish_waiver_alerts([row], rostered_ids={player_id})


def exercise(scenario):
    with tempfile.TemporaryDirectory(prefix="fantasy-issue-92-") as scratch:
        directory = Path(scratch)
        final = rendered_definitions(directory)
        legacy = {**final, "policies": []}
        if scenario == "existing_args":
            legacy["queues"] = [
                (
                    {
                        **item,
                        "arguments": {
                            "x-dead-letter-exchange": "dlx",
                            "x-dead-letter-routing-key": item["name"],
                        },
                    }
                    if item["name"]
                    in {"lineup_updates", "waiver_actions", "draft_targets"}
                    else item
                )
                for item in final["queues"]
            ]
        (directory / "legacy.json").write_text(json.dumps(legacy))
        container = f"fantasy-issue-92-{uuid.uuid4().hex[:10]}"
        boot = "definitions.json" if scenario == "fresh" else "legacy.json"
        process = None
        seen = []
        lines = queue.Queue()
        try:
            command(
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                container,
                "-p",
                "127.0.0.1::5672",
                "-v",
                f"{directory / boot}:/etc/rabbitmq/definitions.json:ro",
                "-v",
                f"{directory / 'load_definitions.conf'}:/etc/rabbitmq/conf.d/20-load_definitions.conf:ro",
                "-v",
                f"{directory / 'definitions.json'}:/tmp/final-definitions.json:ro",
                "rabbitmq:3-management-alpine",
            )
            port = command("docker", "port", container, "5672/tcp").rsplit(":", 1)[1]
            url = f"amqp://contract:{PASSWORD}@127.0.0.1:{port}/%2f"
            broker = wait_for_connection(url)
            channel = broker.channel()

            if scenario == "fresh":
                channel.queue_declare(
                    queue="mismatch_probe",
                    durable=True,
                    arguments={"x-dead-letter-exchange": "dlx"},
                )
                try:
                    channel.queue_declare(queue="mismatch_probe", durable=True)
                    raise AssertionError(
                        "Incompatible active declaration unexpectedly succeeded"
                    )
                except pika.exceptions.ChannelClosedByBroker as error:
                    assert error.reply_code == 406
                channel = broker.channel()
            else:
                channel.confirm_delivery()
                channel.basic_publish(
                    exchange="",
                    routing_key="waiver_actions",
                    body=json.dumps({"player_id": "existing-message"}),
                    properties=pika.BasicProperties(delivery_mode=2),
                )
                command(
                    "docker",
                    "exec",
                    container,
                    "rabbitmqctl",
                    "import_definitions",
                    "/tmp/final-definitions.json",
                )

            policy = command(
                "docker", "exec", container, "rabbitmqctl", "list_policies", "-p", "/"
            )
            assert "fantasy-dead-letter" in policy
            channel.queue_declare(queue="waiver_actions", passive=True)
            channel.queue_declare(queue="waiver_actions.dlq", passive=True)

            js = """
const { initializeQueueConsumer } = require(process.env.WORKER_ROOT + "/src/queue/connection");
const { PermanentError } = require(process.env.WORKER_ROOT + "/src/errors");
initializeQueueConsumer(async (queue, payload) => {
  if (queue !== "waiver_actions") return;
  if (payload.player_id === "permanent") throw new PermanentError("contract test");
  console.log("DELIVERED:" + payload.player_id);
}).then(() => console.log("CONSUMER_READY")).catch(err => {
  console.error(err);
  process.exit(1);
});
"""
            process = subprocess.Popen(
                ["node", "-e", js],
                cwd=WORKER_ROOT,
                env={**os.environ, "AMQP_URL": url, "WORKER_ROOT": str(WORKER_ROOT)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )

            def collect_output():
                for line in process.stdout:
                    lines.put(line)

            threading.Thread(target=collect_output, daemon=True).start()
            wait_for_line(process, "CONSUMER_READY", seen, lines)
            if scenario != "fresh":
                wait_for_line(process, "DELIVERED:existing-message", seen, lines)
            publish_alert(url, f"normal-{scenario}")
            wait_for_line(process, f"DELIVERED:normal-{scenario}", seen, lines)
            publish_alert(url, "permanent")
            deadline = time.monotonic() + 15
            dead = None
            while time.monotonic() < deadline:
                dead, _, body = channel.basic_get(
                    queue="waiver_actions.dlq", auto_ack=False
                )
                if dead is not None:
                    break
                time.sleep(0.1)
            assert dead is not None
            assert json.loads(body)["player_id"] == "permanent"
            channel.basic_ack(dead.delivery_tag)
            broker.close()
        finally:
            if process is not None:
                process.terminate()
                process.wait(timeout=10)
            subprocess.run(
                ["docker", "stop", container], capture_output=True, check=False
            )


if __name__ == "__main__":
    for name in ("fresh", "existing", "existing_args"):
        exercise(name)
        print(f"{name}: normal delivery and dead-letter routing passed", flush=True)
