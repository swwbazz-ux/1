from django.core.management.base import BaseCommand

from users.models import Employee, EmployeeAccess, Role


ROLES = [
    ('admin', 'Администратор'),
    ('driver', 'Водитель самосвала'),
    ('excavator_operator', 'Машинист экскаватора'),
    ('mining_master', 'Горный мастер'),
    ('dispatcher', 'Диспетчер'),
    ('mechanic', 'Механик'),
    ('manager', 'Руководство'),
]


DEMO_USERS = [
    ('Администратор MVP', 'admin', '100000', '+79000000001'),
    ('Водитель MVP', 'driver', '200000', '+79000000002'),
    ('Машинист экскаватора MVP', 'excavator_operator', '300000', '+79000000003'),
    ('Горный мастер MVP', 'mining_master', '400000', '+79000000004'),
    ('Диспетчер MVP', 'dispatcher', '500000', '+79000000005'),
    ('Руководство MVP', 'manager', '600000', '+79000000006'),
    ('Механик MVP', 'mechanic', '700000', '+79000000007'),
]


class Command(BaseCommand):
    help = 'Создает роли первой очереди и, опционально, демо-доступы для локальной проверки.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--with-demo-users',
            action='store_true',
            help='Создать демо-сотрудников и коды доступа.',
        )

    def handle(self, *args, **options):
        for code, name in ROLES:
            Role.objects.update_or_create(code=code, defaults={'name': name, 'is_active': True})
        self.stdout.write(f'Роли созданы/обновлены: {len(ROLES)}')

        if options['with_demo_users']:
            for full_name, role_code, access_code, phone in DEMO_USERS:
                employee, _ = Employee.objects.update_or_create(
                    full_name=full_name,
                    defaults={'is_active': True, 'phone': phone},
                )
                role = Role.objects.get(code=role_code)
                EmployeeAccess.objects.update_or_create(
                    employee=employee,
                    role=role,
                    defaults={
                        'access_code': access_code,
                        'is_active': True,
                        'status': EmployeeAccess.Status.ACTIVATED,
                    },
                )
            self.stdout.write('Демо-доступы созданы: 100000, 200000, 300000, 400000, 500000, 600000, 700000')

        self.stdout.write(self.style.SUCCESS('Готово.'))
