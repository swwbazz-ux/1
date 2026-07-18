from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


SCHEDULE_CODE = 'individual_permanent_site'
SCHEDULE_NAME = 'Индивидуальный график — постоянно на участке'


def create_individual_schedule(apps, schema_editor):
    WorkSchedule = apps.get_model('users', 'WorkSchedule')
    WorkSchedule.objects.update_or_create(
        code=SCHEDULE_CODE,
        defaults={
            'name': SCHEDULE_NAME,
            'brigade_count': 0,
            'is_active': True,
        },
    )


def remove_individual_schedule(apps, schema_editor):
    Employee = apps.get_model('users', 'Employee')
    WorkSchedule = apps.get_model('users', 'WorkSchedule')
    schedule = WorkSchedule.objects.filter(code=SCHEDULE_CODE).first()
    if not schedule:
        return

    for employee in Employee.objects.filter(work_schedule_id=schedule.id).iterator():
        employee.work_schedule_id = None
        employee.brigade_number = None
        if not employee.rotation:
            employee.rotation = schedule.name
        employee.save(update_fields=['work_schedule', 'brigade_number', 'rotation'])
    schedule.delete()


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0013_normalize_department_work_schedule'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='workschedule',
            name='work_schedule_brigade_count_1_4',
        ),
        migrations.AlterField(
            model_name='workschedule',
            name='brigade_count',
            field=models.PositiveSmallIntegerField(
                default=2,
                validators=[MinValueValidator(0), MaxValueValidator(4)],
                verbose_name='Количество бригад',
            ),
        ),
        migrations.AddConstraint(
            model_name='workschedule',
            constraint=models.CheckConstraint(
                condition=models.Q(brigade_count__gte=0, brigade_count__lte=4),
                name='work_schedule_brigade_count_0_4',
            ),
        ),
        migrations.RunPython(create_individual_schedule, remove_individual_schedule),
    ]
