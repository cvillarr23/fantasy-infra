# fantasy-football-infra

This GitOps infrastructure repository manages the Kubernetes deployments, network services, stateful stores, and auto-scaling definitions for the automated fantasy football pipeline.

## Repository Layout

*   `base/`: Core infrastructure configurations agnostic of operational scope.
    *   `databases/`: Manifests for low-memory PostgreSQL/TimescaleDB, Redis, and RabbitMQ.
    *   `core-pipeline/`: Deployment states for the FastAPI Engine and Weekly CronJobs.
    *   `browser-worker/`: KEDA ScaledJob criteria definitions for Playwright containers.
    *   `grafana/`: Analytics layout metrics configurations.
*   `overlays/`: Environmental delta configurations.
    *   `production/`: Active operational values, sync targets, and persistent credentials.
    *   `draft-day/`: CPU burst and memory caching patches applied specifically during the live draft window.

## Cluster Pre-requisites

Your Kubernetes node must have the following system resources and cluster operators installed prior to applying these configurations:
1.  **KEDA (Kubernetes Event-driven Autoscaling)**: Installed in your cluster to scale Playwright pods via message metrics.
2.  **StorageClass Configured**: A default local-path or hostpath storage provider to satisfy database PersistentVolumeClaims (PVC).

## Quickstart Deployment

To spin up the base automation services on your local node, run the following:

```bash
kubectl apply -k overlays/production/
```
