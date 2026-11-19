#!/usr/bin/env bash
# Copies the manifests the chart ships verbatim from base/ into charts/fantasy/files/.
# Until Argo CD deploys the chart, base/ stays the source: run this after editing the catalog,
# a Grafana alert or dashboard, or the Image Updater manifests, then commit both copies.
# scripts/compare-renders.py fails in CI when they differ.
set -euo pipefail
cd "$(dirname "$0")/.."
files=charts/fantasy/files

cp base/launcher/catalog.yaml "$files/catalog.yaml"
rm -f "$files"/telemetry/*.yaml "$files"/dashboards/*.yaml "$files"/argocd-image-updater/*.yaml
cp base/telemetry/*-alert*.yaml "$files/telemetry/"
cp base/core-pipeline/fantasy-*-dashboard.yaml "$files/dashboards/"
cp base/argocd-image-updater/{rbac,deployment,config,ssh-config}.yaml "$files/argocd-image-updater/"
