#!/usr/bin/env bash
# Forces an immediate image rollout instead of waiting out Argo CD Image Updater's
# configured poll interval (--interval on the updater Deployment). Runs a real (non-dry-run)
# update cycle in the existing updater pod, then waits for Argo to reconcile whatever it
# wrote back to git.
#
# The one-shot reuses the pod's mounted registries.conf and git SSH credentials, and
# leaves the long-running loop untouched.
set -euo pipefail

ARGOCD_NS="${ARGOCD_NS:-argocd}"
APP="${APP:-fantasy-infra}"
WAIT_SECS="${WAIT_SECS:-120}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl is required." >&2
  exit 1
fi

echo "==> Running a one-shot image update cycle"
# --health-port=0 --metrics-port=0 are required: the long-running process in this pod
# already holds those ports, and a one-shot that tries to bind them dies on startup.
#
# The output is captured rather than piped so the updater's exit status is preserved;
# piping through grep would hand back grep's status and hide a failed exec.
updater_log="$(mktemp)"
trap 'rm -f "$updater_log"' EXIT
updater_rc=0
kubectl exec -n "$ARGOCD_NS" deployment/argocd-image-updater -- \
  argocd-image-updater run --once --health-port=0 --metrics-port=0 >"$updater_log" 2>&1 || updater_rc=$?
grep -vE 'operation_name=|git config|Trace args|informer' "$updater_log" || true
if ((updater_rc != 0)); then
  echo "ERROR: the image update cycle failed (exit $updater_rc); see the output above." >&2
  exit "$updater_rc"
fi

echo
echo "==> Waiting up to ${WAIT_SECS}s for $APP to reconcile"
# Comparing against remote HEAD, not just Synced/Healthy: the updater has just pushed a
# commit, and Argo still reports Synced/Healthy against the *previous* revision until it
# refreshes. Waiting on status alone returns instantly and reports success for the old code.
expected=""
if command -v git >/dev/null 2>&1; then
  expected="$(git ls-remote https://github.com/cvillarr23/fantasy-infra.git main 2>/dev/null | cut -f1 || true)"
  if [[ -z "$expected" ]]; then
    echo "WARNING: could not resolve remote HEAD; falling back to status-only wait." >&2
  fi
else
  echo "WARNING: git is not installed, so the target revision is unknown; falling back to a" >&2
  echo "         status-only wait, which can report success against the previous revision." >&2
fi
echo "Target revision ${expected:0:8}"

# Ask Argo to re-read git now rather than waiting out its own poll interval.
kubectl patch application "$APP" -n "$ARGOCD_NS" --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"normal"}}}' >/dev/null 2>&1 || true

deadline=$((SECONDS + WAIT_SECS))
while ((SECONDS < deadline)); do
  sync="" health="" revision=""
  read -r sync health revision < <(
    kubectl get application "$APP" -n "$ARGOCD_NS" \
      -o jsonpath='{.status.sync.status} {.status.health.status} {.status.sync.revision}' 2>/dev/null
  ) || true
  if [[ "${sync:-}" == "Synced" && "${health:-}" == "Healthy" ]] &&
     [[ -z "$expected" || "${revision:-}" == "$expected" ]]; then
    echo "Synced/Healthy at ${revision:0:8}"
    break
  fi
  sleep 5
done

if [[ "${sync:-}" != "Synced" || "${health:-}" != "Healthy" ]]; then
  echo "WARNING: $APP is ${sync:-unknown}/${health:-unknown} after ${WAIT_SECS}s." >&2
elif [[ -n "$expected" && "${revision:-}" != "$expected" ]]; then
  echo "WARNING: $APP is Synced at ${revision:0:8}, not the expected ${expected:0:8}." >&2
fi

echo
echo "==> Images actually running"
# These two env values name the images core-pipeline pulls when it spawns draft Jobs at
# runtime. They are the ones that historically drifted, so they are worth printing even
# when everything reports Synced - a healthy Deployment says nothing about them.
kubectl get deployment fantasy-core-pipeline -n default -o jsonpath='{range .spec.template.spec.containers[*]}{.name}: {.image}{"\n"}{end}' 2>/dev/null || true
kubectl get deployment fantasy-core-pipeline -n default \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="DRAFT_SYNC_IMAGE")]}DRAFT_SYNC_IMAGE: {.value}{"\n"}{end}{range .spec.template.spec.containers[0].env[?(@.name=="BROWSER_WORKER_IMAGE")]}BROWSER_WORKER_IMAGE: {.value}{"\n"}{end}' 2>/dev/null || true
