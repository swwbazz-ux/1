"""Локальный стенд SSE: две рабочие роли, смены, назначение и рейс.

Команда не является продуктовой функцией. Она нужна изолированному стенду
T-016: поднять ровно две одноразовые учётные записи (``driver`` и
``excavator_operator``), открыть им смены на технике, дать принятое рабочее
назначение и путевое назначение самосвала, а также минимальный набор
справочников, без которых экраны водителя и машиниста не отдают данные.

Почему через доменные сервисы, а не прямыми INSERT:
``open_driver_shift``/``open_excavator_shift`` сами проверяют активный доступ,
конфликты смены сотрудника и техники, показания и идемпотентность по
``client_action_id``; ``schedule_haul_assignment`` и
``accept_haul_assignment_now`` — штатный путь «диспетчер назначил → водитель
принял». Команда не обходит эти правила и не пишет ``EmployeeShift`` руками.

Границы:
- работает только с локальной стендовой базой (``sse_local``/``test_sse_local``);
- ``--reset`` удаляет ровно те записи, которые помечены префиксом стенда;
- пароли/коды — одноразовые локальные, в production их переносить нельзя.
"""

from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    EquipmentAssignment,
    HaulAssignment,
    HaulAssignmentHandoff,
)
from assignments.services import (
    accept_haul_assignment_now,
    schedule_haul_assignment,
    validate_work_assignment,
)
from core.models import OperationalStateVersion
from downtimes.models import DowntimeEvent
from references.models import (
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
    TruckCapacityRule,
)
from shifts.models import (
    DriverShiftReadingConfirmation,
    EmployeeShift,
    ExcavatorShiftReadingConfirmation,
    ShiftClientAction,
    ShiftReadingCorrection,
)
from shifts.services import driver_open_shift_queryset, open_driver_shift, open_excavator_shift
from trips.models import Trip, TripClientAction, TripStatus
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role


# Маркеры: по ним команда находит свои записи и не трогает чужие данные.
STAND_PREFIX = 'SSE-STAND'
DRIVER_PERSONNEL_NUMBER = f'{STAND_PREFIX}-DRV'
EXCAVATOR_PERSONNEL_NUMBER = f'{STAND_PREFIX}-EXC'
TRUCK_GARAGE_NUMBER = 'SSE-1'
EXCAVATOR_GARAGE_NUMBER = 'SSE-ЭКС-1'
TRUCK_MODEL_NAME = 'SSE Самосвал 40 т'
EXCAVATOR_MODEL_NAME = 'SSE Экскаватор 1,2 м3'
DUMP_POINT_NAME = 'SSE Отвал 1'
TRIP_NOTE = 'SSE стенд T-016: исторический рейс'
STAND_DATABASES = frozenset({'sse_local', 'test_sse_local'})

DRIVER = {
    'role_code': 'driver',
    'role_name': 'Водитель самосвала',
    'full_name': 'Стенд SSE: водитель',
    'phone': '+79000000101',
    'access_code': '111111',
    'personnel_number': DRIVER_PERSONNEL_NUMBER,
    'shift_action_id': 'sse-stand-driver-shift-open-20261112',
}
EXCAVATOR_OPERATOR = {
    'role_code': 'excavator_operator',
    'role_name': 'Машинист экскаватора',
    'full_name': 'Стенд SSE: машинист экскаватора',
    'phone': '+79000000102',
    'access_code': '222222',
    'personnel_number': EXCAVATOR_PERSONNEL_NUMBER,
    'shift_action_id': 'sse-stand-excavator-shift-open-20261112',
}

# Показания на начало смены — целые числа (правило формы смены).
DRIVER_READINGS = {
    'start_fuel': 400,
    'start_mileage': 100000,
    'start_engine_hours': 5000,
}
EXCAVATOR_FUEL_LITERS = 3000
EXCAVATOR_ENGINE_HOURS = 12000


