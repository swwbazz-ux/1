from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shifts', '0018_excavatorshiftreadingconfirmation'),
    ]

    operations = [
        migrations.AddField(
            model_name='employeeshift',
            name='service_close_kind',
            field=models.CharField(blank=True, choices=[('neglected', 'Сотрудник не закрыл сам'), ('coordinated', 'По согласованию с диспетчером'), ('auto_expired', 'Автоматически через 13 часов')], db_index=True, max_length=16, verbose_name='Вид служебного закрытия'),
        ),
        migrations.AddField(
            model_name='employeeshift',
            name='service_close_note',
            field=models.CharField(blank=True, max_length=255, verbose_name='Заметка о закрытии'),
        ),
    ]
