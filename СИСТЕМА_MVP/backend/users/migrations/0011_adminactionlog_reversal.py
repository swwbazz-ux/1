from django.db import migrations, models
import django.db.models.deletion


ACTION_CODES = {
    'ОУП: создан сотрудник': 'oup_employee_created',
    'ОУП: изменена карточка сотрудника': 'oup_employee_updated',
    'ОУП: удалено фото сотрудника': 'oup_employee_photo_removed',
    'ОУП: уволен сотрудник': 'oup_employee_dismissed',
    'ОУП: выдан первичный PIN': 'oup_access_issued',
    'ОУП: перевыпущен первичный PIN': 'oup_access_reissued',
    'ОУП: отключён доступ сотрудника': 'oup_access_deactivated',
    'ОУП: начата дневная смена': 'oup_period_started',
    'ОУП: начат рабочий период': 'oup_period_started',
    'ОУП: завершена дневная смена': 'oup_period_finished',
    'ОУП: завершён рабочий период': 'oup_period_finished',
    'ОУП: создан сотрудник массовым импортом': 'oup_bulk_employee_created',
    'ОУП: обновлена карточка массовым импортом': 'oup_bulk_employee_updated',
}


def fill_action_codes(apps, schema_editor):
    AdminActionLog = apps.get_model('users', 'AdminActionLog')
    for action, action_code in ACTION_CODES.items():
        AdminActionLog.objects.filter(action=action, action_code='').update(action_code=action_code)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_normalize_activated_employee_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='adminactionlog',
            name='action_code',
            field=models.CharField(blank=True, db_index=True, max_length=64, verbose_name='Код действия'),
        ),
        migrations.AddField(
            model_name='adminactionlog',
            name='reversal_of',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='reversal', to='users.adminactionlog', verbose_name='Отмененное действие'),
        ),
        migrations.AddField(
            model_name='adminactionlog',
            name='undo_payload',
            field=models.JSONField(blank=True, default=dict, verbose_name='Снимок для отмены'),
        ),
        migrations.RunPython(fill_action_codes, migrations.RunPython.noop),
    ]
