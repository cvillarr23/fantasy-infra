"""Exercise the provisioned IR-occupant SQL against PostgreSQL."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]
config = yaml.safe_load((ROOT / 'charts/fantasy/files/telemetry/ir-ineligible-occupant-alert.yaml').read_text())
provisioning = yaml.safe_load(next(iter(config['data'].values())))
rule = provisioning['groups'][0]['rules'][0]
assert config['metadata']['labels']['grafana_alert'] == '1'
assert rule['noDataState'] == 'OK'
assert rule['execErrState'] == 'Error'
assert rule['data'][0]['datasourceUid'] == 'FantasyFootballDB'
assert rule['data'][2]['model']['conditions'][0]['evaluator'] == {'type': 'gt', 'params': [0]}
query = rule['data'][0]['model']['rawSql'].strip().rstrip(';')
print('''BEGIN;
CREATE TEMP TABLE trade_candidate_runs (league_id TEXT, our_team_id TEXT, computed_at TIMESTAMPTZ);
CREATE TEMP TABLE league_rosters (team_id TEXT, slot_position TEXT, espn_injury_status TEXT);
CREATE TEMP TABLE league_teams (league_id TEXT, team_id TEXT, ir_slots_used INT, updated_at TIMESTAMP);
''')


def case(name, expected, rosters=(), used=None, ours=(('8', '1 hour'),), used_league='L1'):
    """rosters: (team_id, slot, status); used: {team_id: ir_slots_used}, in used_league;
    ours: (team_id, age) candidate runs, all in league L1."""
    print('TRUNCATE trade_candidate_runs, league_rosters, league_teams;')
    for team, age in ours:
        print(f"INSERT INTO trade_candidate_runs VALUES ('L1', '{team}', now() - interval '{age}');")
    for team, slot, status in rosters:
        value = 'NULL' if status is None else f"'{status}'"
        print(f"INSERT INTO league_rosters VALUES ('{team}', '{slot}', {value});")
    for team, count in (used or {}).items():
        print(f"INSERT INTO league_teams VALUES ('{used_league}', '{team}', {count}, now());")
    print(f"DO $$ BEGIN IF ({query}) <> {expected} THEN RAISE EXCEPTION 'failed: {name}'; END IF; END $$;")


case('empty IR', 0, used={'8': 0})
case('INJURY_RESERVE occupant is eligible', 0, [('8', 'IR', 'INJURY_RESERVE')], {'8': 1})
case('OUT occupant is eligible', 0, [('8', 'IR', 'OUT')], {'8': 1})
case('ACTIVE occupant alerts', 1, [('8', 'IR', 'ACTIVE')], {'8': 1})
case('QUESTIONABLE occupant alerts', 1, [('8', 'IR', 'QUESTIONABLE')], {'8': 1})
case('lower-case eligible status is eligible', 0, [('8', 'IR', 'injury_reserve')], {'8': 1})
case('missing status alerts', 1, [('8', 'IR', None)], {'8': 1})
case('unmatched occupant alerts', 1, [], {'8': 1})
case('unmatched plus ineligible', 2, [('8', 'IR', 'ACTIVE')], {'8': 2})
case('no ir_slots_used row counts matched rows only', 1, [('8', 'IR', 'DOUBTFUL')])
case('healthy bench player ignored', 0, [('8', 'BE', 'ACTIVE')], {'8': 0})
case('other teams ignored', 0, [('7', 'IR', 'DOUBTFUL')], {'7': 1, '8': 0})
case('latest candidate run picks the team', 1, [('7', 'IR', 'DOUBTFUL'), ('8', 'IR', 'OUT')], {'7': 1, '8': 1},
     ours=(('8', '2 days'), ('7', '1 hour')))
case('ir_slots_used from another league ignored', 0, [('8', 'IR', 'OUT')], {'8': 3}, used_league='L2')
case('no candidate run yet reads as healthy', 0, [('8', 'IR', 'ACTIVE')], {'8': 1}, ours=())
print('ROLLBACK;')
