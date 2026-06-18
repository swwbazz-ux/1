from django.db import migrations


MECHANIC_REASONS = [
    ('Диагностика', False),
    ('Текущий ремонт', True),
    ('Электрика', True),
    ('Гидравлика', True),
    ('Двигатель', True),
    ('Ходовая часть', True),
    ('ТО и обслуживание', False),
]


def seed_mechanic_reasons(apps, schema_editor):
    DowntimeReason = apps.get_model('downtimes', 'DowntimeReason')
    for name, is_critical in MECHANIC_REASONS:
        DowntimeReason.objects.update_or_create(
            name=name,
            defaults={
                'equipment_type': None,
                'is_critical': is_critical,
                'is_active': True,
            },
        )


def unseed_mechanic_reasons(apps, schema_editor):
    DowntimeReason = apps.get_model('downtimes', 'DowntimeReason')
    DowntimeReason.objects.filter(name__in=[name for name, _ in MECHANIC_REASONS], equipment_type__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('downtimes', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_mechanic_reasons, unseed_mechanic_reasons),
    ]
