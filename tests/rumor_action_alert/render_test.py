"""Exercise the actual provisioned SQL against PostgreSQL, with a frozen transaction clock."""
from pathlib import Path
import yaml
import json

ROOT = Path(__file__).resolve().parents[2]
config = yaml.safe_load((ROOT / 'base/telemetry/rumor-action-outcome-alert.yaml').read_text())
provisioning = yaml.safe_load(next(iter(config['data'].values())))
rule = provisioning['groups'][0]['rules'][0]
assert config['metadata']['labels']['grafana_alert'] == '1'
assert provisioning['groups'][0]['interval'] == '5m'
assert rule['for'] == '0m'
assert rule['noDataState'] == 'OK'
assert rule['execErrState'] == 'Error'
assert rule['data'][0]['datasourceUid'] == 'FantasyFootballDB'
assert rule['data'][2]['model']['conditions'][0]['evaluator'] == {'type': 'gt', 'params': [0]}
dashboard_config = yaml.safe_load((ROOT / 'base/core-pipeline/fantasy-execution-log-dashboard.yaml').read_text())
dashboard = json.loads(dashboard_config['data']['fantasy-execution-log.json'])
outcome_panel = next(p for p in dashboard['panels'] if p['id'] == 6)
assert outcome_panel.get('timeFrom') == '26h', 'Linked panel must include the entire alert window'
query = rule['data'][0]['model']['rawSql'].strip().rstrip(';')
print('''BEGIN;
CREATE TEMP TABLE execution_log (
  id SERIAL PRIMARY KEY, transaction_type TEXT, bot_name TEXT, status TEXT,
  executed_at TIMESTAMP, outcome_details JSONB
);
''')


def case(name, rows, expected):
    print('TRUNCATE execution_log;')
    for status, age, bot, transaction in rows:
        print(f"INSERT INTO execution_log (status,executed_at,bot_name,transaction_type) VALUES ('{status}', now() - interval '{age}', '{bot}', '{transaction}');")
    print(f"DO $$ BEGIN IF ({query}) <> {expected} THEN RAISE EXCEPTION 'failed: {name}'; END IF; END $$;")


def row(status, age='1 hour', bot='rumor_action_service', transaction='rumor_action_trigger'):
    return status, age, bot, transaction


case('empty history / missed job belongs to Kubernetes alert', [], 0)
case('healthy no-op and accepted work', [row('success')], 0)
case('partial failure', [row('partial_failure')], 1)
case('total failure', [row('failure')], 1)
case('subsequent empty success cannot hide failure', [row('failure', '2 hours'), row('success')], 1)
case('both failure kinds counted', [row('failure'), row('partial_failure')], 2)
case('old failure expires', [row('failure', '26 hours 1 second')], 0)
case('26 hour boundary expires', [row('failure', '26 hours')], 0)
case('just within window', [row('failure', '25 hours 59 minutes 59 seconds')], 1)
case('other bot and transaction ignored', [row('failure', bot='other'), row('failure', transaction='other')], 0)
case('legacy failure with no JSON still alerts', [row('failure')], 1)
print('ROLLBACK;')
