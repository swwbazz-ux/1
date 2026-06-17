from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
    TruckCapacityRule,
)
from reports.models import ReportTemplate, ReportType
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role


class Command(BaseCommand):
    help = 'Создает демонстрационные данные для показа MVP производственного контура.'

    def handle(self, *args, **options):
        call_command('seed_mvp_roles', with_demo_users=True)

        employees = self.get_demo_employees()
        truck, excavator = self.get_demo_equipment()
        rock = self.get_demo_rock()
        dump_point = self.get_demo_dump_point()
        dormitory_section = self.get_demo_dormitory_section()

        DriverPrimaryRegistration.objects.update_or_create(
            employee=employees['driver'],
            defaults={
                'shift_type': 'day',
                'truck': truck,
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
        pending_truck = self.get_or_create_equipment(
            equipment_type_name='Самосвал',
            model_name='БЕЛАЗ демо',
            garage_number='ДЕМО-11',
            body_volume_m3=Decimal('38.00'),
            payload_tons=Decimal('90.00'),
        )
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
            status=TripStatus.ACTIVE,
        ).first()
        if not active_trip:
            active_trip = Trip.objects.create(
                excavator=excavator,
                truck=truck,
                excavator_operator=employees['excavator_operator'],
                loading_shift=excavator_shift,
                rock_type=rock,
                dump_point=dump_point,
                volume_m3=volume,
                tonnage=tonnage,
                status=TripStatus.ACTIVE,
            )

        completed_truck = self.get_or_create_equipment(
            equipment_type_name='Самосвал',
            model_name='БЕЛАЗ демо',
            garage_number='ДЕМО-12',
            body_volume_m3=Decimal('38.00'),
            payload_tons=Decimal('90.00'),
        )
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
                volume_m3=volume,
                tonnage=tonnage,
                status=TripStatus.COMPLETED,
                completed_at=timezone.now(),
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
                    'volume_m3',
                    'tonnage',
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

        self.stdout.write(self.style.SUCCESS('Демо-сценарий MVP подготовлен.'))
        self.stdout.write('Коды доступа: 1000 админ, 2000 водитель, 3000 машинист, 4000 горный мастер, 5000 диспетчер.')
        self.stdout.write(f'Активный рейс: {active_trip}')

    def get_demo_employees(self):
        role_by_code = {role.code: role for role in Role.objects.all()}
        demo = {
            'admin': ('Администратор MVP', 'admin', '1000'),
            'driver': ('Водитель MVP', 'driver', '2000'),
            'excavator_operator': ('Машинист экскаватора MVP', 'excavator_operator', '3000'),
            'mining_master': ('Горный мастер MVP', 'mining_master', '4000'),
            'dispatcher': ('Диспетчер MVP', 'dispatcher', '5000'),
        }
        employees = {}
        for key, (full_name, role_code, access_code) in demo.items():
            employee, _ = Employee.objects.update_or_create(full_name=full_name, defaults={'is_active': True})
            EmployeeAccess.objects.update_or_create(
                access_code=access_code,
                defaults={'employee': employee, 'role': role_by_code[role_code], 'is_active': True},
            )
            employees[key] = employee
        return employees

    def get_demo_equipment(self):
        truck = Equipment.objects.filter(equipment_type__name='Самосвал', is_active=True).order_by('garage_number').first()
        if not truck:
            truck = self.get_or_create_equipment(
                equipment_type_name='Самосвал',
                model_name='БЕЛАЗ демо',
                garage_number='ДЕМО-10',
                body_volume_m3=Decimal('38.00'),
                payload_tons=Decimal('90.00'),
            )
        excavator = Equipment.objects.filter(equipment_type__name='Экскаватор', is_active=True).order_by('garage_number').first()
        if not excavator:
            excavator = self.get_or_create_equipment(
                equipment_type_name='Экскаватор',
                model_name='Экскаватор демо',
                garage_number='ДЕМО-1',
                body_volume_m3=Decimal('12.00'),
                payload_tons=None,
            )
        return truck, excavator

    def get_or_create_equipment(self, equipment_type_name, model_name, garage_number, body_volume_m3, payload_tons):
        equipment_type, _ = EquipmentType.objects.update_or_create(
            name=equipment_type_name,
            defaults={'is_active': True},
        )
        model, _ = EquipmentModel.objects.update_or_create(
            equipment_type=equipment_type,
            name=model_name,
            defaults={
                'body_volume_m3': body_volume_m3,
                'payload_tons': payload_tons,
                'is_active': True,
            },
        )
        equipment, _ = Equipment.objects.update_or_create(
            garage_number=garage_number,
            defaults={
                'equipment_type': equipment_type,
                'model': model,
                'is_own': True,
                'is_active': True,
            },
        )
        return equipment

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
