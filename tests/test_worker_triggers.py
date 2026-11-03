"""Offline rendered-manifest contract for every browser-worker consumer queue."""
import json
import subprocess
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
QUEUES = {'lineup_updates', 'waiver_actions', 'draft_targets', 'trade_evaluations',
          'trade_negotiations', 'llm_requests', 'trade_submissions', 'trade_responses'}
# Set only on the ScaledJob worker container: trade_builder reads TRADE_DRY_RUN, so DRY_RUN stays
# unset everywhere and no other bot is armed.
TRADE_ARMING = {'TRADE_DRY_RUN': 'false', 'TRADE_SUBMIT_ENABLED': 'true'}
SCALER = ('ScaledJob', 'fantasy-browser-worker-scaler')
LAUNCHER = ('Deployment', 'fantasy-job-launcher')
CATCH_UP = '30m'
WORKER_RESOURCES = {'requests': {'cpu': '50m', 'memory': '256Mi'},
                    'limits': {'cpu': '1000m', 'memory': '1200Mi'}}
# Bundles k8s.BUNDLES in core turns into env; none of them sets a trade arming variable.
BROWSER_BUNDLES = {'db', 'espn', 'notify', 'league-config'}
# The inbox publishes trade.offer_received; amqp sets only AMQP_URL.
INBOX_BUNDLES = BROWSER_BUNDLES | {'amqp'}


def launcher_catalog(docs):
    (volume,) = docs[LAUNCHER]['spec']['template']['spec']['volumes']
    return yaml.load(docs['ConfigMap', volume['configMap']['name']]['data']['catalog.yaml'],
                     Loader=yaml.BaseLoader)


def containers(node):
    if isinstance(node, dict):
        yield from node.get('containers', [])
        for value in node.values():
            yield from containers(value)
    elif isinstance(node, list):
        for value in node:
            yield from containers(value)


def env_names(container):
    return {e['name'] for e in container.get('env', [])}


def scaler_worker(docs):
    (worker,) = docs[SCALER]['spec']['jobTargetRef']['template']['spec']['containers']
    return worker


class WorkerTriggerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.overlays = {}
        for overlay in ('production', 'draft-day'):
            rendered = subprocess.check_output(
                ['kubectl', 'kustomize', str(ROOT / 'overlays' / overlay)], text=True)
            cls.overlays[overlay] = {
                (doc['kind'], doc['metadata']['name']): doc
                for doc in yaml.safe_load_all(rendered) if doc
            }

    def test_all_overlays_cover_consumers(self):
        for overlay, docs in self.overlays.items():
            with self.subTest(overlay=overlay):
                job = docs[SCALER]
                triggers = job['spec']['triggers']
                names = [t['metadata']['queueName'] for t in triggers]
                self.assertEqual(set(names), QUEUES)
                self.assertEqual(len(names), len(QUEUES))
                for trigger in triggers:
                    self.assertEqual(trigger['type'], 'rabbitmq')
                    self.assertEqual(trigger['metadata']['mode'], 'QueueLength')
                    self.assertEqual(trigger['metadata']['value'], '1')
                    self.assertEqual(trigger['authenticationRef']['name'], 'rabbitmq-trigger-auth')

    def test_trade_queues_are_predeclared(self):
        for overlay, docs in self.overlays.items():
            with self.subTest(overlay=overlay):
                broker = docs['ConfigMap', 'fantasy-broker-definitions']
                definitions = json.loads(broker['data']['definitions.template.json'])
                queues = {q['name']: q for q in definitions['queues']}
                for name in ('trade_submissions', 'trade_responses'):
                    self.assertEqual(queues[name], {
                        'name': name, 'vhost': '/', 'durable': True,
                        'auto_delete': False, 'arguments': {},
                    })

    def test_trade_inbox_and_scan_run_from_the_catalog(self):
        for overlay, docs in self.overlays.items():
            with self.subTest(overlay=overlay):
                catalog = launcher_catalog(docs)
                inbox, scan = catalog['jobs']['trade-inbox'], catalog['jobs']['trade-scan']
                self.assertEqual(inbox['command'], ['node', 'src/bots/trade_inbox.js', '--refresh'])
                self.assertEqual(scan['command'], ['node', 'src/bots/trade_scan.js', '--stage'])
                self.assertEqual(inbox['schedules'], [
                    {'cron': '0 8-22/2 * * *', 'tz': 'America/Chicago', 'catch_up': CATCH_UP}])
                self.assertEqual(scan['schedules'], [
                    {'cron': '0 13 * * 3,5', 'tz': 'America/Chicago', 'catch_up': CATCH_UP}])
                self.assertEqual((inbox['deadline'], scan['deadline']), ('11m', '16m'))
                self.assertEqual(inbox['resources'], WORKER_RESOURCES)
                self.assertEqual(scan['resources'], WORKER_RESOURCES)
                self.assertEqual(catalog['defaults']['overlap'], 'forbid')
                for job in (inbox, scan):
                    self.assertNotIn('overlap', job)

    def test_trade_job_profiles_match_the_worker_contract(self):
        for overlay, docs in self.overlays.items():
            with self.subTest(overlay=overlay):
                catalog = launcher_catalog(docs)
                inbox = catalog['profiles'][catalog['jobs']['trade-inbox']['profile']]
                scan = catalog['profiles'][catalog['jobs']['trade-scan']['profile']]
                self.assertEqual(set(inbox['secrets']), INBOX_BUNDLES)
                self.assertEqual(set(scan['secrets']), BROWSER_BUNDLES | {'llm'})
                for profile in (inbox, scan):
                    self.assertEqual(profile['image'], 'WORKER_IMAGE')
                    self.assertEqual(profile['serviceAccount'], 'fantasy-browser-worker')
                    self.assertEqual(profile['volumes'], ['espn-session-state'])
                launcher = docs[LAUNCHER]['spec']['template']['spec']['containers'][0]
                image = {e['name']: e.get('value') for e in launcher['env']}['WORKER_IMAGE']
                self.assertEqual(image, scaler_worker(docs)['image'])
                self.assertIn('@sha256:', image)

    def test_trade_arming_stays_on_the_scaled_job(self):
        for overlay, docs in self.overlays.items():
            with self.subTest(overlay=overlay):
                worker = scaler_worker(docs)
                worker_env = {e['name']: e for e in worker['env']}
                # TRADE_DRY_RUN / TRADE_SUBMIT_ENABLED arm trade submissions on the queue worker only.
                for name, value in TRADE_ARMING.items():
                    self.assertEqual(worker_env[name], {'name': name, 'value': value})
                others = [c for key, doc in docs.items() if key != SCALER for c in containers(doc)]
                self.assertIn('launcher', [c['name'] for c in others])
                safe_defaults = {'DRY_RUN', 'TRADE_RESPONSE_ENABLED',
                                 'TRADE_INBOX_SHADOW_ACCEPT_MIN_SCORE',
                                 'TRADE_INBOX_SHADOW_DECLINE_MAX_SCORE'}
                for container in others:
                    self.assertFalse(TRADE_ARMING.keys() & env_names(container), container['name'])
                    self.assertFalse(safe_defaults & env_names(container), container['name'])
                self.assertFalse(safe_defaults & env_names(worker))
                # Launched trade jobs import these two, so a key here would arm them.
                for name in ('fantasy-league-config', 'fantasy-notify-config'):
                    config = docs['ConfigMap', name]['data']
                    self.assertFalse(TRADE_ARMING.keys() & config.keys(), name)
                    self.assertFalse(safe_defaults & config.keys(), name)

    def test_no_cronjobs_remain(self):
        for overlay, docs in self.overlays.items():
            with self.subTest(overlay=overlay):
                self.assertEqual([name for kind, name in docs if kind == 'CronJob'], [])


if __name__ == '__main__':
    unittest.main()
