from datetime import date

from django.db import migrations, models


def set_initial_history_start(apps, schema_editor):
    tracking = apps.get_model('trips', 'DispatcherDutyTracking')
    tracking.objects.using(schema_editor.connection.alias).filter(pk=1).update(history_from=date(2026, 11, 24))


class Migration(migrations.Migration):
    dependencies = [('trips', '0019_archive_dispatcher_shifts_20261125')]

    operations = [
        migrations.AddField(
            model_name='dispatcherdutytracking',
            name='history_from',
            field=models.DateField('Показывать смены с', blank=True, null=True),
        ),
        migrations.RunPython(set_initial_history_start, migrations.RunPython.noop),
    ]
