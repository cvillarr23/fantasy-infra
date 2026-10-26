---
title: Infrastructure
description: Deploy, connect to, and operate the fantasy platform on Kubernetes.
sidebar_position: 1
slug: /
---

Argo CD deploys this repository's Kubernetes manifests. Start here to trace a release, understand access boundaries, or investigate a scheduled workload.

```mermaid
flowchart LR
    Git[Infra Git repository] -->|production overlay| Argo[Argo CD]
    Argo -->|reconcile| Apps[API and UI]
    Argo -->|templates| Jobs[CronJobs and KEDA workers]
    Bao[OpenBao] -->|External Secrets Operator| Secrets[Kubernetes Secrets]
    Secrets -->|credentials| Apps
    Secrets -->|credentials| Jobs
    Apps -->|data and actions| Stores[(Postgres, Redis, RabbitMQ)]
    Jobs -->|data and actions| Stores
```

| You need to… | Read |
|---|---|
| Trace a merge and verify image digests | [Deployment](deployment.md) |
| Find ports, authentication boundaries and DNS ownership | [Networking](networking.md) |
| Back up, restore or inspect database access | [Databases](databases.md) |
| Look up every CronJob and its alert | [Schedules](schedules.md) |
| Recover a failed job, session, queue or release | [Operations](operations.md) |
| Understand how jobs start and why CronJobs are going away | [ADR 0001: Unified job launcher](adr/0001-unified-job-launcher.md) |
| Understand credential delivery and rotation | [Secrets](secrets.md) |

:::note[Repository boundary]
Shared controllers, public DNS, tunnel routing and telemetry services live in the cluster repository. This repository owns application workloads, stores, secret references, dashboards and alert definitions.
:::
