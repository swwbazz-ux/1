from django.db import migrations, models


def mark_equipment_positions(apps, schema_editor):
    positions = apps.get_model('users', 'PersonnelPosition').objects.using(schema_editor.connection.alias)
    titles = ('водитель', 'машинист', 'помощник машиниста')
    ids = []
    for pk, name in positions.exclude(settlement_category='itr').values_list('pk', 'name'):
        name = ' '.join(name.casefold().split())
        if any(name == title or name.startswith((title + ' ', title + '-')) for title in titles):
            ids.append(pk)
    positions.filter(pk__in=ids).update(requires_equipment=True)


class Migration(migrations.Migration):
    dependencies = [('users', '0030_merge_settlement_category_owner_shifts')]
    operations = [
        migrations.AddField(
            model_name='personnelposition', name='requires_equipment',
            field=models.BooleanField('Требуется техника', default=False, db_default=False),
        ),
        migrations.RunPython(mark_equipment_positions, migrations.RunPython.noop),
    ]
