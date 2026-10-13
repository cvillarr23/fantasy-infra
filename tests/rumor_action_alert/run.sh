#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Under $RUNNER_TEMP in CI: on the self-hosted runners Docker runs in a
# separate dind container that only shares the job workspace, so a
# plain /tmp dir would bind-mount as empty. Falls back to /tmp locally.
WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/work.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
python3 "$HERE/render_test.py" > "$WORK/tests.sql"
chmod 755 "$WORK"
docker run --rm --network none --user postgres -v "$WORK:/work:ro" \
  postgres:16 bash -ceu '
    initdb -D /tmp/pgdata -A trust >/dev/null
    pg_ctl -D /tmp/pgdata -o "-k /tmp" -w start >/dev/null
    psql -h /tmp -v ON_ERROR_STOP=1 -f /work/tests.sql
  '
