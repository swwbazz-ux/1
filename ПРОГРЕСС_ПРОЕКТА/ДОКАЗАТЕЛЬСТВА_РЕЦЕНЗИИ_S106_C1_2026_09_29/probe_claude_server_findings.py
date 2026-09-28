import os, sys, tempfile, json, unittest
from pathlib import Path
from datetime import timedelta

variant, repo = sys.argv[1:]
backend = Path(repo).resolve() / 'СИСТЕМА_MVP/backend'
assert not (backend / '.env').exists()
os.chdir(backend)
sys.path.insert(0, str(backend))
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
os.environ['DJANGO_DB_ENGINE'] = 'sqlite'
import config.settings as cfg
temp = tempfile.TemporaryDirectory(prefix='s106-claude-server-')
cfg.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:', 'TEST': {'NAME': ':memory:'}}}
cfg.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
cfg.MEDIA_ROOT = Path(temp.name)/'media'
cfg.MEDIA_ROOT.mkdir()
cfg.PORTAL_PRIVATE_MEDIA_ROOT = Path(temp.name)/'private'
cfg.ROTATIONS_PRIVATE_MEDIA_ROOT = Path(temp.name)/'rotations'
cfg.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
import django
django.setup()
from django.test.runner import DiscoverRunner
from django.utils import timezone
from core.test_offline_sync import OfflineEventSyncTests
from core.models import OfflineFieldEvent
from core.offline_sync import normalize_offline_event
from trips.models import Trip, TripClientAction, TripStatus

class Probe(OfflineEventSyncTests):
    def test_clock_domain_sequence(self):
        if variant != 'pr106':
            self.skipTest('PR-specific replay-gate check')
        self._open_shift_context_earlier(timedelta(minutes=60))
        loaded = self.load_event('a-parent', 1, occurred_at=timezone.now()-timedelta(minutes=3))
        first = self.sync([loaded]).json()['results'][0]
        self.assertEqual(first['status'], 'accepted', first)
        trip = Trip.objects.get()
        trip.driver_participation_recorded = True
        trip.driver_control_shift = None
        trip.is_carryover = False
        trip.save(update_fields=['driver_participation_recorded','driver_control_shift','is_carryover'])
        received = timezone.now()-timedelta(minutes=1)
        raw = received+timedelta(minutes=40)
        event = {'event_id':'a-repeat','event_type':'driver.trip.unloaded','format_version':1,
                 'occurred_at':raw.isoformat(),'sequence':1,'depends_on':[],
                 'shift_id':self.truck_shift.id,'equipment_id':self.truck.id,
                 'trip_id':trip.id,'payload':{}}
        norm = normalize_offline_event(event, role_code='driver',device_id='a-driver',received_at=received)
        receipt = OfflineFieldEvent.objects.create(event_id=event['event_id'],event_type=event['event_type'],format_version=1,
            actor=self.driver,access=self.driver_access,role_code='driver',device_id='a-driver',sequence=1,
            depends_on=[],occurred_at=raw,received_at=received,shift=self.truck_shift,equipment=self.truck,
            fingerprint=norm['fingerprint'],payload={},context_snapshot={},status='conflict',error_code='trip_driver_shift_changed')
        results=[]
        for _ in range(4):
            results.append(self.sync([event],client=self.driver_client(),role_code='driver',device_id='a-driver').json()['results'][0])
        codes=[r['code'] for r in results]
        self.assertEqual(codes,['device_clock_ahead','trip_driver_shift_changed','device_clock_ahead','trip_driver_shift_changed'])
        receipt.refresh_from_db()
        self.assertEqual(receipt.occurred_at,raw)
        self.assertEqual(receipt.received_at,received)
        self.assertEqual(Trip.objects.count(),1)
        print('TRACE_A='+json.dumps({'variant':variant,'codes':codes,'trip_count':Trip.objects.count(),'immutable_raw':raw.isoformat(),'first_received':received.isoformat()}))

    def test_manual_completion_excavator_mismatch(self):
        self._open_shift_context_earlier(timedelta(minutes=60))
        loaded=self.driver_manual_event('b-load',1,occurred_at=timezone.now()-timedelta(minutes=4))
        first=self.sync([loaded],client=self.driver_client(),role_code='driver',device_id='b-driver').json()['results'][0]
        self.assertEqual(first['status'],'accepted',first)
        trip=Trip.objects.get()
        complete=self.driver_manual_complete_event(loaded,event_id='b-complete',sequence=2,trip_id=trip.id,occurred_at=timezone.now()-timedelta(minutes=2))
        complete['payload']['excavator_id']=self.other_excavator.id
        result=self.sync([complete],client=self.driver_client(),role_code='driver',device_id='b-driver').json()['results'][0]
        trip.refresh_from_db()
        if variant=='release':
            self.assertEqual(result['status'],'accepted',result)
            self.assertEqual(trip.status,TripStatus.COMPLETED)
            self.assertEqual(trip.completed_at,timezone.datetime.fromisoformat(complete['occurred_at']))
        else:
            self.assertEqual(result['status'],'conflict',result)
            self.assertEqual(result['code'],'driver_manual_trip_changed')
            self.assertEqual(trip.status,TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.excavator_id,self.excavator.id)
        print('TRACE_B='+json.dumps({'variant':variant,'status':result['status'],'code':result.get('code'),'trip_status':trip.status,'actual_excavator':trip.excavator_id,'payload_excavator':complete['payload']['excavator_id']}))

class Runner(DiscoverRunner):
    def build_suite(self,*args,**kwargs):
        names=['test_manual_completion_excavator_mismatch']
        if variant=='pr106': names.insert(0,'test_clock_domain_sequence')
        return unittest.TestSuite(Probe(n) for n in names)

try:
    raise SystemExit(Runner(verbosity=2,interactive=False).run_tests([]))
finally:
    temp.cleanup()
