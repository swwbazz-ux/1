from django.db import migrations

# The owner classified the storekeeper's position as a worker, not ITR.
STOREKEEPER_NAMES = ('Кладовщик',)


def _normalize(name):
    return ' '.join(name.casefold().replace('ё', 'е').split())


def classify_storekeeper(apps, schema_editor):
    position = apps.get_model('users', 'PersonnelPosition')
    catalogue = position.objects.using(schema_editor.connection.alias)
    targets = {_normalize(name) for name in STOREKEEPER_NAMES}
    for row in catalogue.filter(settlement_category='').only('name').iterator():
        if _normalize(row.name) in targets:
            catalogue.filter(pk=row.pk, settlement_category='').update(
                settlement_category='worker',
            )


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0027_personnel_position_settlement_category'),
    ]

    operations = [
        migrations.RunPython(classify_storekeeper, migrations.RunPython.noop),
    ]
