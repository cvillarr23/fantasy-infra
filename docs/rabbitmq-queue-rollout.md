# RabbitMQ queue contract and storage rollout

The broker owns the durable topology for `lineup_updates`, `waiver_actions`, and
`draft_targets`. Producers and consumers check that these queues exist without
redeclaring their arguments. A broker policy sends rejected messages to the
existing `dlx` exchange; each queue's original routing key selects its matching
`.dlq`. Existing queues with hardcoded dead-letter arguments continue to use
those arguments because RabbitMQ gives them precedence over a regular policy.

## Operator approval gate

Merging the infra PR can trigger Argo CD to replace the RabbitMQ pod. The current
Deployment has no data volume, so that replacement can discard every message
still in the pod. **Do not merge or sync the infra PR until the operator approves
this gate.** The six live queues had zero ready and zero unacknowledged messages
when inspected on 2026-09-27; that observation must be repeated at rollout.

1. Pause all AMQP publishers and consumers, including the core API, producer
   CronJobs, and browser-worker Jobs. Wait for active Jobs and deliveries to
   finish. Keep them paused through the broker replacement.
2. Run the command below twice, at least one minute apart. Include every queue
   returned, including any `.dlq`. Every `messages_ready` and
   `messages_unacknowledged` value must be zero. If any value is nonzero,
   **stop the rollout and leave the current pod running**. Drain or transfer
   those messages under a separate operator-approved procedure, then repeat
   this gate. Do not delete queues or reset broker data.
3. Approve the infra merge and Argo CD sync. Its `Recreate` strategy starts
   one broker against the new `fantasy-broker-data` PVC, with a stable Erlang
   node hostname. Wait for the PVC to bind and the pod to become ready.
4. Verify that the policy, source queues, DLQs, exchange bindings, and PVC are
   present. Resume consumers, then publishers. If verification fails, keep
   publishers paused; any rollback must continue mounting the PVC once it
   contains messages.

```bash
kubectl exec -n default deploy/fantasy-broker -c rabbitmq -- \
  rabbitmqctl list_queues -p / name messages_ready messages_unacknowledged
kubectl get pvc fantasy-broker-data -n default
kubectl exec -n default deploy/fantasy-broker -c rabbitmq -- \
  rabbitmqctl list_policies -p /
kubectl exec -n default deploy/fantasy-broker -c rabbitmq -- \
  rabbitmqctl list_bindings -p / source_name destination_name routing_key
```

The local integration test uses disposable Docker brokers and the real Python
producer and Node consumer. It covers a fresh definitions import and existing
queues both with and without hardcoded dead-letter arguments, each carrying a
persistent message before policy import. From this repo,
run it after installing the sibling repos' dependencies:

```bash
FANTASY_CORE_ROOT=/path/to/fantasy-core-pipeline \
FANTASY_WORKER_ROOT=/path/to/fantasy-browser-worker \
  /path/to/fantasy-core-pipeline/.venv/bin/python tests/broker_contract.py
```

The test starts and stops its own containers. It never connects to the live
broker.
