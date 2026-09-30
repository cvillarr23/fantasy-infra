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


def broker_pod_storage():
    """The rabbitmq container's broker-data mount plus the pod-level settings that shape it,
    as rendered for production - so the restart scenario tests what Argo CD would apply."""
    manifests = yaml.safe_load_all(
        command("kubectl", "kustomize", str(ROOT / "overlays/production"))
    )
    deployment = next(
        item
        for item in manifests
        if item.get("kind") == "Deployment"
        and item.get("metadata", {}).get("name") == "fantasy-broker"
    )
    spec = deployment["spec"]["template"]["spec"]
    rabbitmq = next(c for c in spec["containers"] if c["name"] == "rabbitmq")
    mount = next(m for m in rabbitmq["volumeMounts"] if m["name"] == "broker-data")
    return {
        "mount_path": mount["mountPath"],
        "sub_path": mount.get("subPath"),
        "hostname": spec["hostname"],
        "fs_group": spec.get("securityContext", {}).get("fsGroup"),
    }


def apply_fs_group(volume, fs_group):
    """What kubelet does to the whole volume on every mount under fsGroup with the default
    fsGroupChangePolicy: group-own everything, add group rw, setgid directories."""
    if fs_group is None:
        return
    command(
        "docker", "run", "--rm", "-v", f"{volume}:/data", "alpine:3", "sh", "-c",
        f"chgrp -R {fs_group} /data && chmod -R g+rwX /data"
        " && find /data -type d -exec chmod g+s {} +",
    )


def exercise_restart(migrate=False):
    """fantasy-infra#103: the first boot on a fresh PVC always passed, but kubelet re-applied
    fsGroup on the next mount, left .erlang.cookie group-readable, and RabbitMQ refused to
    start ever again. Boot, persist a message, restart through a simulated fsGroup pass, and
    require the broker to come back with the message.

    migrate=True makes the first boot use the pre-#103 layout (all of /var/lib/rabbitmq on
    the volume), so the second boot proves the live PVC's queued messages carry over."""
    storage = broker_pod_storage()
    first = (
        {**storage, "mount_path": "/var/lib/rabbitmq", "sub_path": None}
        if migrate
        else storage
    )
    with tempfile.TemporaryDirectory(prefix="fantasy-issue-103-") as scratch:
        directory = Path(scratch)
        rendered_definitions(directory)
        volume = f"fantasy-issue-103-{uuid.uuid4().hex[:10]}"
        command("docker", "volume", "create", volume)

        def mount_for(layout):
            mount = f"type=volume,src={volume},dst={layout['mount_path']}"
            if layout["sub_path"]:
                # kubelet creates a missing subPath directory; docker requires it to exist.
                command(
                    "docker", "run", "--rm", "-v", f"{volume}:/data", "alpine:3",
                    "mkdir", "-p", f"/data/{layout['sub_path']}",
                )
                mount += f",volume-subpath={layout['sub_path']}"
            return mount

        def boot(layout):
            mount = mount_for(layout)
            apply_fs_group(volume, layout["fs_group"])
            container = f"fantasy-issue-103-{uuid.uuid4().hex[:10]}"
            containers.append(container)
            # No --rm: a broker that crashes at boot must stay around for `docker logs`.
            command(
                "docker", "run", "-d", "--name", container,
                "--hostname", layout["hostname"],
                "-p", "127.0.0.1::5672",
                "--mount", mount,
                "-v", f"{directory / 'definitions.json'}:/etc/rabbitmq/definitions.json:ro",
                "-v", f"{directory / 'load_definitions.conf'}:/etc/rabbitmq/conf.d/20-load_definitions.conf:ro",
                "rabbitmq:3-management-alpine",
            )
            try:
                port = command("docker", "port", container, "5672/tcp").rsplit(":", 1)[1]
                return container, wait_for_connection(
                    f"amqp://contract:{PASSWORD}@127.0.0.1:{port}/%2f"
                )
            except Exception as error:
                logs = subprocess.run(
                    ["docker", "logs", container], capture_output=True, text=True
                )
                raise AssertionError(
                    f"broker boot #{len(containers)} did not come up: {error}\n"
                    f"{(logs.stdout + logs.stderr)[-1500:]}"
                ) from error

        containers = []
        try:
            container, broker = boot(first)
            channel = broker.channel()
            channel.confirm_delivery()
            channel.basic_publish(
                exchange="",
                routing_key="waiver_actions",
                body=json.dumps({"player_id": "survives-restart"}),
                properties=pika.BasicProperties(delivery_mode=2),
            )
            broker.close()
            command("docker", "stop", container)

            container, broker = boot(storage)
            method, _, body = broker.channel().basic_get(
                queue="waiver_actions", auto_ack=True
            )
            assert method is not None, "persisted message was lost across the restart"
            assert json.loads(body)["player_id"] == "survives-restart"
            broker.close()
        finally:
            for name in containers:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, check=False)


if __name__ == "__main__":
    selected = sys.argv[1:] or ["fresh", "existing", "existing_args", "restart", "migrate"]
    for name in selected:
        if name in ("restart", "migrate"):
            exercise_restart(migrate=name == "migrate")
            print(f"{name}: broker survived an fsGroup remount with its data", flush=True)
        else:
            exercise(name)
            print(f"{name}: normal delivery and dead-letter routing passed", flush=True)
