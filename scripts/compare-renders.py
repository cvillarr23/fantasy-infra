#!/usr/bin/env python3
"""Fails when the Helm chart and the kustomize overlays render different manifests.

Argo CD deploys overlays/production until it switches to charts/fantasy. Until then both must
produce the same objects, so the switch changes nothing in the cluster: a resource the chart
renamed or dropped would be pruned, and for the Postgres StatefulSet that loses the database.

Two differences are expected and normalised away:
- kustomize's configMapGenerator suffixes fantasy-job-catalog with a content hash; the chart
  uses the plain name and a checksum/catalog pod annotation to roll pods instead.
- the chart's checksum/* annotations themselves.

Usage: compare-renders.py [overlay ...]   (default: production draft-day)
"""

import difflib
from pathlib import Path
import re
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "fantasy"
# Each overlay and the chart values files that reproduce it. values-images.yaml holds the same
# digests as the production kustomization, so an image bump to only one of them fails here.
OVERLAY_VALUES = {
    "production": ["values-production.yaml", "values-images.yaml"],
    "draft-day": ["values-production.yaml", "values-draft-day.yaml", "values-images.yaml"],
}
GENERATED_CATALOG = re.compile(r"\bfantasy-job-catalog-[a-z0-9]{10}\b")


def main(argv):
    overlays = argv or list(OVERLAY_VALUES)
    failures = [overlay for overlay in overlays if not _matches(overlay)]
    if failures:
        print(f"Chart render differs from overlays: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"Chart render matches overlays: {', '.join(overlays)}")
    return 0


def _matches(overlay):
    expected = _by_key(_kustomize(overlay))
    actual = _by_key(_helm(OVERLAY_VALUES[overlay]))
    problems = diff(expected, actual)
    for problem in problems:
        print(f"[{overlay}] {problem}", file=sys.stderr)
    return not problems


def diff(expected, actual):
    """Problems between two {key: object} renders: missing, extra and changed objects."""
    problems = [f"missing from the chart: {_label(key)}" for key in sorted(expected.keys() - actual.keys())]
    problems += [f"only in the chart: {_label(key)}" for key in sorted(actual.keys() - expected.keys())]
    for key in sorted(expected.keys() & actual.keys()):
        if expected[key] != actual[key]:
            problems.append(f"differs: {_label(key)}\n{_unified(expected[key], actual[key])}")
    return problems


def _kustomize(overlay):
    out = subprocess.check_output(["kubectl", "kustomize", str(ROOT / "overlays" / overlay)], text=True)
    return [_without_checksums(doc) for doc in _docs(GENERATED_CATALOG.sub("fantasy-job-catalog", out))]


def _helm(values_files):
    command = ["helm", "template", "fantasy", str(CHART), "--namespace", "default"]
    for name in values_files:
        command += ["-f", str(CHART / name)]
    return [_without_checksums(doc) for doc in _docs(subprocess.check_output(command, text=True))]


def _docs(text):
    return [doc for doc in yaml.safe_load_all(text) if doc]


def _without_checksums(doc):
    """Drops checksum/* annotations from the object and its pod template."""
    for metadata in (doc.get("metadata"), doc.get("spec", {}).get("template", {}).get("metadata")):
        annotations = (metadata or {}).get("annotations") or {}
        for name in [name for name in annotations if name.startswith("checksum/")]:
            del annotations[name]
        if metadata and "annotations" in metadata and not annotations:
            del metadata["annotations"]
    return doc


def _by_key(docs):
    return {(doc["kind"], doc["metadata"].get("namespace", ""), doc["metadata"]["name"]): doc for doc in docs}


def _label(key):
    kind, namespace, name = key
    return f"{kind} {namespace + '/' if namespace else ''}{name}"


def _unified(expected, actual):
    lines = difflib.unified_diff(
        yaml.safe_dump(expected, sort_keys=True).splitlines(),
        yaml.safe_dump(actual, sort_keys=True).splitlines(),
        "kustomize", "helm", lineterm="")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
