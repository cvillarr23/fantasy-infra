#!/usr/bin/env python3
"""Print the launcher container's image from rendered manifests on stdin.

Job catalog CI validates the catalog with this image, the one the launcher actually runs.
Usage: helm template fantasy-infra charts/fantasy -n default -f charts/fantasy/values-production.yaml \
         -f charts/fantasy/values-images.yaml | python3 scripts/launcher-image.py
"""

import sys

import yaml

DEPLOYMENT = "fantasy-job-launcher"
CONTAINER = "launcher"


def _is_launcher(doc):
    return bool(doc) and doc.get("kind") == "Deployment" and doc["metadata"]["name"] == DEPLOYMENT


def main():
    deployment = next(d for d in yaml.safe_load_all(sys.stdin) if _is_launcher(d))
    containers = deployment["spec"]["template"]["spec"]["containers"]
    image = next(c["image"] for c in containers if c["name"] == CONTAINER)
    if "@sha256:" not in image:
        sys.exit(f"{DEPLOYMENT} image is not digest-pinned: {image}")
    print(image)


if __name__ == "__main__":
    main()
