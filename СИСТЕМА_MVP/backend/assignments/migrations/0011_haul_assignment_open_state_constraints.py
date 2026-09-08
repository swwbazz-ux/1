from django.db import migrations, models
from django.db.models import Count


def validate_open_assignment_states(apps, schema_editor):
    HaulAssignment = apps.get_model('assignments', 'HaulAssignment')
    for status in ('accepted', 'pending'):
        duplicates = list(
            HaulAssignment.objects
            .filter(status=status, ended_at__isnull=True)
            .values('truck_id')
            .annotate(total=Count('id'))
            .filter(total__gt=1)
            .values_list('truck_id', 'total')
        )
        if duplicates:
            raise RuntimeError(
                f'Нельзя установить ограничение: дубли открытых назначений '
                f'со статусом {status}: {duplicates}'
            )


class Migration(migrations.Migration):
    dependencies = [
        ('assignments', '0010_excavatordumppointsetting'),
    ]

    operations = [
        migrations.RunPython(validate_open_assignment_states, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='haulassignment',
            constraint=models.UniqueConstraint(
                condition=models.Q(status='accepted', ended_at__isnull=True),
                fields=('truck',),
                name='uniq_open_accepted_haul_truck',
            ),
        ),
        migrations.AddConstraint(
            model_name='haulassignment',
            constraint=models.UniqueConstraint(
                condition=models.Q(status='pending', ended_at__isnull=True),
                fields=('truck',),
                name='uniq_open_pending_haul_truck',
            ),
        ),
    ]
