from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    DumpPoint,
    Equipment,
    RockType,
    TruckCapacityRule,
)
from reports.models import PilotFeedback, ReportTemplate, ReportType
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role


class Command(BaseCommand):
    help = 'Создает демонстрационные данные для показа MVP производственного контура.'

    def handle(self, *args, **options):
        call_command('seed_mvp_roles', with_demo_users=True)

        employees = self.get_demo_employees()
        trucks, excavator = self.get_demo_equipment()
        truck = trucks[0]
        rock = self.get_demo_rock()
        dump_point = self.get_demo_dump_point()
        dormitory_section = self.get_demo_dormitory_section()

        DriverPrimaryRegistration.objects.update_or_create(
            employee=employees['driver'],
            defaults={
                'dormitory_section': dormitory_section,
            },
        )

        driver_shift = self.get_open_shift(
            employee=employees['driver'],
            equipment=truck,
            shift_type='day',
            start_fuel=Decimal('100.00'),
            start_mileage=Decimal('2500.00'),
            start_engine_hours=Decimal('700.00'),
        )
        excavator_shift = self.get_open_shift(
            employee=employees['excavator_operator'],
            equipment=excavator,
            shift_type='day',
            start_fuel=None,
            start_mileage=None,
            start_engine_hours=Decimal('1200.00'),
        )

        accepted_assignment = self.get_or_create_haul_assignment(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.ACCEPTED,
            assigned_by=employees['mining_master'],
            accepted_at=timezone.now(),
        )
        pending_truck = trucks[1] if len(trucks) > 1 else None
        if pending_truck:
            self.get_or_create_haul_assignment(
                truck=pending_truck,
                excavator=excavator,
                status=AssignmentStatus.PENDING,
                assigned_by=employees['mining_master'],
                accepted_at=None,
            )

        volume = self.get_trip_volume(truck, rock)
        tonnage = self.get_trip_tonnage(volume, rock)
        active_trip = Trip.objects.filter(
            truck=truck,
            excavator=excavator,
            status__in=(TripStatus.ACTIVE, TripStatus.LOADED_WAITING_UNLOAD),
        ).first()
        if not active_trip:
            active_trip = Trip.objects.create(
                excavator=excavator,
                truck=truck,
                excavator_operator=employees['excavator_operator'],
                loading_shift=excavator_shift,
                rock_type=rock,
                dump_point=dump_point,
                planned_volume_m3=Decimal('7000.00'),
                volume_m3=volume,
                tonnage=tonnage,
                loading_horizon='75',
                loading_block='52',
                transport_distance_km=Decimal('3.10'),
                downtime_text='зачистка забоя',
                note='демо рейс на разгрузку',
                status=TripStatus.LOADED_WAITING_UNLOAD,
            )
        else:
            active_trip.planned_volume_m3 = Decimal('7000.00')
            active_trip.loading_horizon = '75'
            active_trip.loading_block = '52'
            active_trip.transport_distance_km = Decimal('3.10')
            active_trip.downtime_text = 'зачистка забоя'
            active_trip.note = 'демо рейс на разгрузку'
            active_trip.status = TripStatus.LOADED_WAITING_UNLOAD
            active_trip.save(update_fields=[
                'planned_volume_m3',
                'loading_horizon',
                'loading_block',
                'transport_distance_km',
                'downtime_text',
                'note',
                'status',
            ])

        completed_truck = trucks[2] if len(trucks) > 2 else None
        if completed_truck:
            if not Trip.objects.filter(truck=completed_truck, status=TripStatus.COMPLETED).exists():
                Trip.objects.create(
                    excavator=excavator,
                    truck=completed_truck,
                    excavator_operator=employees['excavator_operator'],
                    driver=employees['driver'],
                    loading_shift=excavator_shift,
                    unloading_shift=driver_shift,
                    rock_type=rock,
                    dump_point=dump_point,
                    planned_volume_m3=Decimal('7000.00'),
                    volume_m3=volume,
                    tonnage=tonnage,
                    loading_horizon='75',
                    loading_block='52',
                    transport_distance_km=Decimal('3.10'),
                    downtime_text='ожидание разгрузки',
                    note='демо завершенный рейс для отчета заказчику',
                    status=TripStatus.COMPLETED,
                    completed_at=timezone.now(),
                )
            else:
                Trip.objects.filter(truck=completed_truck, status=TripStatus.COMPLETED).update(
                    planned_volume_m3=Decimal('7000.00'),
                    loading_horizon='75',
                    loading_block='52',
                    transport_distance_km=Decimal('3.10'),
                    downtime_text='ожидание разгрузки',
                    note='демо завершенный рейс для отчета заказчику',
                )

        ReportTemplate.objects.update_or_create(
            name='Демо отчет по объемам',
            defaults={
                'report_type': ReportType.SHIFT_VOLUME,
                'columns': [
                    'truck',
                    'excavator',
                    'rock_type',
                    'dump_point',
                    'planned_volume_m3',
                    'volume_m3',
                    'tonnage',
                    'loading_horizon',
                    'loading_block',
                    'transport_distance_km',
                    'loading_shift',
                    'unloading_shift',
                    'is_carryover',
                    'completed_at',
                ],
                'created_by': employees['admin'],
                'updated_by': employees['admin'],
                'is_active': True,
            },
        )
        self.seed_demo_downtimes(
            equipment=excavator,
            employee=employees['mechanic'],
            source_text=active_trip.downtime_text,
        )
        self.seed_demo_pilot_feedback(employees['dispatcher'])

        self.stdout.write(self.style.SUCCESS('Демо-сценарий MVP подготовлен.'))
        self.stdout.write(
            'Коды доступа: 100000 админ, 200000 водитель, 300000 машинист, 400000 горный мастер, '
            '500000 диспетчер, 600000 руководство, 700000 механик.'
        )
        self.stdout.write(f'Активный рейс: {active_trip}')

    def get_demo_employees(self):
        role_by_code = {role.code: role for role in Role.objects.all()}
        demo = {
            'admin': ('Администратор MVP', 'admin', '100000', '+79000000001'),
            'driver': ('Водитель MVP', 'driver', '200000', '+79000000002'),
            'excavator_operator': ('Машинист экскаватора MVP', 'excavator_operator', '300000', '+79000000003'),
            'mining_master': ('Горный мастер MVP', 'mining_master', '400000', '+79000000004'),
            'dispatcher': ('Диспетчер MVP', 'dispatcher', '500000', '+79000000005'),
            'manager': ('Руководство MVP', 'manager', '600000', '+79000000006'),
            'mechanic': ('Механик MVP', 'mechanic', '700000', '+79000000007'),
        }
        employees = {}
        for key, (full_name, role_code, access_code, phone) in demo.items():
            employee, _ = Employee.objects.update_or_create(full_name=full_name, defaults={'is_active': True, 'phone': phone})
            EmployeeAccess.objects.update_or_create(
                employee=employee,
                role=role_by_code[role_code],
                defaults={
                    'access_code': access_code,
                    'is_active': True,
                    'status': EmployeeAccess.Status.ACTIVATED,
                },
            )
            employees[key] = employee
        return employees

    def get_demo_equipment(self):
        trucks = list(
            Equipment.objects
            .filter(equipment_type__name='Самосвал', is_active=True)
            .exclude(garage_number__istartswith='ДЕМО')
            .order_by('garage_number')
        )
        if not trucks:
            raise CommandError('В справочнике нет активных самосвалов. Сначала загрузите технику в справочники.')
        excavator = (
            Equipment.objects
            .filter(equipment_type__name='Экскаватор', is_active=True)
            .exclude(garage_number__istartswith='ДЕМО')
            .order_by('garage_number')
            .first()
        )
        if not excavator:
            raise CommandError('В справочнике нет активных экскаваторов. Сначала загрузите технику в справочники.')
        return trucks, excavator

    def get_demo_rock(self):
        rock = RockType.objects.filter(is_active=True, density__isnull=False).order_by('name').first()
        if rock:
            return rock
        rock, _ = RockType.objects.update_or_create(
            name='Руда демо',
            defaults={
                'density': Decimal('2.50'),
                'loosening_factor': Decimal('1.30'),
                'is_active': True,
            },
        )
        return rock

    def get_demo_dump_point(self):
        dump_point = DumpPoint.objects.filter(is_active=True).order_by('name').first()
        if dump_point:
            return dump_point
        dump_point, _ = DumpPoint.objects.update_or_create(name='ККД демо', defaults={'is_active': True})
        return dump_point

    def get_demo_dormitory_section(self):
        section = DormitorySection.objects.select_related('block__dormitory').first()
        if section:
            return section
        dormitory, _ = Dormitory.objects.update_or_create(number='5', defaults={'is_active': True})
        block, _ = DormitoryBlock.objects.update_or_create(dormitory=dormitory, name='Блок 1')
        section, _ = DormitorySection.objects.update_or_create(
            block=block,
            name='А',
            defaults={'day_capacity': 3, 'night_capacity': 3},
        )
        return section

    def seed_demo_downtimes(self, equipment, employee, source_text):
        now = timezone.now()
        open_reason, _ = DowntimeReason.objects.update_or_create(
            name='Демо диагностика механической службы',
            defaults={
                'equipment_type': equipment.equipment_type,
                'is_critical': True,
                'is_active': True,
            },
        )
        closed_reason, _ = DowntimeReason.objects.update_or_create(
            name='Демо плановое обслуживание завершено',
            defaults={
                'equipment_type': equipment.equipment_type,
                'is_critical': False,
                'is_active': True,
            },
        )
        self.upsert_demo_downtime(
            equipment=equipment,
            employee=employee,
            reason=open_reason,
            started_at=now - timedelta(minutes=80),
            ended_at=None,
            comment=source_text or 'Демо-простой: механическая служба ведет диагностику',
        )
        self.upsert_demo_downtime(
            equipment=equipment,
            employee=employee,
            reason=closed_reason,
            started_at=now - timedelta(hours=5),
            ended_at=now - timedelta(hours=3, minutes=20),
            comment='Демо-простой: плановое обслуживание завершено',
        )

    def upsert_demo_downtime(self, equipment, employee, reason, started_at, ended_at, comment):
        event = DowntimeEvent.objects.filter(
            equipment=equipment,
            reason=reason,
            comment=comment,
        ).first()
        if event:
            event.employee = employee
            event.started_at = started_at
            event.ended_at = ended_at
            event.save(update_fields=['employee', 'started_at', 'ended_at'])
            return event
        return DowntimeEvent.objects.create(
            equipment=equipment,
            employee=employee,
            reason=reason,
            started_at=started_at,
            ended_at=ended_at,
            comment=comment,
        )

    def seed_demo_pilot_feedback(self, employee):
        PilotFeedback.objects.update_or_create(
            title='Демо-замечание: сверить почасовую группировку с диспетчерской',
            defaults={
                'category': 'report',
                'priority': 'p2',
                'status': 'new',
                'screen': 'Отчет по объемам',
                'description': 'На пилоте нужно проверить, достаточно ли группировки по часу вместо старой широкой почасовой матрицы.',
                'decision': 'Решение принять после сверки с диспетчерской службой.',
                'created_by': employee,
            },
        )

    def get_open_shift(self, employee, equipment, shift_type, start_fuel, start_mileage, start_engine_hours):
        shift = EmployeeShift.objects.filter(employee=employee, closed_at__isnull=True).order_by('-opened_at').first()
        if shift:
            return shift
        return EmployeeShift.objects.create(
            employee=employee,
            shift_type=shift_type,
            equipment=equipment,
            start_fuel=start_fuel,
            start_mileage=start_mileage,
            start_engine_hours=start_engine_hours,
            opened_at=timezone.now(),
            opened_by=employee,
        )

    def get_or_create_haul_assignment(self, truck, excavator, status, assigned_by, accepted_at):
        assignment = HaulAssignment.objects.filter(
            truck=truck,
            excavator=excavator,
            status=status,
            ended_at__isnull=True,
        ).first()
        if assignment:
            assignment.assigned_by = assigned_by
            if accepted_at:
                assignment.accepted_at = accepted_at
            assignment.save(update_fields=['assigned_by', 'accepted_at'])
            return assignment
        return HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=status,
            assigned_by=assigned_by,
            accepted_at=accepted_at,
        )

    def get_trip_volume(self, truck, rock):
        if truck.model:
            rule = TruckCapacityRule.objects.filter(equipment_model=truck.model, rock_type=rock).first()
            if rule:
                return rule.volume_m3
            if truck.model.body_volume_m3:
                return truck.model.body_volume_m3
        return Decimal('38.00')

    def get_trip_tonnage(self, volume, rock):
        if not volume or not rock.density:
            return None
        return (Decimal(volume) * Decimal(rock.density)).quantize(Decimal('0.01'))
