import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


def _noop(apps, schema_editor):
    pass


def _reverse_fail_closed(apps, schema_editor):
    Version = apps.get_model('rotations', 'ArrivalRosterVersion')
    unsafe = Version.objects.filter(
        models.Q(status__in=['confirmed', 'superseded'])
        | models.Q(based_on_version_id__isnull=False)
        | models.Q(confirmed_by_access_id__isnull=False)
        | models.Q(confirmed_at__isnull=False)
        | ~models.Q(confirmation_snapshot={})
        | ~models.Q(confirmation_sha256='')
        | models.Q(superseded_at__isnull=False)
    ).order_by('pk')
    count = unsafe.count()
    if count:
        pks = list(unsafe.values_list('pk', flat=True)[:50])
        raise RuntimeError(
            f'Fail-closed reverse rotations.0005: approval data exists; count={count}; PK={pks}'
        )


class Migration(migrations.Migration):
    dependencies = [('rotations', '0004_arrival_roster_employee_pool')]

    operations = [
        migrations.RemoveConstraint(
            model_name='arrivalrosterversion', name='arrival_version_status_t11',
        ),
        migrations.AddField(
            model_name='arrivalrosterversion', name='based_on_version',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='replacement_versions', to='rotations.arrivalrosterversion',
                verbose_name='Основана на версии',
            ),
        ),
        migrations.AddField(
            model_name='arrivalrosterversion', name='confirmation_sha256',
            field=models.CharField(
                blank=True, max_length=64,
                validators=[django.core.validators.RegexValidator(regex=r'^$|^[0-9a-f]{64}$')],
                verbose_name='SHA-256 утверждения',
            ),
        ),
        migrations.AddField(
            model_name='arrivalrosterversion', name='confirmation_snapshot',
            field=models.JSONField(blank=True, default=dict, verbose_name='Канонический снимок утверждения'),
        ),
        migrations.AddField(
            model_name='arrivalrosterversion', name='confirmed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Утверждена'),
        ),
        migrations.AddField(
            model_name='arrivalrosterversion', name='confirmed_by_access',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='confirmed_arrival_roster_versions', to='users.employeeaccess',
                verbose_name='Точный доступ утвердившего',
            ),
        ),
        migrations.AddField(
            model_name='arrivalrosterversion', name='superseded_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Заменена'),
        ),
        migrations.AlterField(
            model_name='arrivalrosterevent', name='action',
            field=models.CharField(
                choices=[
                    ('uploaded', 'Файл загружен'), ('reused', 'Повторная загрузка распознана'),
                    ('parsed', 'Предварительная проверка завершена'), ('resident_selected', 'Жилец выбран'),
                    ('resident_cleared', 'Сопоставление отменено'), ('participation_changed', 'Участие изменено'),
                    ('arrival_mode_changed', 'Способ прибытия изменён'), ('dates_changed', 'Даты изменены'),
                    ('notes_changed', 'Основание или комментарий изменены'), ('issue_resolved', 'Вопрос решён'),
                    ('issue_reopened', 'Вопрос возвращён на проверку'),
                    ('pool_created', 'Список сформирован из карточек сотрудников'),
                    ('pool_employee_added', 'Сотрудник добавлен в список'),
                    ('pool_external_added', 'Внешний жилец добавлен в список'),
                    ('confirmed', 'Версия окончательно утверждена'),
                ], max_length=24, verbose_name='Действие',
            ),
        ),
        migrations.AlterField(
            model_name='arrivalrosterversion', name='status',
            field=models.CharField(
                choices=[('draft', 'Черновик'), ('review_required', 'Требуется проверка'),
                         ('confirmed', 'Утверждена'), ('superseded', 'Заменена')],
                db_index=True, default='review_required', max_length=24,
                verbose_name='Состояние',
            ),
        ),
        migrations.AddConstraint(
            model_name='arrivalrosterversion',
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=['draft', 'review_required', 'confirmed', 'superseded']),
                name='arrival_version_status_t14a',
            ),
        ),
        migrations.AddConstraint(
            model_name='arrivalrosterversion',
            constraint=models.UniqueConstraint(
                condition=models.Q(status='confirmed'), fields=('watch_period',),
                name='uniq_arrival_confirmed_period',
            ),
        ),
        migrations.AddConstraint(
            model_name='arrivalrosterversion',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(status__in=['draft', 'review_required'], confirmed_by_access__isnull=True,
                             confirmed_at__isnull=True, confirmation_snapshot={}, confirmation_sha256='',
                             superseded_at__isnull=True)
                    | models.Q(status='confirmed', confirmed_by_access__isnull=False,
                               confirmed_at__isnull=False, confirmation_sha256__regex=r'^[0-9a-f]{64}$',
                               superseded_at__isnull=True) & ~models.Q(confirmation_snapshot={})
                    | models.Q(status='superseded', confirmed_by_access__isnull=False,
                               confirmed_at__isnull=False, confirmation_sha256__regex=r'^[0-9a-f]{64}$',
                               superseded_at__isnull=False) & ~models.Q(confirmation_snapshot={})
                ), name='arrival_version_approval_shape',
            ),
        ),
        migrations.RunPython(_noop, _reverse_fail_closed),
    ]
