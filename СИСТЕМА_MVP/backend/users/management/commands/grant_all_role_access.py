"""Выдать одному человеку доступ во все рабочие приложения.

Администратору нужно уметь работать самому, а не только «от имени» кого-то:
если ни горного мастера, ни диспетчера нет в смене, назначать комплексы и
самосвалы всё равно должен кто-то. Вход под своей фамилией честнее входа от
чужого имени — в журнале остаётся тот, кто действительно нажал кнопку.

Пинкод берём тот же, что у уже работающего доступа: помнить разные коды для
каждого приложения человек не будет, а вход всё равно разводится по ролям —
каждое приложение ищет доступ только своей роли.

Запуск:
    python manage.py grant_all_role_access --phone +79991112233
    python manage.py grant_all_role_access --phone +79991112233 --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from users.forms import normalize_phone
from users.models import Employee, EmployeeAccess, Role
from users.protected_cards import allow_protected_card_write
from users.role_apps import ROLE_APPS_BY_CODE


class Command(BaseCommand):
    help = 'Выдаёт сотруднику доступ во все рабочие приложения с его текущим пинкодом.'

    def add_arguments(self, parser):
        parser.add_argument('--phone', required=True, help='Телефон сотрудника')
        parser.add_argument(
            '--position',
            default='',
            help='Новая должность в карточке (например «Системный администратор»).',
        )
        parser.add_argument(
            '--protect',
            action='store_true',
            help='Защитить карточку: её не изменит ни импорт из отдела кадров, ни другой администратор.',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Записать в базу. Без этого флага только показывает, что будет сделано.',
        )

    def handle(self, *args, **options):
        phone = normalize_phone(options['phone'])
        if not phone:
            raise CommandError('Не разобрал телефон.')

        employees = [
            employee
            for employee in Employee.objects.filter(is_active=True)
            if normalize_phone(employee.phone) == phone
        ]
        if not employees:
            raise CommandError(f'Сотрудник с телефоном {options["phone"]} не найден.')
        if len(employees) > 1:
            names = ', '.join(employee.full_name for employee in employees)
            raise CommandError(f'На этот телефон несколько сотрудников: {names}')
        employee = employees[0]

        existing = list(
            EmployeeAccess.objects
            .select_related('role')
            .filter(employee=employee)
        )
        by_role = {access.role.code: access for access in existing}

        # Пинкод берём у рабочего доступа: у неактивированных его ещё нет.
        source = next(
            (
                access
                for access in existing
                if access.is_active
                and access.status == EmployeeAccess.Status.ACTIVATED
                and (access.access_code or '').isdigit()
                and len(access.access_code) == 6
            ),
            None,
        )
        if source is None:
            raise CommandError(
                'У сотрудника нет ни одного рабочего доступа с шестизначным пинкодом — '
                'сначала он должен завести пинкод сам через /start/.'
            )

        roles = (
            Role.objects
            .filter(is_active=True, code__in=list(ROLE_APPS_BY_CODE))
            .order_by('name')
        )

        self.stdout.write(f'Сотрудник: {employee.full_name} ({options["phone"]})')
        self.stdout.write(f'Пинкод берём от роли «{source.role.name}».')

        to_create, to_repair, already = [], [], []
        for role in roles:
            access = by_role.get(role.code)
            if access is None:
                to_create.append(role)
            elif (
                access.is_active
                and access.status == EmployeeAccess.Status.ACTIVATED
                and access.access_code == source.access_code
            ):
                already.append(role)
            else:
                to_repair.append(access)

        for role in to_create:
            self.stdout.write(f'  завести:   {role.name}')
        for access in to_repair:
            self.stdout.write(f'  поправить: {access.role.name} (был {access.status}, активен={access.is_active})')
        for role in already:
            self.stdout.write(f'  уже есть:  {role.name}')

        new_position = (options['position'] or '').strip()
        if new_position and new_position != employee.position:
            self.stdout.write(f'  должность: «{employee.position}» -> «{new_position}»')
        elif new_position:
            self.stdout.write('  должность: уже такая')
        if options['protect'] and not employee.is_protected:
            self.stdout.write('  защита:    включить')
        elif options['protect']:
            self.stdout.write('  защита:    уже включена')

        if not options['apply']:
            self.stdout.write(self.style.WARNING('Пробный прогон. Повторите с --apply, чтобы записать.'))
            return

        now = timezone.now()
        # Карточка может быть уже защищена — тогда без явного разрешения не
        # запишется даже эта команда: запрет стоит на уровне модели.
        with transaction.atomic(), allow_protected_card_write():
            employee_fields = []
            if new_position and new_position != employee.position:
                employee.position = new_position
                employee_fields.append('position')
            if options['protect'] and not employee.is_protected:
                employee.is_protected = True
                employee_fields.append('is_protected')
            if employee_fields:
                employee.save(update_fields=[*employee_fields, 'updated_at'])
            for role in to_create:
                EmployeeAccess.objects.create(
                    employee=employee,
                    role=role,
                    access_code=source.access_code,
                    status=EmployeeAccess.Status.ACTIVATED,
                    is_active=True,
                    activated_at=now,
                )
            for access in to_repair:
                access.access_code = source.access_code
                access.status = EmployeeAccess.Status.ACTIVATED
                access.is_active = True
                access.activated_at = access.activated_at or now
                access.blocked_at = None
                access.block_reason = ''
                access.deactivated_at = None
                access.save(update_fields=[
                    'access_code', 'status', 'is_active', 'activated_at',
                    'blocked_at', 'block_reason', 'deactivated_at',
                ])

        self.stdout.write(self.style.SUCCESS(
            f'Готово: заведено {len(to_create)}, поправлено {len(to_repair)}, '
            f'уже было {len(already)}.'
        ))
        if options['protect']:
            self.stdout.write(self.style.SUCCESS(
                'Карточка защищена: изменить её не сможет ни импорт из отдела '
                'кадров, ни другой администратор.'
            ))
