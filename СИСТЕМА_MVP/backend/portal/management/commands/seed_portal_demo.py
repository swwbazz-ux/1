from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from portal.models import Poll, PollOption, PortalStaffPermission, Publication
from users.models import Employee


class Command(BaseCommand):
    help = 'Создаёт локальный демонстрационный контент корпоративного портала. Команда идемпотентна.'

    def handle(self, *args, **options):
        admin = Employee.objects.filter(full_name='Администратор MVP', is_active=True).first()
        driver = Employee.objects.filter(full_name='Водитель MVP', is_active=True).first()
        if not admin or not driver:
            raise CommandError('Сначала выполните: python manage.py seed_mvp_roles --with-demo-users')

        today = timezone.localdate()
        driver.work_category = Employee.WorkCategory.DRIVER
        driver.position = 'Водитель карьерного самосвала'
        driver.birth_date = today.replace(year=max(today.year - 35, 1900))
        driver.hired_at = today.replace(year=max(today.year - 2, 1900))
        driver.save(update_fields=['work_category', 'position', 'birth_date', 'hired_at', 'updated_at'])

        admin.position = 'Системный администратор'
        admin.save(update_fields=['position', 'updated_at'])

        PortalStaffPermission.objects.update_or_create(
            employee=admin,
            defaults={
                'can_author': True,
                'can_edit': True,
                'can_publish': True,
                'receives_feedback': True,
                'is_active': True,
                'assigned_by': admin,
            },
        )

        now = timezone.now()
        Publication.objects.update_or_create(
            slug='portal-uchastka-2-otkryt',
            defaults={
                'title': 'Корпоративный портал участка № 2 открыт',
                'publication_type': Publication.Type.NEWS,
                'status': Publication.Status.PUBLISHED,
                'visibility': Publication.Visibility.BOTH,
                'audience': Publication.Audience.ALL,
                'summary': 'Единое место для новостей, историй сотрудников, опросов и рабочих приложений.',
                'body': (
                    'Это демонстрационная публикация локального стенда. Портал объединяет официальные новости '
                    'ООО «Коппер Рисорсез» и закрытые материалы для сотрудников участка № 2. '
                    'Производственные показатели будут поступать только из учётной системы.'
                ),
                'pin_to_dashboard': True,
                'author': admin,
                'editor': admin,
                'publisher': admin,
                'published_at': now,
            },
        )
        Publication.objects.update_or_create(
            slug='vazhnaya-informatsiya-demo',
            defaults={
                'title': 'Важная информация: ознакомьтесь с возможностями портала',
                'publication_type': Publication.Type.ANNOUNCEMENT,
                'status': Publication.Status.PUBLISHED,
                'visibility': Publication.Visibility.INTERNAL,
                'audience': Publication.Audience.ALL,
                'summary': 'Обязательное объявление демонстрирует подтверждение «Ознакомился».',
                'body': (
                    'В закрытой части можно читать новости, участвовать в опросах, открыть своё рабочее '
                    'приложение и написать руководству. Это демонстрационный материал, а не производственное распоряжение.'
                ),
                'is_mandatory': True,
                'pin_to_dashboard': True,
                'author': admin,
                'editor': admin,
                'publisher': admin,
                'published_at': now,
            },
        )
        Publication.objects.update_or_create(
            slug='lyudi-uchastka-demo',
            defaults={
                'title': 'Люди участка № 2: работа водителя карьерного самосвала',
                'publication_type': Publication.Type.INTERVIEW,
                'status': Publication.Status.PUBLISHED,
                'visibility': Publication.Visibility.BOTH,
                'audience': Publication.Audience.ALL,
                'summary': 'Демонстрация будущего формата добровольных интервью с сотрудниками.',
                'body': (
                    'Это демонстрационная история без реальных персональных высказываний. '
                    'Настоящие интервью будут публиковаться только после согласования с сотрудником.'
                ),
                'subject_employee': driver,
                'public_consent_confirmed': True,
                'author': admin,
                'editor': admin,
                'publisher': admin,
                'published_at': now,
            },
        )

        poll, _ = Poll.objects.update_or_create(
            title='Какие материалы вы хотите видеть чаще?',
            defaults={
                'description': 'Анонимный демонстрационный опрос. Промежуточные результаты скрыты.',
                'status': Poll.Status.OPEN,
                'is_anonymous': True,
                'audience': Publication.Audience.ALL,
                'opens_at': now,
                'author': admin,
                'publisher': admin,
            },
        )
        if not poll.votes.exists():
            poll.options.all().delete()
            PollOption.objects.bulk_create(
                [
                    PollOption(poll=poll, text='Истории сотрудников', order=1),
                    PollOption(poll=poll, text='Новости участка', order=2),
                    PollOption(poll=poll, text='Достижения и итоги', order=3),
                ]
            )

        self.stdout.write(self.style.SUCCESS('Демонстрационный контент портала готов.'))
        self.stdout.write('Вход администратора: +79000000001 / 100000')
        self.stdout.write('Вход сотрудника: +79000000002 / 200000')
