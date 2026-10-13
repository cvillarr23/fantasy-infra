"""Offline rendered-manifest contract for every browser-worker consumer queue."""
import json
import subprocess
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
QUEUES = {'lineup_updates', 'waiver_actions', 'draft_targets', 'trade_evaluations',
          'trade_negotiations', 'llm_requests', 'trade_submissions', 'trade_responses'}


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
                job = docs['ScaledJob', 'fantasy-browser-worker-scaler']
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

    def test_inbox_schedule_and_safe_worker_contract(self):
        for overlay, docs in self.overlays.items():
            with self.subTest(overlay=overlay):
                inbox = docs['CronJob', 'trade-inbox']
                job = docs['ScaledJob', 'fantasy-browser-worker-scaler']
                spec = inbox['spec']
                self.assertEqual(spec['schedule'], '0 8-22/2 * * *')
                self.assertEqual(spec['timeZone'], 'America/Chicago')
                self.assertEqual(spec['concurrencyPolicy'], 'Forbid')
                self.assertEqual(spec['startingDeadlineSeconds'], 1800)
                target = spec['jobTemplate']['spec']
                self.assertEqual(target['activeDeadlineSeconds'], 660)
                self.assertEqual(target['backoffLimit'], 0)
                pod = target['template']['spec']
                self.assertEqual(pod['restartPolicy'], 'Never')
                self.assertEqual(pod['serviceAccountName'], 'fantasy-browser-worker')
                container = pod['containers'][0]
                worker = job['spec']['jobTargetRef']['template']['spec']['containers'][0]
                self.assertEqual(container['image'], worker['image'])
                self.assertIn('@sha256:', container['image'])
                self.assertEqual(container['command'], ['node', 'src/bots/trade_inbox.js', '--refresh'])
                scan = docs['CronJob', 'trade-scan']
                scan_pod = scan['spec']['jobTemplate']['spec']['template']['spec']
                self.assertEqual(pod['volumes'], scan_pod['volumes'])
                self.assertEqual(container['volumeMounts'], scan_pod['containers'][0]['volumeMounts'])
                self.assertEqual(container['envFrom'], scan_pod['containers'][0]['envFrom'])
                env = {e['name']: e for e in container['env']}
                scan_env = {e['name']: e for e in scan_pod['containers'][0]['env']}
                # RUN_CRONJOB names each Job's own CronJob for the run audit log.
                expected_env = {name: value for name, value in scan_env.items()
                                if name not in {'GROQ_API_KEY', 'GEMINI_API_KEY', 'RUN_CRONJOB'}}
                self.assertEqual(env.pop('RUN_CRONJOB')['value'], 'trade-inbox')
                self.assertEqual(env, expected_env)
                self.assertNotIn('GROQ_API_KEY', env)
                self.assertNotIn('GEMINI_API_KEY', env)
                safe_defaults = {'DRY_RUN', 'TRADE_RESPONSE_ENABLED',
                                 'TRADE_INBOX_SHADOW_ACCEPT_MIN_SCORE',
                                 'TRADE_INBOX_SHADOW_DECLINE_MAX_SCORE'}
                config = docs['ConfigMap', 'fantasy-league-config']
                self.assertFalse(safe_defaults & config['data'].keys())
                for workload in (container, worker, scan_pod['containers'][0]):
                    self.assertFalse(safe_defaults & {e['name'] for e in workload['env']})


if __name__ == '__main__':
    unittest.main()
