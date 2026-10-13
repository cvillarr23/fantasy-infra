#!/usr/bin/env bash
# Unit-test base/telemetry's alert expressions with promtool (in Docker, no install).
# Renders the committed Grafana rules to a Prometheus rule file, then runs tests.yaml.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# >= 3.15 for test groups' start_timestamp (real Tuesday America/Chicago epochs).
IMAGE="${PROMTOOL_IMAGE:-prom/prometheus:v3.15.0}"
# Under $RUNNER_TEMP in CI: on the self-hosted runners Docker runs in a
# separate dind container that only shares the job workspace, so a
# plain /tmp dir would bind-mount as empty. Falls back to /tmp locally.
WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/work.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

python3 "$HERE/render_rules.py" > "$WORK/rules.yaml"
cp "$HERE/tests.yaml" "$WORK/tests.yaml"
docker run --rm --user "$(id -u):$(id -g)" -v "$WORK:/work:ro" -w /work \
  --entrypoint promtool "$IMAGE" test rules tests.yaml
