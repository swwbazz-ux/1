from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0003_reporttemplate_group_by'),
    ]

    operations = [
        migrations.AddField(
            model_name='reporttemplate',
            name='column_labels',
            field=models.JSONField(blank=True, default=dict, verbose_name='Названия столбцов'),
        ),
    ]
