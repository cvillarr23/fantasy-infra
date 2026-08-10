# GitHub Issues Template Backlog: fantasy-football-infra

This document serves as the project backlog for the infrastructure layer. These items are ready to be converted into GitHub Issues, complete with prioritization tags and tracking scopes.

---

## [INFRA-01] Provision Redis and RabbitMQ Broker Workloads
*   **Priority**: High 🔥
*   **Milestone**: Phase 1 - Core Cluster Foundations
*   **Tags**: `area/database`, `component/broker`, `type/feature`

### Description
Implement the core cache and message routing deployments inside the stateful database layer (`base/databases/`). Redis will capture sub-millisecond transaction updates during live draft day processing, while RabbitMQ manages asynchronous event passing between the ingestion pipelines and Playwright execution workers.

### Technical Tasks
- [ ] Create `base/databases/redis-deployment.yaml` with a 256MiB memory limit constraint.
- [ ] Create `base/databases/rabbitmq-deployment.yaml` configuring default exchanges and tracking queues (`waiver_actions`, `lineup_updates`, `trade_proposals`).
- [ ] Update `base/databases/kustomization.yaml` to include both resource configurations.
- [ ] Expose both deployments inside the cluster using lightweight headless services.

---

## [INFRA-02] Configure FastAPI and MCP Gateway Manifests
*   **Priority**: High 🔥
*   **Milestone**: Phase 1 - Core Cluster Foundations
*   **Tags**: `area/networking`, `component/gateway`, `type/feature`

### Description
Create the infrastructure manifests required to deploy the FastAPI control plane and Model Context Protocol (MCP) server. This container acts as the bridge connecting your external network inputs, local relational database states, and free cloud LLM tool hooks.

### Technical Tasks
- [ ] Define `base/core-pipeline/fastapi-deployment.yaml` tracking the standard port bindings.
- [ ] Implement a Kubernetes Service to expose the control gateway internally to the worker pods.
- [ ] Configure environment variable bindings routing database connection strings securely from Kustomize secrets.
- [ ] Set resource bounds keeping memory usage clamped at 256MiB requests and 512MiB limits.

---

## [INFRA-03] Establish Weekly Analytics and Ingestion CronJob Schedule
*   **Priority**: Medium ⚡
*   **Milestone**: Phase 2 - In-Season Operations
*   **Tags**: `area/cron`, `component/ingestion`, `type/feature`

### Description
Provision the ephemeral Kubernetes CronJob definitions that run the data scraping utilities. This infrastructure handles waking up python execution pipelines on fixed schedules to load updated advanced stats (`nflverse`) and structural league tables (`espn-api`).

### Technical Tasks
- [ ] Create `base/core-pipeline/weekly-cronjob.yaml`.
- [ ] Configure the Tuesday 04:00 AM EST schedule string (`0 4 * * 2`) to trigger heavy metrics parsing.
- [ ] Configure the Thursday/Sunday pre-game lock check schedules.
- [ ] Ensure `restartPolicy: OnFailure` is enforced alongside `concurrencyPolicy: Forbid`.

---

## [INFRA-04] Deploy KEDA ScaledJob System for Playwright Worker Isolation
*   **Priority**: High 🔥
*   **Milestone**: Phase 3 - Execution Layer
*   **Tags**: `area/scaling`, `component/worker`, `type/feature`

### Description
Implement a KEDA (Kubernetes Event-driven Autoscaling) `ScaledJob` or `ScaledObject` profile targeting your RabbitMQ deployment. This ensures that the heavy `fantasy-browser-worker` (which bundles a memory-intensive Chromium execution frame) stays locked at exactly `0` replicas, scaling to `1` instantly only when transaction messages hit the queue.

### Technical Tasks
- [ ] Verify KEDA CRDs are active in the local home-lab cluster.
- [ ] Draft `base/browser-worker/keda-scaledjob.yaml`.
- [ ] Configure the RabbitMQ trigger target tracking the depth of the active action queue.
- [ ] Set strict resource allocation allowances ensuring a spawning Playwright pod does not breach a 1200MiB RAM boundary.

---

## [INFRA-05] Integrate Grafana Real-Time Visualization Telemetry
*   **Priority**: Medium ⚡
*   **Milestone**: Phase 4 - Telemetry & Visibility
*   **Tags**: `area/telemetry`, `component/dashboard`, `type/feature`

### Description
Spin up a low-footprint Grafana monitoring node into the cluster environment. It must connect to the PostgreSQL StatefulSet instance to visually present shifting VORP calculations, roster-deficit league maps, and historic automated worker execution metrics.

### Technical Tasks
- [ ] Deploy `base/grafana/grafana-deployment.yaml` tuned down to run on a 150MiB resource boundary.
- [ ] Establish a local service map to access the analytical dashboard interface outside the node grid.
- [ ] Provision automated datasource provisioning linking Grafana to the `fantasy_football` database instantly on pod boot.
- [ ] Setup volume storage persistence ensuring customized graphing boards remain saved across engine restarts.

---

## [INFRA-06] Build Draft-Day High-Performance Overlay Patches
*   **Priority**: Low ⏳
*   **Milestone**: Phase 5 - Pre-Season Optimization
*   **Tags**: `area/kustomize`, `component/patch`, `type/optimization`

### Description
Develop the Kustomize resource overrides inside `overlays/draft-day/`. During the high-concurrency window of your live league draft, you will apply this overlay to temporarily allocate maximum CPU burst access to your math solvers, scale up connection pool boundaries, and sustain a permanent streaming worker instance inside the draft room.

### Technical Tasks
- [ ] Write a strategic `strategicMergePatch` targeting the database configuration states to allow larger concurrent processing pools.
- [ ] Apply CPU patch adjustments raising math solver boundaries to use up to 3 cores of your Core i7-7700K during live matrix solving loops.
- [ ] Inject draft room network parameters through localized overlay ConfigMaps.
