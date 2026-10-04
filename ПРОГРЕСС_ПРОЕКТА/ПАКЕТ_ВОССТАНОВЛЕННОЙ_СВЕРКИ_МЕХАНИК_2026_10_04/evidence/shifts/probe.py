#!/usr/bin/env python3
"""Actual AST functions with in-memory ORM doubles; NOT a Django/PG/device test."""
import ast
import contextlib
import hashlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
manifest = json.loads((BASE / 'source-manifest.json').read_text())
verified = []
for item in manifest['files']:
    data = (BASE / item['local_path']).read_bytes()
    digest = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    assert digest == item['git_blob_sha'], (item['path'], digest)
    verified.append({**item, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})

def fn(file, name, scope):
    tree = ast.parse((BASE / 'source' / file).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(BASE / 'source' / file), 'exec'), scope)
    return scope[name]

class Row(types.SimpleNamespace):
    def save(self, **kw): pass

class Q:
    def __init__(self, **kw): self.test = lambda row: all(getattr(row, k) is v for k, v in kw.items())
    def __or__(self, other):
        q = Q(); q.test = lambda row: self.test(row) or other.test(row); return q

class Query:
    def __init__(self, rows, all_rows=None): self.rows = rows; self.all_rows = all_rows if all_rows is not None else rows
    def select_for_update(self, **kw): return self
    def select_related(self, *a): return self
    def filter(self, *preds, **kw):
        def match(row):
            for key, value in kw.items():
                if key.endswith('__isnull'):
                    if (getattr(row, key[:-8]) is None) != value: return False
                elif getattr(row, key) != value: return False
            return all(p.test(row) for p in preds)
        return Query([r for r in self.rows if match(r)], self.all_rows)
    def order_by(self, *fields):
        rows = self.rows[:]
        for name in reversed(fields): rows.sort(key=lambda r: getattr(r, name.lstrip('-')), reverse=name.startswith('-'))
        return Query(rows, self.all_rows)
    def first(self): return self.rows[0] if self.rows else None
    def get(self, **kw): return self.filter(**kw).rows[0]
    def create(self, **kw):
        idx = len(self.all_rows) + 1
        row = Row(id=idx, pk=idx, closed_at=None, end_fuel=None, end_mileage=None, end_engine_hours=None, **kw)
        row.employee_id = row.employee.pk; row.equipment_id = row.equipment.pk
        self.all_rows.append(row); return row
    def __iter__(self): return iter(self.rows)

def instant(hour, minute=0): return datetime(2026, 10, 3, hour, minute, tzinfo=timezone.utc)
def run_order(order):
    employee, equipment = Row(pk=1), Row(pk=7)
    shifts, actions = [], {}
    employees = Row(objects=Query([employee])); equipments = Row(objects=Query([equipment]))
    for name, clsname, cls in [('users.models', 'Employee', employees), ('references.models', 'Equipment', equipments)]:
        module = types.ModuleType(name); setattr(module, clsname, cls); sys.modules[name] = module
    core = types.ModuleType('core.models'); core.bump_operational_state = lambda *a, **kw: None; sys.modules['core.models'] = core
    def handover(row, *, closed_by, closed_at): row.closed_at = closed_at; row.is_service_closed = True; return row
    def create_action(**kw): actions[kw['client_action_id']] = kw['shift']
    scope = {
        '_existing_driver_shift_action': lambda _kind, key: actions.get(key),
        'transaction': Row(atomic=contextlib.nullcontext), 'lock_idempotency_key': lambda *a: None,
        'EmployeeShift': Row(objects=Query(shifts)), 'Q': Q, 'handover_other_role_shift': handover,
        'shift_reading_is_whole': lambda value: True, 'validate_driver_fuel_reading': lambda *a: None,
        'ValidationError': ValueError, 'resolve_published_watch_period_for_shift': lambda **kw: None,
        'assign_shift_plan_snapshot': lambda *a: None, 'DRIVER_SHIFT_READING_FIELDS': [],
        'ShiftReadingCorrection': Row(objects=Row(bulk_create=lambda *a: None)),
        'ShiftClientAction': Row(objects=Row(create=create_action)),
    }
    open_shift = fn('services.py', 'open_driver_shift_from_device', scope)
    for label, hour in order:
        open_shift(employee=employee, equipment=equipment, shift_type='day', readings={}, client_action_id=label, opened_at=instant(hour))
    return {label: {'opened_at': row.opened_at.isoformat(), 'closed_at': row.closed_at.isoformat() if row.closed_at else None} for label, row in actions.items()}

forward = run_order([('A', 10), ('B', 11)])
reverse = run_order([('B', 11), ('A', 10)])
assert forward['B']['closed_at'] is None and reverse['B']['closed_at'] == instant(11).isoformat()
assert reverse['A']['closed_at'] is None

shift = Row(pk=42, id=42, opened_at=instant(8), closed_at=instant(11), equipment=Row(pk=7), is_service_closed=True,
            end_fuel=None, end_mileage=None, end_engine_hours=None)
logs = []
close_scope = {'_locked_shift': lambda *a, **kw: shift, '_log_discrepancy': lambda **kw: logs.append(kw['code']),
               '_current_operational_version': lambda: 123}
close_shift = fn('offline_sync.py', '_process_driver_shift_closed_by_device', close_scope)
normalized = {'event_id': 'own-close-42', 'shift_id': 42, 'occurred_at': instant(10, 30),
              'payload': {'end_fuel': '400', 'end_mileage': '1000', 'end_engine_hours': '500'}}
response, links = close_shift(Row(employee=Row(pk=1)), normalized)
assert response['already_closed'] is True and shift.closed_at == instant(11) and shift.end_fuel is None

result = {
    'run_kind': 'NEW_RECOVERY_RUN', 'source_commit': manifest['commit'],
    'scope': 'Actual extracted function bodies; in-memory query and transaction doubles. No Django, PostgreSQL, device, HTTP, side-effect integration or production execution.',
    'old_raw_reused': False, 'source_blob_verification': verified,
    'cases': [
        {'id': 'SHIFT-OPEN-DELIVERY-ORDER', 'status': 'COUNTEREXAMPLE_REPRODUCED', 'forward': forward, 'reverse': reverse,
         'observation': 'Late A closes newer B at B own start, leaving historical A current. Same known chronological inputs give different current shifts.'},
        {'id': 'SHIFT-R21-OWN-EARLIER-CLOSE', 'status': 'COUNTEREXAMPLE_REPRODUCED', 'own_close': instant(10, 30).isoformat(),
         'service_close_after_processing': shift.closed_at.isoformat(), 'readings_applied': shift.end_fuel is not None,
         'response': response, 'diagnostics': logs,
         'precondition': 'Fixture assumes trusted earlier own close for exact same shift, the approved R-21 case; clock/rights verification is outside this isolated test.'}
    ],
    'product_acceptance': 'NOT_RUN',
}
(BASE / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'sources_verified': len(verified), 'cases': [c['status'] for c in result['cases']], 'product_acceptance': 'NOT_RUN'}))
