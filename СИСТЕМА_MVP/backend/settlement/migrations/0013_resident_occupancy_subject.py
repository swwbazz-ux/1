import django.db.models.deletion
from django.db import migrations, models


EXTERNAL_TYPES = ('CONTRACTOR', 'BUSINESS_TRIP', 'EXTERNAL_OTHER')


def _effective_end(row):
    values = [value for value in (row.ends_at, row.terminated_at) if value is not None]
    return min(values) if values else None


def _assert_no_interval_conflicts(rows, *, key_name):
    groups = {}
    for row in rows:
        groups.setdefault(getattr(row, key_name), []).append(row)
    conflicts = []
    for group_rows in groups.values():
        ordered = sorted(group_rows, key=lambda item: (item.starts_at, item.pk))
        for index, left in enumerate(ordered):
            left_end = _effective_end(left)
            for right in ordered[index + 1:]:
                if left_end is not None and right.starts_at >= left_end:
                    break
                right_end = _effective_end(right)
                if right_end is None or left.starts_at < right_end:
                    conflicts.append((left.pk, right.pk))
    if conflicts:
        raise RuntimeError(
            'settlement.0013 resident occupancy transition found interval conflicts '
            f'for {key_name}: {conflicts[:20]} (count={len(conflicts)}).'
        )


def forwards(apps, schema_editor):
    using = schema_editor.connection.alias
    Resident = apps.get_model('settlement', 'SettlementResident')
    Occupancy = apps.get_model('settlement', 'EmployeeBedOccupancy')

    external_without_sex = list(
        Resident.objects.using(using)
        .filter(resident_type__in=EXTERNAL_TYPES, external_sex__isnull=True)
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    if external_without_sex:
        raise RuntimeError(
            'settlement.0013 requires authoritative external_sex for every existing '
            f'external SettlementResident: count={len(external_without_sex)}, '
            f'pk={external_without_sex[:50]}.'
        )
    invalid_external = list(
        Resident.objects.using(using)
        .filter(resident_type__in=EXTERNAL_TYPES)
        .exclude(external_sex__in=('male', 'female'))
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    internal_with_external_sex = list(
        Resident.objects.using(using)
        .filter(resident_type='EMPLOYEE')
        .exclude(external_sex__isnull=True)
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    if invalid_external or internal_with_external_sex:
        raise RuntimeError(
            'settlement.0013 found invalid resident sex provenance: '
            f'external_pk={invalid_external[:50]}, internal_pk={internal_with_external_sex[:50]}.'
        )

    employee_ids = tuple(
        Occupancy.objects.using(using)
        .order_by('employee_id')
        .values_list('employee_id', flat=True)
        .distinct()
    )
    resident_by_employee = {}
    for employee_id in employee_ids:
        residents = list(
            Resident.objects.using(using)
            .filter(employee_id=employee_id)
            .order_by('pk')
        )
        if len(residents) > 1 or (
            residents and residents[0].resident_type != 'EMPLOYEE'
        ):
            raise RuntimeError(
                'settlement.0013 found ambiguous internal resident wrapper for '
                f'Employee pk={employee_id}.'
            )
        resident = residents[0] if residents else Resident.objects.using(using).create(
            resident_type='EMPLOYEE',
            employee_id=employee_id,
            external_sex=None,
        )
        resident_by_employee[employee_id] = resident.pk

    for occupancy in Occupancy.objects.using(using).order_by('pk'):
        occupancy.resident_id = resident_by_employee[occupancy.employee_id]
        occupancy.save(update_fields=['resident'])

    rows = list(Occupancy.objects.using(using).order_by('pk'))
    if any(row.resident_id is None for row in rows):
        raise RuntimeError('settlement.0013 left occupancy without SettlementResident.')
    _assert_no_interval_conflicts(rows, key_name='resident_id')
    _assert_no_interval_conflicts(rows, key_name='physical_bed_id')


def backwards(apps, schema_editor):
    using = schema_editor.connection.alias
    Occupancy = apps.get_model('settlement', 'EmployeeBedOccupancy')

    external_rows = list(
        Occupancy.objects.using(using)
        .filter(
            models.Q(resident__employee_id__isnull=True)
            | ~models.Q(resident__resident_type='EMPLOYEE')
        )
        .order_by('pk')
        .values_list('pk', 'resident_id')
    )
    if external_rows:
        raise RuntimeError(
            'settlement.0013 reverse cannot represent external occupancy as Employee: '
            f'count={len(external_rows)}, occupancy/resident={external_rows[:50]}.'
        )

    for occupancy in (
        Occupancy.objects.using(using)
        .select_related('resident')
        .order_by('pk')
    ):
        occupancy.employee_id = occupancy.resident.employee_id
        occupancy.save(update_fields=['employee'])


class Migration(migrations.Migration):

    dependencies = [
        ('settlement', '0012_m7_saved_previews'),
        ('users', '0017_employee_sex'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='employeebedoccupancy',
            options={
                'ordering': ['-settled_at', '-id'],
                'verbose_name': 'Размещение жильца на койко-месте',
                'verbose_name_plural': 'Размещения жильцов на койко-местах',
            },
        ),
        migrations.RemoveConstraint(
            model_name='settlementresident',
            name='settlement_resident_subject_valid',
        ),
        migrations.AddField(
            model_name='settlementresident',
            name='external_sex',
            field=models.CharField(
                blank=True,
                choices=[('male', 'Мужской'), ('female', 'Женский')],
                max_length=7,
                null=True,
                verbose_name='Пол внешнего жильца',
            ),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='resident',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='bed_occupancies',
                to='settlement.settlementresident',
                verbose_name='Жилец',
            ),
        ),
        migrations.AlterField(
            model_name='employeebedoccupancy',
            name='employee',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='bed_occupancies',
                to='users.employee',
                verbose_name='Сотрудник',
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(
            model_name='employeebedoccupancy',
            name='resident',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='bed_occupancies',
                to='settlement.settlementresident',
                verbose_name='Жилец',
            ),
        ),
        migrations.RemoveField(
            model_name='employeebedoccupancy',
            name='employee',
        ),
        migrations.AddConstraint(
            model_name='settlementresident',
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(
                            resident_type='EMPLOYEE',
                            employee__isnull=False,
                            full_name='',
                            position_title='',
                            organization='',
                            phone='',
                            external_sex__isnull=True,
                        )
                        & (models.Q(photo__isnull=True) | models.Q(photo=''))
                    )
                    | (
                        models.Q(
                            resident_type__in=EXTERNAL_TYPES,
                            employee__isnull=True,
                            created_by_access__isnull=False,
                        )
                        & ~models.Q(full_name='')
                        & ~models.Q(position_title='')
                        & ~models.Q(organization='')
                        & ~models.Q(phone='')
                    )
                ),
                name='settlement_resident_subject_valid',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementresident',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(resident_type='EMPLOYEE', external_sex__isnull=True)
                    | models.Q(
                        resident_type__in=EXTERNAL_TYPES,
                        external_sex__isnull=False,
                        external_sex__in=('male', 'female'),
                    )
                ),
                name='settlement_resident_external_sex_valid',
            ),
        ),
    ]