class Command(BaseCommand):
    help = (
        'Создаёт локальные записи стенда SSE: две роли (driver, excavator_operator), '
        'открытые смены, назначения и минимальные справочники. Только для локальной БД.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Удалить записи стенда (по маркерам SSE-STAND/SSE-*) и создать их заново.',
        )
        parser.add_argument(
            '--allow-any-database',
            action='store_true',
            help=(
                'Разрешить запуск на базе с другим именем. По умолчанию команда '
                'работает только с sse_local/test_sse_local.'
            ),
        )

    # ------------------------------------------------------------------ guard

    def assert_local_database(self, allow_any_database):
        database = settings.DATABASES['default']
        engine = str(database.get('ENGINE') or '')
        name = str(database.get('NAME') or '')
        if allow_any_database:
            self.stdout.write(self.style.WARNING(
                'Проверка имени базы отключена (--allow-any-database).'
            ))
            return
        if 'postgresql' not in engine or name not in STAND_DATABASES:
            raise CommandError(
                'Команда предназначена только для локального стенда SSE: '
                f'нужна PostgreSQL-база {sorted(STAND_DATABASES)}, '
                f'а фактически {engine!r} / {name!r}. '
                'Если это осознанное решение, добавьте --allow-any-database.'
            )

    # ---------------------------------------------------------------- команда

    def handle(self, *args, **options):
        self.assert_local_database(options['allow_any_database'])
        report = []
        with transaction.atomic():
            if options['reset']:
                self.reset_stand(report)
            summary = self.build_stand(report)
        self.print_summary(summary, report)

    # ------------------------------------------------------------------ reset

    def reset_stand(self, report):
        employees = Employee.objects.filter(personnel_number__startswith=STAND_PREFIX)
        employee_ids = list(employees.values_list('id', flat=True))
        equipment = Equipment.objects.filter(
            garage_number__in=(TRUCK_GARAGE_NUMBER, EXCAVATOR_GARAGE_NUMBER)
        )
        equipment_ids = list(equipment.values_list('id', flat=True))
        shifts = EmployeeShift.objects.filter(employee_id__in=employee_ids)
        shift_ids = list(shifts.values_list('id', flat=True))
        trips = Trip.objects.filter(
            Q(truck_id__in=equipment_ids) | Q(excavator_id__in=equipment_ids)
        )
        trip_ids = list(trips.values_list('id', flat=True))
        assignments = HaulAssignment.objects.filter(
            Q(truck_id__in=equipment_ids) | Q(excavator_id__in=equipment_ids)
        )
        haul_assignment_ids = list(assignments.values_list('id', flat=True))

        if not employee_ids and not equipment_ids:
            report.append('reset: записей стенда не найдено, удалять нечего.')
            return

        # Порядок важен: PROTECT-связи удаляются до своих владельцев.
        from core.models import OfflineFieldEvent, OfflineFieldEventConflict

        self.delete_queryset(
            OfflineFieldEventConflict.objects.filter(
                Q(actor_id__in=employee_ids) | Q(existing_event__actor_id__in=employee_ids)
            ),
            'OfflineFieldEventConflict',
            report,
        )
        self.delete_queryset(
            OfflineFieldEvent.objects.filter(
                Q(actor_id__in=employee_ids)
                | Q(trip_id__in=trip_ids)
                | Q(equipment_id__in=equipment_ids)
                | Q(shift_id__in=shift_ids)
            ),
            'OfflineFieldEvent',
            report,
        )
        self.delete_queryset(
            TripClientAction.objects.filter(
                Q(trip_id__in=trip_ids) | Q(actor_id__in=employee_ids)
            ),
            'TripClientAction',
            report,
        )
        self.delete_queryset(
            HaulAssignmentHandoff.objects.filter(
                Q(truck_id__in=equipment_ids)
                | Q(source_excavator_id__in=equipment_ids)
                | Q(source_shift_id__in=shift_ids)
                | Q(target_assignment_id__in=haul_assignment_ids)
                | Q(source_assignment_id__in=haul_assignment_ids)
            ),
            'HaulAssignmentHandoff',
            report,
        )
        self.delete_queryset(Trip.objects.filter(id__in=trip_ids), 'Trip', report)
        self.delete_queryset(
            HaulAssignment.objects.filter(id__in=haul_assignment_ids),
            'HaulAssignment',
            report,
        )
        self.delete_queryset(
            ShiftClientAction.objects.filter(employee_id__in=employee_ids),
            'ShiftClientAction',
            report,
        )
        self.delete_queryset(
            ShiftReadingCorrection.objects.filter(
                Q(equipment_id__in=equipment_ids) | Q(employee_id__in=employee_ids)
            ),
            'ShiftReadingCorrection',
            report,
        )
        self.delete_queryset(
            DriverShiftReadingConfirmation.objects.filter(employee_id__in=employee_ids),
            'DriverShiftReadingConfirmation',
            report,
        )
        self.delete_queryset(
            ExcavatorShiftReadingConfirmation.objects.filter(employee_id__in=employee_ids),
            'ExcavatorShiftReadingConfirmation',
            report,
        )
        self.delete_queryset(
            DowntimeEvent.objects.filter(
                Q(equipment_id__in=equipment_ids) | Q(employee_id__in=employee_ids)
            ),
            'DowntimeEvent',
            report,
        )
        self.delete_queryset(
            EquipmentAssignment.objects.filter(
                Q(employee_id__in=employee_ids) | Q(equipment_id__in=equipment_ids)
            ),
            'EquipmentAssignment',
            report,
        )
        self.delete_queryset(EmployeeShift.objects.filter(id__in=shift_ids), 'EmployeeShift', report)
        self.delete_queryset(
            DriverPrimaryRegistration.objects.filter(employee_id__in=employee_ids),
            'DriverPrimaryRegistration',
            report,
        )
        self.delete_queryset(
            EmployeeAccess.objects.filter(employee_id__in=employee_ids),
            'EmployeeAccess',
            report,
        )
        self.delete_queryset(Employee.objects.filter(id__in=employee_ids), 'Employee', report)
        self.delete_queryset(
            TruckCapacityRule.objects.filter(equipment_model__name__in=(
                TRUCK_MODEL_NAME, EXCAVATOR_MODEL_NAME,
            )),
            'TruckCapacityRule',
            report,
        )
        self.delete_queryset(
            Equipment.objects.filter(id__in=equipment_ids), 'Equipment', report
        )
        self.delete_queryset(
            EquipmentModel.objects.filter(name__in=(TRUCK_MODEL_NAME, EXCAVATOR_MODEL_NAME)),
            'EquipmentModel',
            report,
        )
        self.delete_queryset(DumpPoint.objects.filter(name=DUMP_POINT_NAME), 'DumpPoint', report)
        # Породу (канонический справочник) и типы техники «Самосвал»/«Экскаватор»
        # команда намеренно не удаляет: это общие справочные строки, а не
        # учётные записи стенда.

    def delete_queryset(self, queryset, label, report, *, strict=True):
        from django.db.models import ProtectedError

        try:
            deleted, _ = queryset.delete()
        except ProtectedError as error:
            if not strict:
                report.append(
                    f'reset: {label} оставлен — есть защищённые ссылки '
                    f'({len(error.protected_objects)}).'
                )
                return
            report.append(
                f'reset: {label} не удалён — на него ссылаются защищённые записи '
                f'({len(error.protected_objects)}). Стенд пересоздавать небезопасно.'
            )
            raise CommandError(
                f'Не удалось очистить {label}: остались защищённые ссылки. '
                'Проверьте записи вручную, команда не удаляет чужие данные.'
            ) from error
        if deleted:
            report.append(f'reset: {label} — удалено объектов/строк: {deleted}.')

    # ------------------------------------------------------------------ данные

    def stand_rock_name(self):
        from references.rock_catalog import CANONICAL_ROCK_NAMES

        return CANONICAL_ROCK_NAMES[-1]

    def ensure_row(self, model, lookup, defaults):
        """Создаёт строку или обновляет её только при фактическом изменении.

        Прямой ``update_or_create`` вызывает ``save()`` даже без изменений, а на
        ``Equipment``/``Employee``/``EmployeeAccess`` висят post_save сигналы:
        повторный запуск команды нагонял бы версию оперативного состояния и
        лишние SSE-события. Стенд обязан быть идемпотентным и по журналу.
        """
        obj = model.objects.filter(**lookup).order_by('id').first()
        if obj is None:
            return model.objects.create(**lookup, **defaults), True
        changed = []
        for field, value in defaults.items():
            if getattr(obj, field) != value:
                setattr(obj, field, value)
                changed.append(field)
        if changed:
            obj.save(update_fields=changed)
        return obj, False

    def ensure_roles(self):
        roles = {}
        for spec in (DRIVER, EXCAVATOR_OPERATOR):
            role, _ = self.ensure_row(
                Role,
                {'code': spec['role_code']},
                {'name': spec['role_name'], 'is_active': True},
            )
            roles[spec['role_code']] = role
        return roles

    def ensure_references(self):
        truck_type, _ = self.ensure_row(
            EquipmentType, {'name': 'Самосвал'}, {'is_active': True},
        )
        excavator_type, _ = self.ensure_row(
            EquipmentType, {'name': 'Экскаватор'}, {'is_active': True},
        )
        truck_model, _ = self.ensure_row(
            EquipmentModel,
            {'equipment_type': truck_type, 'name': TRUCK_MODEL_NAME},
            {
                'payload_tons': Decimal('40.00'),
                'body_volume_m3': Decimal('25.00'),
                # Лимит топлива нужен форме открытия смены водителя.
                'fuel_capacity_limit_l': 600,
                'is_active': True,
            },
        )
        excavator_model, _ = self.ensure_row(
            EquipmentModel,
            {'equipment_type': excavator_type, 'name': EXCAVATOR_MODEL_NAME},
            {
                'payload_tons': Decimal('2.50'),
                'body_volume_m3': Decimal('1.20'),
                # Вместимость бака нужна экрану машиниста (топливо в литрах).
                'fuel_capacity_limit_l': 7450,
                'is_active': True,
            },
        )
        truck, _ = self.ensure_row(
            Equipment,
            {'garage_number': TRUCK_GARAGE_NUMBER},
            {
                'equipment_type': truck_type,
                'model': truck_model,
                'is_own': True,
                'is_active': True,
            },
        )
        excavator, _ = self.ensure_row(
            Equipment,
            {'garage_number': EXCAVATOR_GARAGE_NUMBER},
            {
                'equipment_type': excavator_type,
                'model': excavator_model,
                'is_own': True,
                'is_active': True,
            },
        )
        rock, _ = self.ensure_row(
            RockType,
            {'name': self.stand_rock_name()},
            {
                'density': Decimal('2.60'),
                'loosening_factor': Decimal('1.50'),
                'is_active': True,
            },
        )
        # Каноническая порода могла прийти из справочника без плотности: тогда
        # экран машиниста её не покажет. Дозаполняем только пустые значения.
        rock_filled = []
        if rock.density is None:
            rock.density = Decimal('2.60')
            rock_filled.append('density')
        if rock.loosening_factor is None:
            rock.loosening_factor = Decimal('1.50')
            rock_filled.append('loosening_factor')
        if rock_filled:
            rock.save(update_fields=rock_filled)
        dump_point, _ = self.ensure_row(
            DumpPoint, {'name': DUMP_POINT_NAME}, {'is_active': True},
        )
        # Кубатура рейса по породе: без правила экран машиниста не предложит
        # породу для погрузки.
        self.ensure_row(
            TruckCapacityRule,
            {'equipment_model': truck_model, 'rock_type': rock},
            {'volume_m3': Decimal('25.00')},
        )
        return {
            'truck': truck,
            'excavator': excavator,
            'rock': rock,
            'dump_point': dump_point,
        }

    def ensure_employee(self, spec, role):
        self.assert_credentials_free(spec)
        employee, _ = self.ensure_row(
            Employee,
            {'personnel_number': spec['personnel_number']},
            {
                'full_name': spec['full_name'],
                'phone': spec['phone'],
                'status': Employee.Status.ACTIVE,
                'is_active': True,
            },
        )
        access = (
            EmployeeAccess.objects
            .select_related('role')
            .filter(employee=employee, role=role)
            .order_by('id')
            .first()
        )
        if access is None:
            access = EmployeeAccess.objects.create(
                employee=employee,
                role=role,
                access_code=spec['access_code'],
                status=EmployeeAccess.Status.ACTIVATED,
                activated_at=timezone.now(),
                is_active=True,
            )
        else:
            changed = []
            if access.access_code != spec['access_code']:
                access.access_code = spec['access_code']
                changed.append('access_code')
            if access.status != EmployeeAccess.Status.ACTIVATED:
                access.status = EmployeeAccess.Status.ACTIVATED
                access.activated_at = access.activated_at or timezone.now()
                changed.extend(['status', 'activated_at'])
            if not access.is_active:
                access.is_active = True
                changed.append('is_active')
            if changed:
                access.save(update_fields=changed)
        # Второй доступ той же роли на том же номере сделал бы вход
        # неоднозначным (users.access_auth), поэтому лишние гасим.
        duplicates = (
            EmployeeAccess.objects
            .filter(employee=employee, role=role, is_active=True)
            .exclude(pk=access.pk)
        )
        if duplicates.exists():
            duplicates.update(is_active=False, status=EmployeeAccess.Status.DEACTIVATED)
        return employee, access

    def assert_credentials_free(self, spec):
        """Тот же номер у другого сотрудника = неоднозначный вход."""
        from users.forms import normalize_phone

        phone = normalize_phone(spec['phone'])
        if not phone:
            raise CommandError(f'Некорректный номер стенда: {spec["phone"]!r}.')
        conflicts = (
            Employee.objects
            .filter(is_active=True)
            .exclude(personnel_number__startswith=STAND_PREFIX)
            .exclude(Q(phone='') | Q(phone__isnull=True))
        )
        for employee in conflicts.only('id', 'full_name', 'phone'):
            if normalize_phone(employee.phone) == phone:
                raise CommandError(
                    f'Номер {spec["phone"]} уже принадлежит сотруднику '
                    f'{employee.pk} ({employee.full_name}). '
                    'Стенд не переопределяет чужие номера.'
                )

    def ensure_work_assignment(self, employee, role, equipment):
        """Принятое рабочее назначение — источник состояния экранов."""
        existing = (
            EquipmentAssignment.objects
            .filter(
                employee=employee,
                role=role,
                equipment=equipment,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
                shift__isnull=True,
            )
            .order_by('id')
            .first()
        )
        if existing is not None:
            return existing, False
        validate_work_assignment(
            employee=employee,
            role=role,
            equipment=equipment,
            shift_type='day',
        )
        assignment = EquipmentAssignment.objects.create(
            employee=employee,
            role=role,
            equipment=equipment,
            shift_type='day',
            assigned_by=None,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        return assignment, True

    def ensure_driver_shift(self, employee, work_assignment):
        open_shift = driver_open_shift_queryset(employee).order_by('-opened_at').first()
        if open_shift is not None:
            return open_shift, False
        shift, created = open_driver_shift(
            employee=employee,
            work_assignment=work_assignment,
            readings=dict(DRIVER_READINGS),
            client_action_id=DRIVER['shift_action_id'],
        )
        return shift, created

    def ensure_excavator_shift(self, employee, equipment):
        open_shift = (
            EmployeeShift.objects
            .select_related('equipment', 'equipment__equipment_type')
            .filter(employee=employee, closed_at__isnull=True)
            .filter(equipment__equipment_type__name='Экскаватор')
            .filter(Q(workplace_code='excavator_operator') | Q(workplace_code=''))
            .order_by('-opened_at')
            .first()
        )
        if open_shift is not None:
            return open_shift, False
        payload = open_excavator_shift(
            employee=employee,
            equipment=equipment,
            shift_type='day',
            fuel_value=EXCAVATOR_FUEL_LITERS,
            engine_hours_value=EXCAVATOR_ENGINE_HOURS,
            client_action_id=EXCAVATOR_OPERATOR['shift_action_id'],
        )
        shift = (
            EmployeeShift.objects
            .select_related('equipment', 'equipment__equipment_type')
            .get(pk=payload['shift_id'])
        )
        return shift, not payload.get('deduplicated', False)

    def ensure_haul_assignment(self, truck, excavator, report):
        accepted = (
            HaulAssignment.objects
            .filter(
                truck=truck,
                excavator=excavator,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
            )
            .order_by('-assigned_at', '-id')
            .first()
        )
        if accepted is not None:
            return accepted, False
        assignment, _ = schedule_haul_assignment(
            truck=truck,
            excavator=excavator,
            assigned_by=None,
        )
        # «Принять» у водителя = «Перейти сейчас»: применяем немедленно, чтобы
        # стенд не ждал пятиминутную паузу назначения.
        applied, _ = accept_haul_assignment_now(assignment.id, truck_id=truck.id)
        final = applied or assignment
        if final is None or final.status != AssignmentStatus.ACCEPTED:
            report.append(
                'назначение самосвала осталось в статусе '
                f'{getattr(final, "status", None)!r} — проверьте вручную.'
            )
        return final, True

    def ensure_completed_trip(self, refs, driver, driver_shift, excavator_operator, excavator_shift):
        trip = Trip.objects.filter(truck=refs['truck'], note=TRIP_NOTE).first()
        if trip is not None:
            return trip, False
        now = timezone.now()
        trip = Trip.objects.create(
            excavator=refs['excavator'],
            truck=refs['truck'],
            excavator_operator=excavator_operator,
            driver=driver,
            loading_shift=excavator_shift,
            unloading_shift=driver_shift,
            rock_type=refs['rock'],
            dump_point=refs['dump_point'],
            assigned_dump_point=refs['dump_point'],
            actual_dump_point=refs['dump_point'],
            planned_volume_m3=Decimal('25.00'),
            volume_m3=Decimal('25.00'),
            tonnage=Decimal('65.00'),
            loading_horizon='75',
            loading_block='52',
            transport_distance_km=Decimal('3.10'),
            note=TRIP_NOTE,
            status=TripStatus.COMPLETED,
            loaded_at=now,
            completed_at=now,
        )
        return trip, True

    # ---------------------------------------------------------------- сборка

    def build_stand(self, report):
        roles = self.ensure_roles()
        refs = self.ensure_references()

        driver, driver_access = self.ensure_employee(DRIVER, roles['driver'])
        excavator_operator, excavator_access = self.ensure_employee(
            EXCAVATOR_OPERATOR, roles['excavator_operator'],
        )

        driver_work, driver_work_created = self.ensure_work_assignment(
            driver, roles['driver'], refs['truck'],
        )
        excavator_work, excavator_work_created = self.ensure_work_assignment(
            excavator_operator, roles['excavator_operator'], refs['excavator'],
        )

        driver_shift, driver_shift_created = self.ensure_driver_shift(driver, driver_work)
        excavator_shift, excavator_shift_created = self.ensure_excavator_shift(
            excavator_operator, refs['excavator'],
        )
        haul_assignment, haul_created = self.ensure_haul_assignment(
            refs['truck'], refs['excavator'], report,
        )
        trip, trip_created = self.ensure_completed_trip(
            refs, driver, driver_shift, excavator_operator, excavator_shift,
        )

        for label, created in (
            ('рабочее назначение водителя', driver_work_created),
            ('рабочее назначение машиниста', excavator_work_created),
            ('смена водителя', driver_shift_created),
            ('смена машиниста', excavator_shift_created),
            ('назначение самосвала', haul_created),
            ('исторический рейс', trip_created),
        ):
            report.append(f'{label}: {"создано" if created else "уже было"}')

        state = OperationalStateVersion.objects.filter(key='production').first()
        return {
            'database': str(settings.DATABASES['default'].get('NAME') or ''),
            'driver': (driver, driver_access, driver_shift, driver_work),
            'excavator_operator': (
                excavator_operator, excavator_access, excavator_shift, excavator_work,
            ),
            'equipment': refs,
            'haul_assignment': haul_assignment,
            'trip': trip,
            'state': state,
        }

    def print_summary(self, summary, report):
        self.stdout.write('')
        for line in report:
            self.stdout.write(f'  {line}')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Локальный стенд SSE готов. База: {summary["database"]}'
        ))

        for label, spec, bundle in (
            ('Водитель', DRIVER, summary['driver']),
            ('Машинист экскаватора', EXCAVATOR_OPERATOR, summary['excavator_operator']),
        ):
            employee, access, shift, work_assignment = bundle
            shift_state = 'открыта' if shift and shift.closed_at is None else 'ЗАКРЫТА'
            self.stdout.write(
                f'  {label}: телефон {spec["phone"]}, код {spec["access_code"]}, '
                f'роль {spec["role_code"]}, employee_id={employee.pk}, '
                f'access_id={access.pk}, shift_id={shift.pk if shift else None} ({shift_state}), '
                f'work_assignment_id={work_assignment.pk}'
            )

        equipment = summary['equipment']
        truck = equipment['truck']
        excavator = equipment['excavator']
        haul = summary['haul_assignment']
        trip = summary['trip']
        self.stdout.write(
            f'  Техника: самосвал {truck.garage_number} (id={truck.pk}), '
            f'экскаватор {excavator.garage_number} (id={excavator.pk})'
        )
        self.stdout.write(
            f'  Путевое назначение: id={haul.pk if haul else None}, '
            f'статус={getattr(haul, "status", None)}, '
            f'рейс (завершён): id={trip.pk if trip else None}'
        )
        state = summary['state']
        if state is not None:
            self.stdout.write(
                f'  Оперативное состояние: version={state.version}, '
                f'journal_epoch={state.journal_epoch}'
            )
        else:
            self.stdout.write(
                '  Оперативное состояние ещё не создано: первое бизнес-действие '
                'создаст key=production.'
            )
        self.stdout.write(
            '  Вход: POST / с phone+access_code (как users/views.py:1424-1426). '
            'Коды — одноразовые локальные, не из production.'
        )
        self.stdout.write(
            '  Внимание: незакрытых рейсов не создаётся намеренно — иначе экран '
            'машиниста запретит погрузку. Первый рейс стенда создаётся нажатием '
            '«Погрузил» в приложении машиниста.'
        )
