"""Execute exact base/candidate normalizers and replay entry points; ORM doubles only.

This is an extracted-function contract probe, not a Django/DB run.
"""
import ast
import copy
import hashlib
import json
import logging
import re
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone as dtz
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'base_offline_sync.py'
CANDIDATE = ROOT / 'СИСТЕМА_MVP/backend/core/offline_sync.py'
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=dtz.utc)
STATUS = NS(ACCEPTED='accepted', CONFLICT='conflict', RETRY='retry', INVALID='invalid')
access = NS(employee_id=7, id=70)
calls = []

def load_functions(path):
    tree = ast.parse(path.read_text())
    names = {'SYNC_FORMAT_VERSION', 'MAX_BATCH_SIZE', 'MAX_DEPENDENCIES',
             'MAX_FUTURE_CLOCK_SKEW', 'EVENT_ID_RE', 'DEVICE_ID_RE', 'SUPPORTED_EVENT_ROLES',
             'OfflineEventProblem', '_invalid', '_conflict', '_retry', '_clean_identifier',
             '_positive_int', '_clock_hint', 'normalize_offline_event', '_result',
             '_stored_result', 'process_one_offline_event'}
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            selected.append(node)
    class Manager:
        row = None
        def select_for_update(self, **kw): return self
        def filter(self, **kw): return self
        def first(self): return self.row
    manager = Manager()
    env = dict(hashlib=hashlib, json=json, re=re, timedelta=timedelta,
               timezone=NS(now=lambda: NOW, is_naive=lambda t: t.tzinfo is None),
               parse_datetime=datetime.fromisoformat, OfflineFieldEventStatus=STATUS,
               transaction=NS(atomic=nullcontext), IntegrityError=type('IntegrityError', (Exception,), {}),
               OfflineFieldEvent=NS(objects=manager), lock_idempotency_key=lambda *a: None,
               _record_conflict_attempt=lambda **kw: calls.append(kw['code']),
               logger=logging.getLogger('probe'))
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), env)
    return env, manager

base, bm = load_functions(BASE)
candidate, cm = load_functions(CANDIDATE)
out = {'probe_kind': 'exact-source functions with ORM doubles; no Django/DB', 'cases': []}
for role, event_type in [('driver', 'driver.trip.unloaded'), ('excavator_operator', 'excavator.trip.loaded')]:
    raw = {'event_id': 'legacy-' + role, 'event_type': event_type, 'format_version': 1,
           'actor_id': 7, 'access_id': 70, 'role_code': role, 'device_id': 'device-001',
           'sequence': 10, 'occurred_at': '2026-09-29T10:00:00+00:00',
           'shift_id': 5, 'equipment_id': 9, 'depends_on': [], 'payload': {'trip_id': 123}}
    kwargs = {'role_code': role, 'device_id': 'device-001', 'received_at': NOW}
    old = base['normalize_offline_event'](copy.deepcopy(raw), **kwargs)
    new = candidate['normalize_offline_event'](copy.deepcopy(raw), **kwargs)
    receipt = NS(event_id=raw['event_id'], actor_id=7, access_id=70, role_code=role,
                 device_id='device-001', fingerprint=old['fingerprint'], status='accepted',
                 error_code='', error_message='', retryable=False, result_payload={},
                 occurred_at=old['occurred_at'], received_at=NOW, depends_on=[])
    bm.row = receipt
    cm.row = receipt
    old_result = base['process_one_offline_event'](access, old)
    new_result = candidate['process_one_offline_event'](access, new)
    assert old_result['status'] == 'deduplicated', old_result
    assert new_result['code'] == 'event_id_reused', new_result
    assert old['fingerprint'] != new['fingerprint']
    out['cases'].append({'role': role, 'wire_changed': False,
                         'base_fingerprint': old['fingerprint'], 'candidate_fingerprint': new['fingerprint'],
                         'new_normalized_keys': sorted(set(new)-set(old)),
                         'base_replay': old_result, 'candidate_replay': new_result})
print(json.dumps(out, ensure_ascii=False, indent=2))
