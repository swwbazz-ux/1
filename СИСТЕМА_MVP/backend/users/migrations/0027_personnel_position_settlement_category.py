from django.db import migrations, models


# The owner's ITR list and the unambiguous manual trades in the existing catalogue.
# Match official names rather than codes, which can differ in a populated database.
ITR_POSITIONS = (
    'Заместитель начальника участка',
    'Начальник участка',
    'Инженер по шинам',
    'Диспетчер по горно-транспортному оборудованию',
    'Делопроизводитель',
    'Техник-наладчик по АСУ ГТК',
    'Начальник АЗС',
    'Горный диспетчер',
    'Старший диспетчер',
    'Горный мастер',
    'Старший горный мастер',
    'Техник по учету',
    'Начальник склада',
    'Начальник отдела',
    'Старший механик',
    'Инженер по ТОиР',
)
WORKER_POSITIONS = (
    'Водитель автомобиля, занятый на транспортировании горной массы в технологическом процессе',
    'Водитель автомобиля грузового',
    'Водитель легкового автомобиля',
    'Водитель погрузчика',
    'Машинист экскаватора',
    'Помощник машиниста экскаватора',
    'Машинист бульдозера',
    'Машинист автогрейдера',
    'Машинист катка',
    'Машинист крана автомобильного',
    'Слесарь-ремонтник',
    'Слесарь-электрик по ремонту электрооборудования (Автоэлектрик)',
    'Электрогазосварщик',
    'Подсобный рабочий',
    'Грузчик-стропальщик',
)


def _normalize(name):
    return ' '.join(name.casefold().replace('ё', 'е').split())


def classify_positions(apps, schema_editor):
    position = apps.get_model('users', 'PersonnelPosition')
    catalogue = position.objects.using(schema_editor.connection.alias)
    categories = {
        _normalize(name): category
        for category, names in (('itr', ITR_POSITIONS), ('worker', WORKER_POSITIONS))
        for name in names
    }
    for row in catalogue.filter(settlement_category='').only('name').iterator():
        category = categories.get(_normalize(row.name))
        if category:
            catalogue.filter(pk=row.pk, settlement_category='').update(
                settlement_category=category,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0026_native_push_device'),
    ]

    operations = [
        migrations.AddField(
            model_name='personnelposition',
            name='settlement_category',
            field=models.CharField(
                blank=True,
                choices=[('', 'Не указана'), ('worker', 'Рабочий'), ('itr', 'ИТР')],
                default='',
                help_text='Для авторасселения рабочих и ИТР по разным комнатам.',
                max_length=8,
                verbose_name='Категория для расселения',
            ),
        ),
        migrations.RunPython(classify_positions, migrations.RunPython.noop),
    ]
