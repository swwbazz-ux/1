import uuid

import django.db.models.deletion
from django.db import migrations, models


def _subject_employee_ids(Binding, Member, using):
    return tuple(sorted({
        *Binding.objects.using(using).exclude(employee_id=None).values_list('employee_id', flat=True),
        *Member.objects.using(using).exclude(employee_id=None).values_list('employee_id', flat=True),
    }))


def forward_resident_subjects(apps, schema_editor):
    Binding = apps.get_model('settlement', 'EmployeeAccommodationBinding')
    Member = apps.get_model('settlement', 'SettlementCohortMember')
    Resident = apps.get_model('settlement', 'SettlementResident')
    using = schema_editor.connection.alias

    resident_by_employee_id = {}
    for employee_id in _subject_employee_ids(Binding, Member, using):
        residents = list(
            Resident.objects.using(using)
            .filter(employee_id=employee_id)
            .order_by('pk')
        )
        if len(residents) > 1:
            raise RuntimeError(
                f'settlement.0011: Employee {employee_id} имеет {len(residents)} resident wrappers.',
            )
        if residents:
            resident = residents[0]
            if resident.resident_type != 'EMPLOYEE' or resident.employee_id != employee_id:
                raise RuntimeError(
                    f'settlement.0011: resident {resident.pk} конфликтует с Employee {employee_id}.',
                )
        else:
            resident = Resident.objects.using(using).create(
                stable_id=uuid.uuid4(),
                employee_id=employee_id,
                resident_type='EMPLOYEE',
                full_name='',
                photo=None,
                position_title='',
                organization='',
                phone='',
                status='ACTIVE',
                revision=1,
                archived_at=None,
                created_by_access_id=None,
                updated_by_access_id=None,
            )
        resident_by_employee_id[employee_id] = resident.pk

    for model in (Binding, Member):
        for employee_id, resident_id in resident_by_employee_id.items():
            model.objects.using(using).filter(employee_id=employee_id).update(
                resident_id=resident_id,
            )
        missing_ids = list(
            model.objects.using(using)
            .filter(resident_id=None)
            .order_by('pk')
            .values_list('pk', flat=True)
        )
        if missing_ids:
            raise RuntimeError(
                f'settlement.0011: {model.__name__} без resident: {missing_ids}.',
            )
        mismatched_ids = list(
            model.objects.using(using)
            .exclude(employee_id=models.F('resident__employee_id'))
            .order_by('pk')
            .values_list('pk', flat=True)
        )
        if mismatched_ids:
            raise RuntimeError(
                f'settlement.0011: {model.__name__} имеет конфликт subject mapping: '
                f'{mismatched_ids}.',
            )


def reverse_employee_subjects(apps, schema_editor):
    Binding = apps.get_model('settlement', 'EmployeeAccommodationBinding')
    Member = apps.get_model('settlement', 'SettlementCohortMember')
    using = schema_editor.connection.alias

    conflicts = []
    for model in (Binding, Member):
        ids = list(
            model.objects.using(using)
            .filter(
                models.Q(resident__employee_id=None)
                | ~models.Q(resident__resident_type='EMPLOYEE')
            )
            .order_by('pk')
            .values_list('pk', flat=True)
        )
        if ids:
            conflicts.append(f'{model.__name__}={ids}')
    if conflicts:
        raise RuntimeError(
            'settlement.0011 reverse запрещён: external resident subjects обнаружены; '
            f'{"; ".join(conflicts)}.',
        )

    for model in (Binding, Member):
        rows = tuple(
            model.objects.using(using)
            .order_by('pk')
            .values_list('pk', 'resident__employee_id')
        )
        for pk, employee_id in rows:
            model.objects.using(using).filter(pk=pk).update(employee_id=employee_id)
        missing_ids = list(
            model.objects.using(using)
            .filter(employee_id=None)
            .order_by('pk')
            .values_list('pk', flat=True)
        )
        if missing_ids:
            raise RuntimeError(
                f'settlement.0011 reverse: {model.__name__} без Employee: {missing_ids}.',
            )


class Migration(migrations.Migration):

    dependencies = [
        ('settlement', '0010_settlement_residents'),
    ]

    operations = [
        migrations.AlterField(
            model_name='employeeaccommodationbinding',
            name='employee',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='accommodation_bindings',
                to='users.employee',
                verbose_name='Сотрудник',
            ),
        ),
        migrations.AlterField(
            model_name='settlementcohortmember',
            name='employee',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_memberships',
                to='users.employee',
                verbose_name='Сотрудник',
            ),
        ),
        migrations.AddField(
            model_name='employeeaccommodationbinding',
            name='resident',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='accommodation_bindings',
                to='settlement.settlementresident',
                verbose_name='Жилец',
            ),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='resident',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_memberships',
                to='settlement.settlementresident',
                verbose_name='Жилец',
            ),
        ),
        migrations.RunPython(forward_resident_subjects, reverse_employee_subjects),
        migrations.RemoveConstraint(
            model_name='employeeaccommodationbinding',
            name='unique_employee_slot_binding_start',
        ),
        migrations.RemoveConstraint(
            model_name='settlementcohortmember',
            name='unique_employee_per_cohort',
        ),
        migrations.RemoveIndex(
            model_name='employeeaccommodationbinding',
            name='employee_binding_period_idx',
        ),
        migrations.RemoveIndex(
            model_name='settlementcohortmember',
            name='cohort_member_employee_idx',
        ),
        migrations.AddIndex(
            model_name='employeeaccommodationbinding',
            index=models.Index(
                fields=['resident', 'status', 'valid_from'],
                name='resident_binding_period_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='settlementcohortmember',
            index=models.Index(
                fields=['resident', 'participation_status', 'arrival_at'],
                name='cohort_member_resident_idx',
            ),
        ),
        migrations.AddConstraint(
            model_name='employeeaccommodationbinding',
            constraint=models.UniqueConstraint(
                fields=('resident', 'anchor_calendar_slot', 'valid_from'),
                name='unique_resident_slot_binding_start',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementcohortmember',
            constraint=models.UniqueConstraint(
                fields=('cohort', 'resident'),
                name='unique_resident_per_cohort',
            ),
        ),
        migrations.AlterField(
            model_name='employeeaccommodationbinding',
            name='resident',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='accommodation_bindings',
                to='settlement.settlementresident',
                verbose_name='Жилец',
            ),
        ),
        migrations.AlterField(
            model_name='settlementcohortmember',
            name='resident',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_memberships',
                to='settlement.settlementresident',
                verbose_name='Жилец',
            ),
        ),
        migrations.RemoveField(
            model_name='employeeaccommodationbinding',
            name='employee',
        ),
        migrations.RemoveField(
            model_name='settlementcohortmember',
            name='employee',
        ),
        migrations.AlterModelOptions(
            name='employeeaccommodationbinding',
            options={
                'ordering': ['resident_id', 'valid_from', 'pk'],
                'verbose_name': 'Постоянное жилищное закрепление жильца',
                'verbose_name_plural': 'Постоянные жилищные закрепления жильцов',
            },
        ),
        migrations.AlterModelOptions(
            name='settlementcohortmember',
            options={
                'ordering': ['cohort_id', 'resident_id', 'pk'],
                'verbose_name': 'Строка жилищного состава заезда',
                'verbose_name_plural': 'Строки жилищного состава заезда',
            },
        ),
    ]
