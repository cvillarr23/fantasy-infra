"""Validate private configuration delivery and migration without live credentials."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
IDS = ('ESPN_LEAGUE_ID', 'ESPN_TEAM_ID')
spec = importlib.util.spec_from_file_location('migration', ROOT / 'scripts/migrate-deployment-values.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def containers(node):
    if isinstance(node, dict):
        yield from node.get('containers', [])
        for value in node.values():
            yield from containers(value)
    elif isinstance(node, list):
        for value in node:
            yield from containers(value)


class DeploymentValuesTest(unittest.TestCase):
    def test_private_values_in_both_overlays(self):
        for overlay in ('production', 'draft-day'):
            with self.subTest(overlay=overlay):
                docs = list(yaml.safe_load_all(subprocess.check_output(
                    ['kubectl', 'kustomize', str(ROOT / 'overlays' / overlay)], text=True)))
                resources = {(d['kind'], d['metadata']['name']): d for d in docs if d}
                config = resources[('ConfigMap', 'fantasy-league-config')]['data']
                self.assertTrue(all(key not in config for key in IDS))
                session = resources[('ExternalSecret', 'espn-session-secrets')]
                fields = {d['secretKey']: d['remoteRef'] for d in session['spec']['data']}
                for key in IDS:
                    self.assertEqual(fields[key], {'key': 'fantasy/deployment', 'property': key})
                consumers = []
                for container in containers(docs):
                    if {'configMapRef': {'name': 'fantasy-league-config'}} not in container.get('envFrom', []):
                        continue
                    consumers.append(container['name'])
                    env = {item['name']: item for item in container['env']}
                    for key in IDS:
                        self.assertEqual(env[key], {'name': key, 'valueFrom': {
                            'secretKeyRef': {'name': 'espn-session-secrets', 'key': key}}})
                self.assertEqual(len(consumers), 7)
                gateway = next(c for c in containers(resources[('Deployment', 'fantasy-core-pipeline')])
                               if c['name'] == 'gateway')
                env = {e['name']: e for e in gateway['env']}
                self.assertEqual(env['DRAFT_SYNC_ENV_SECRET']['value'], session['spec']['target']['name'])
                oauth = resources[('Deployment', 'oauth2-proxy')]['spec']['template']['spec']
                proxy = oauth['containers'][0]
                self.assertFalse(any(arg.startswith(('--email-domain=', '--redirect-url=', '--cookie-domain='))
                                     for arg in proxy['args']))
                self.assertIn('--authenticated-emails-file=/etc/oauth2-proxy/authenticated-emails-list', proxy['args'])
                env = {e['name']: e for e in proxy['env']}
                for name, key in (('OAUTH2_PROXY_REDIRECT_URL', 'redirect-url'),
                                  ('OAUTH2_PROXY_COOKIE_DOMAINS', 'cookie-domain')):
                    self.assertEqual(env[name]['valueFrom']['secretKeyRef'], {
                        'name': 'oauth2-proxy-deployment-config', 'key': key})
                volume = oauth['volumes'][0]['secret']
                self.assertEqual(volume, {'secretName': 'oauth2-proxy-deployment-config', 'items': [
                    {'key': 'authenticated-emails-list', 'path': 'authenticated-emails-list'}]})
                self.assertNotIn(('ConfigMap', 'oauth2-proxy-authenticated-emails'), resources)
                fields = resources[('ExternalSecret', volume['secretName'])]['spec']['data']
                self.assertEqual({f['secretKey'] for f in fields}, {
                    'authenticated-emails-list', 'redirect-url', 'cookie-domain'})


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.values = {'secret/fantasy/deployment': {'ESPN_LEAGUE_ID': '123456', 'ESPN_TEAM_ID': '1'}}

    def result(self, data=None, error=''):
        return subprocess.CompletedProcess([], bool(error), json.dumps({'data': {'data': data}}), error)

    def test_default_does_not_write(self):
        with patch.object(migration, 'run', return_value=self.result(error='No value found')) as run:
            migration.migrate(self.values)
            self.assertEqual(len(run.call_args_list), 1)
            self.assertEqual(run.call_args.args[0][1:3], ['kv', 'get'])

    def test_apply_uses_stdin_and_cas_then_verifies(self):
        expected = next(iter(self.values.values()))
        with patch.object(migration, 'run', side_effect=[self.result(error='No value found'),
                                                        self.result(), self.result(expected)]) as run:
            migration.migrate(self.values, apply=True)
            write = run.call_args_list[1].args
            self.assertEqual(write[0], ['bao', 'kv', 'put', '-cas=0', 'secret/fantasy/deployment', '-'])
            self.assertEqual(json.loads(write[1]), expected)

    def test_existing_matching_record_is_not_written(self):
        with patch.object(migration, 'run', return_value=self.result(next(iter(self.values.values())))) as run:
            migration.migrate(self.values, apply=True)
            self.assertTrue(all(call.args[0][2] == 'get' for call in run.call_args_list))

    def test_conflict_or_auth_error_prevents_all_writes(self):
        for result in (self.result({'different': 'value'}), self.result(error='permission denied')):
            with self.subTest(result=result), patch.object(migration, 'run', return_value=result) as run:
                with self.assertRaises(RuntimeError):
                    migration.migrate(self.values, apply=True)
                self.assertEqual(len(run.call_args_list), 1)

    def test_rejects_mismatched_oauth_host_before_writing(self):
        sources = [{'data': {'ESPN_LEAGUE_ID': '123456', 'ESPN_TEAM_ID': '1'}},
                   {'data': {'authenticated-emails-list': 'operator@example.com\n'}},
                   {'spec': {'template': {'spec': {'containers': [{'name': 'oauth2-proxy', 'args': [
                       '--redirect-url=https://draft.example.com/oauth2/callback', '--cookie-domain=other.example.com']}]}}}}]
        with patch.object(migration, 'resource', side_effect=sources):
            with self.assertRaises(RuntimeError):
                migration.current_values()


if __name__ == '__main__':
    unittest.main()
