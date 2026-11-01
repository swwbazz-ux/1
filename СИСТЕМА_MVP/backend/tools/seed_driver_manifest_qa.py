"""Synthetic local-only fixture for browser verification of the Driver manifest."""
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.conf import settings
from django.db import transaction
from django.test import Client
from django.utils import timezone
from assignments.models import AssignmentStatus, EquipmentAssignment
from core.models import OperationalStateVersion
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import Dormitory, DormitoryBlock, DormitorySection, DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType
from shifts.models import EmployeeShift, EquipmentPlanGroup
from trips.models import Trip, TripStatus
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role

db = settings.DATABASES['default']
if db['ENGINE'] != 'django.db.backends.sqlite3' or Path(db['NAME']).resolve() != (BACKEND / 'db.sqlite3').resolve():
    raise SystemExit('QA fixture requires the isolated worktree SQLite database')
if 'driver-manifest-downtime-20261101' not in str(BACKEND):
    raise SystemExit('Unexpected QA worktree')

with transaction.atomic():
    if Employee.objects.filter(full_name='Проверка Путёвки Локальная').exists():
        raise SystemExit('Synthetic fixture already exists; no destructive reseed')
    role, _ = Role.objects.get_or_create(code='driver', defaults={'name': 'Водитель самосвала'})
    employee = Employee.objects.create(full_name='Проверка Путёвки Локальная', status=Employee.Status.ACTIVE, is_active=True)
    access = EmployeeAccess.objects.create(employee=employee, role=role, access_code='912345', status=EmployeeAccess.Status.ACTIVATED, is_active=True)
    truck_type, _ = EquipmentType.objects.get_or_create(name='Самосвал')
    excavator_type, _ = EquipmentType.objects.get_or_create(name='Экскаватор')
    model = EquipmentModel.objects.create(equipment_type=truck_type, name='Тестовая модель путёвки', fuel_capacity_limit_l='2500')
    truck = Equipment.objects.create(equipment_type=truck_type, garage_number='QA-24', model=model)
    excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='QA-ЭКС')
    rock = RockType.objects.create(name='Тестовая порода путёвки')
    point = DumpPoint.objects.create(name='Тестовый отвал')
    dormitory = Dormitory.objects.create(number='QA-Путёвка')
    block = DormitoryBlock.objects.create(dormitory=dormitory, name='Тест')
    section = DormitorySection.objects.create(block=block, name='А')
    DriverPrimaryRegistration.objects.create(employee=employee, dormitory_section=section)
    first = timezone.now().replace(microsecond=0) - timedelta(hours=13)
    shift = EmployeeShift.objects.create(employee=employee, workplace_code='driver', equipment=truck, shift_type='day', opened_at=first-timedelta(minutes=20), closed_at=first+timedelta(seconds=38553), opened_by=employee, start_fuel='2000', end_fuel='1782', start_mileage='110000', end_mileage='110059', start_engine_hours='10000', end_engine_hours='10018')
    group = EquipmentPlanGroup.objects.create(code='manifest-qa', name='Тестовые самосвалы путёвки', calculation_mode='trips', plan_value='15')
    group.equipment.add(truck)
    EquipmentAssignment.objects.create(employee=employee, role=role, equipment=truck, shift_type='day', assigned_by=employee, status=AssignmentStatus.ACCEPTED, accepted_at=first-timedelta(minutes=30))
    spans = [(0,184,9),(1807,1962,9),(5261,5496,9),(8946,9570,9),(10457,10746,10),(12918,13491,9),(16934,17946,8),(27186,27249,9),(28203,28381,10),(30999,31516,9),(32290,32600,10),(33184,34154,9)]
    reasons = {}
    for key, name in [(9,'Ожидание погрузки'),(10,'Ожидание разгрузки'),(8,'Заправка')]:
        reason, _ = DowntimeReason.objects.get_or_create(name=name)
        reason.show_for_truck_driver = True
        reason.equipment_type = truck_type
        reason.save()
        reasons[key] = reason
    log = []
    for index,(start,end,key) in enumerate(spans):
        event = DowntimeEvent.objects.create(equipment=truck, employee=employee, subject_employee=employee, recorded_by=employee, reason=reasons[key], started_at=first+timedelta(seconds=start), ended_at=first+timedelta(seconds=end))
        local_id = 'qa-wait-' + str(index)
        log.append({'event_id':local_id,'event_type':'driver.downtime.started','sequence':start+1,'occurred_at':event.started_at.isoformat(),'local_downtime_id':local_id,'server_downtime_id':event.id,'reason_id':reasons[key].id,'reason_label':reasons[key].name,'status':'confirmed'})
        if key != 9:
            log.append({'event_id':local_id+'-end','event_type':'driver.downtime.ended','sequence':end+1,'occurred_at':event.ended_at.isoformat(),'local_downtime_id':local_id,'downtime_id':event.id,'status':'confirmed'})
    for index in range(15):
        loaded = first + timedelta(seconds=400+index*2200)
        Trip.objects.create(excavator=excavator,truck=truck,driver=employee,loading_shift=shift,unloading_shift=shift,driver_control_shift=shift,rock_type=rock,dump_point=point,assigned_dump_point=point,actual_dump_point=point,loaded_at=loaded,completed_at=loaded+timedelta(seconds=1000),status=TripStatus.COMPLETED,volume_m3='57')
    OperationalStateVersion.objects.update_or_create(key='production',defaults={'version':1000,'reason':'local synthetic QA'})
    client = Client()
    session = client.session
    session['employee_access_id'] = access.id
    session['active_role_access_id'] = access.id
    session['active_role_code'] = 'driver'
    session.save()
    out = {'session':session.session_key,'access_id':access.id,'shift_id':shift.id,'first':first.isoformat(),'log':log}

target = BACKEND / 'qa_artifacts' / 'driver-manifest-downtime'
target.mkdir(parents=True,exist_ok=True)
(target/'fixture.json').write_text(json.dumps(out,ensure_ascii=False),encoding='utf-8')
print('LOCAL_MANIFEST_FIXTURE_READY')
