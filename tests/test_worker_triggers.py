"""Offline rendered-manifest contract for every browser-worker consumer queue."""
import subprocess
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
QUEUES = {'lineup_updates', 'waiver_actions', 'draft_targets', 'trade_evaluations',
          'trade_negotiations', 'llm_requests', 'trade_submissions'}


class WorkerTriggerTest(unittest.TestCase):
    def test_all_overlays_cover_consumers(self):
        for overlay in ('production', 'draft-day'):
            with self.subTest(overlay=overlay):
                rendered = subprocess.check_output(
                    ['kubectl', 'kustomize', str(ROOT / 'overlays' / overlay)], text=True)
                job = next(doc for doc in yaml.safe_load_all(rendered)
                           if doc and doc.get('kind') == 'ScaledJob'
                           and doc['metadata']['name'] == 'fantasy-browser-worker-scaler')
                triggers = job['spec']['triggers']
                names = [t['metadata']['queueName'] for t in triggers]
                self.assertEqual(set(names), QUEUES)
                self.assertEqual(len(names), len(QUEUES))
                for trigger in triggers:
                    self.assertEqual(trigger['type'], 'rabbitmq')
                    self.assertEqual(trigger['metadata']['mode'], 'QueueLength')
                    self.assertEqual(trigger['metadata']['value'], '1')
                    self.assertEqual(trigger['authenticationRef']['name'], 'rabbitmq-trigger-auth')


if __name__ == '__main__':
    unittest.main()
