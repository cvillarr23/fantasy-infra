#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLAYS=("overlays/production" "overlays/draft-day")
REQUIRED_ENVS=("DRAFT_SYNC_IMAGE" "DRAFT_SYNC_ENV_SECRET" "DRAFT_SYNC_ENV_CONFIGMAP")

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl is required for preflight checks." >&2
  exit 1
fi

for overlay in "${OVERLAYS[@]}"; do
  echo "==> Validating ${overlay}"
  manifest_file="$(mktemp)"
  kubectl kustomize "${ROOT_DIR}/${overlay}" > "${manifest_file}"

  if ! python3 - "${manifest_file}" "${overlay}" "${REQUIRED_ENVS[@]}" <<'PY'
import sys

try:
    import yaml
except ModuleNotFoundError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

manifest_file = sys.argv[1]
overlay = sys.argv[2]
required_envs = sys.argv[3:]
with open(manifest_file, "r", encoding="utf-8") as f:
    docs = [d for d in yaml.safe_load_all(f.read()) if isinstance(d, dict)]

deployment = next(
    (
        d
        for d in docs
        if d.get("kind") == "Deployment"
        and d.get("metadata", {}).get("name") == "fantasy-core-pipeline"
    ),
    None,
)

if deployment is None:
    print(
        f"ERROR [{overlay}]: Deployment/fantasy-core-pipeline not found in rendered manifest.",
        file=sys.stderr,
    )
    sys.exit(1)

containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
gateway = next((c for c in containers if c.get("name") == "gateway"), None)

if gateway is None:
    print(f"ERROR [{overlay}]: gateway container not found in Deployment.", file=sys.stderr)
    sys.exit(1)

env_list = gateway.get("env", [])
env_by_name = {
    e.get("name"): e
    for e in env_list
    if isinstance(e, dict) and e.get("name")
}

errors = []
for name in required_envs:
    env_entry = env_by_name.get(name)
    if env_entry is None:
        errors.append(f"missing env var {name}")
        continue
    value = env_entry.get("value")
    has_value_from = "valueFrom" in env_entry and env_entry.get("valueFrom") is not None
    if (value is None or str(value).strip() == "") and not has_value_from:
        errors.append(f"env var {name} is empty")

cfg_name = env_by_name.get("DRAFT_SYNC_ENV_CONFIGMAP", {}).get("value", "").strip()
sec_name = env_by_name.get("DRAFT_SYNC_ENV_SECRET", {}).get("value", "").strip()

if cfg_name:
    cfg_exists = any(
        d.get("kind") == "ConfigMap" and d.get("metadata", {}).get("name") == cfg_name
        for d in docs
    )
    if not cfg_exists:
        errors.append(
            f"DRAFT_SYNC_ENV_CONFIGMAP={cfg_name} does not match any ConfigMap in rendered {overlay}"
        )

if sec_name:
    secret_exists = any(
        d.get("kind") == "Secret" and d.get("metadata", {}).get("name") == sec_name
        for d in docs
    )
    externalsecret_target_exists = any(
        d.get("kind") == "ExternalSecret"
        and d.get("spec", {}).get("target", {}).get("name") == sec_name
        for d in docs
    )
    if not (secret_exists or externalsecret_target_exists):
        errors.append(
            f"DRAFT_SYNC_ENV_SECRET={sec_name} does not match a Secret or ExternalSecret target in rendered {overlay}"
        )

if errors:
    for err in errors:
        print(f"ERROR [{overlay}]: {err}", file=sys.stderr)
    sys.exit(1)

print(f"OK [{overlay}]")
PY
  then
    rm -f "${manifest_file}"
    exit 1
  fi
  rm -f "${manifest_file}"
done

echo "Draft-sync env preflight passed for all overlays."
