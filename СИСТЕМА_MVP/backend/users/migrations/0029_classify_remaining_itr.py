from django.db import migrations

# The owner classified every remaining unclassified position as ITR.
REMAINING_ITR_POSITIONS = (
    'Ведущий специалист',
    'Специалист',
    'Ведущий инженер по охране окружающей среды',
    'Механик по выпуску и ремонту горно-транспортного оборудования',
    'Механик по ремонту вспомогательной техники',
    'Механик по ремонту технологических самосвалов',
    'Механик по ремонту экскаваторов',
    'Механик по техническому обслуживанию',
    'Ведущий инженер по БДД',
    'Ведущий специалист по промышленной безопасности и охране труда',
)


def _normalize(name):
    return ' '.join(name.casefold().replace('ё', 'е').split())


def classify_remaining_itr(apps, schema_editor):
    position = apps.get_model('users', 'PersonnelPosition')
    catalogue = position.objects.using(schema_editor.connection.alias)
    targets = {_normalize(name) for name in REMAINING_ITR_POSITIONS}
    for row in catalogue.filter(settlement_category='').only('name').iterator():
        if _normalize(row.name) in targets:
            catalogue.filter(pk=row.pk, settlement_category='').update(
                settlement_category='itr',
            )


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0028_classify_storekeeper_worker'),
    ]

    operations = [
        migrations.RunPython(classify_remaining_itr, migrations.RunPython.noop),
    ]
