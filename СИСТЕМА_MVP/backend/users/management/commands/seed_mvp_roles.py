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
    ('Администратор MVP', 'admin', '1000'),
    ('Водитель MVP', 'driver', '2000'),
    ('Машинист экскаватора MVP', 'excavator_operator', '3000'),
    ('Горный мастер MVP', 'mining_master', '4000'),
    ('Диспетчер MVP', 'dispatcher', '5000'),
]


class Command(BaseCommand):
    help = 'Создает роли первой очереди и, опционально, демо-доступы для локальной проверки.'

    def add_arguments(self, parser):
        parser.add_argument('--with-demo-users', action='store_true', help='Создать демо-сотрудников и коды доступа.')

    def handle(self, *args, **options):
        for code, name in ROLES:
            Role.objects.update_or_create(code=code, defaults={'name': name, 'is_active': True})
        self.stdout.write(f'Роли созданы/обновлены: {len(ROLES)}')

        if options['with_demo_users']:
            for full_name, role_code, access_code in DEMO_USERS:
                employee, _ = Employee.objects.get_or_create(full_name=full_name, defaults={'is_active': True})
                role = Role.objects.get(code=role_code)
                EmployeeAccess.objects.update_or_create(
                    access_code=access_code,
                    defaults={'employee': employee, 'role': role, 'is_active': True},
                )
            self.stdout.write('Демо-доступы созданы: 1000, 2000, 3000, 4000, 5000')

        self.stdout.write(self.style.SUCCESS('Готово.'))
