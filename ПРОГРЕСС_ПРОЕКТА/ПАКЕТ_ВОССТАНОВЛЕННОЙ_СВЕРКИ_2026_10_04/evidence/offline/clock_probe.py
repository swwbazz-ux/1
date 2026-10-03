"""Run exact normalize_offline_event AST with standard-library dependencies only."""
import ast, hashlib, json, re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
base=Path(__file__).resolve().parent
manifest=json.loads((base/'source-manifest.json').read_text())
entry=next(x for x in manifest['files'] if x['local']=='source/offline_sync.py')
source=(base/entry['local']).read_bytes()
assert hashlib.sha1(b'blob '+str(len(source)).encode()+b'\0'+source).hexdigest()==entry['git_blob_sha']
tree=ast.parse(source)
names={'_clean_identifier','_positive_int','_clock_hint','normalize_offline_event'}
funcs=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
assert len(funcs)==4
def invalid(*args):raise ValueError(args)
env={'re':re,'json':json,'hashlib':hashlib,'_invalid':invalid,'EVENT_ID_RE':re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 'SYNC_FORMAT_VERSION':1,'MAX_DEPENDENCIES':32,'MAX_FUTURE_CLOCK_SKEW':timedelta(minutes=5),
 'SUPPORTED_EVENT_ROLES':{'driver.downtime.started':'driver'},
 'parse_datetime':lambda v:datetime.fromisoformat(v.replace('Z','+00:00')),
 'timezone':SimpleNamespace(is_naive=lambda v:v.tzinfo is None)}
exec(compile(ast.Module(body=funcs,type_ignores=[]),entry['path'],'exec'),env)
raw={'event_id':'probe-clock','event_type':'driver.downtime.started','format_version':1,'sequence':1,'depends_on':[],
 'occurred_at':'2026-10-03T10:00:00Z','actor_id':1,'access_id':1,'role_code':'driver','device_id':'probe-device-1',
 'shift_id':4,'equipment_id':3,'payload':{'reason_id':1},'context_snapshot':{}}
received=datetime.fromisoformat('2026-10-03T10:10:00+00:00')
a=env['normalize_offline_event'](raw,role_code='driver',device_id='probe-device-1',received_at=received)
b=env['normalize_offline_event']({**raw,'sent_live':True},role_code='driver',device_id='probe-device-1',received_at=received)
assert a['occurred_at'].isoformat()=='2026-10-03T10:00:00+00:00'
assert b['occurred_at']==received
assert a['fingerprint']==b['fingerprint']
result={'release':manifest['release'],'source_git_blob':entry['git_blob_sha'],'scope':'Actual normalize function AST; standard-library environment, no Django/DB/request processing.',
 'case':'same original event, transport flag changes effective time', 'device_occurred_at':b['device_occurred_at'].isoformat(),
 'received_at':received.isoformat(),'without_sent_live':a['occurred_at'].isoformat(),'with_sent_live':b['occurred_at'].isoformat(),
 'fingerprint_unchanged':True,'status':'COUNTEREXAMPLE_REPRODUCED',
 'limitation':'Fixture supplies sent_live directly; does not prove a ten-minute network delay occurred in a real installation. Flag is not measured clock-offset evidence.'}
(base/'clock-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
