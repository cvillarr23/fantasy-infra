"""The homelab manifests as Argo CD renders them from charts/fantasy."""

from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "fantasy"
# Each homelab environment and the value files Argo CD layers for it, in homelab-deployment's
# argo-controllers/fantasy-infra.yaml order.
ENVIRONMENTS = {
    "production": ["values-production.yaml", "values-images.yaml"],
    "draft-day": ["values-production.yaml", "values-draft-day.yaml", "values-images.yaml"],
}


def render_text(environment="production"):
    command = ["helm", "template", "fantasy-infra", str(CHART), "--namespace", "default"]
    for name in ENVIRONMENTS[environment]:
        command += ["-f", str(CHART / name)]
    return subprocess.check_output(command, text=True)


def render(environment="production"):
    return [doc for doc in yaml.safe_load_all(render_text(environment)) if doc]
