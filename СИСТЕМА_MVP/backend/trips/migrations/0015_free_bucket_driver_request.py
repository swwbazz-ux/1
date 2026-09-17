import django.db.models.deletion
from django.db import migrations, models


def backfill_acceptance_time(apps, schema_editor):
    FreeBucketAcceptance = apps.get_model('trips', 'FreeBucketAcceptance')
    FreeBucketAcceptance.objects.filter(
        status__in=['accepted', 'cancelled', 'used', 'closed'],
        accepted_at__isnull=True,
    ).update(accepted_at=models.F('occurred_at'))


class Migration(migrations.Migration):

    dependencies = [
        ('trips', '0014_free_bucket_acceptance_active_constraint'),
        ('shifts', '0020_shiftclientaction_request_signature'),
        ('users', '0024_employee_contractor_access_from_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='freebucketacceptance',
            name='accepted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Согласован с машинистом'),
        ),
        migrations.AddField(
            model_name='freebucketacceptance',
            name='requested_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='requested_free_bucket_acceptances', to='users.employee', verbose_name='Водитель, запросивший свободный ковш'),
        ),
        migrations.AddField(
            model_name='freebucketacceptance',
            name='requesting_shift',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='requested_free_bucket_acceptances', to='shifts.employeeshift', verbose_name='Смена водителя при запросе'),
        ),
        migrations.AddField(
            model_name='freebucketacceptance',
            name='work_context_snapshot',
            field=models.JSONField(blank=True, default=dict, verbose_name='Контекст погрузки при выборе'),
        ),
        migrations.AlterField(
            model_name='freebucketacceptance',
            name='loading_shift',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='free_bucket_acceptances', to='shifts.employeeshift', verbose_name='Смена приёма'),
        ),
        migrations.AlterField(
            model_name='freebucketacceptance',
            name='operator',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='free_bucket_acceptances', to='users.employee', verbose_name='Машинист экскаватора'),
        ),
        migrations.AlterField(
            model_name='freebucketacceptance',
            name='status',
            field=models.CharField(choices=[('requested', 'Запрошен водителем'), ('accepted', 'Принят под свободный ковш'), ('cancelled', 'Отменён до погрузки'), ('used', 'Погружен'), ('closed', 'Завершён')], default='accepted', max_length=16, verbose_name='Состояние временного приёма'),
        ),
        migrations.RunPython(backfill_acceptance_time, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name='freebucketacceptance',
            name='unique_open_free_bucket_acceptance_per_truck',
        ),
        migrations.AddConstraint(
            model_name='freebucketacceptance',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['requested', 'accepted', 'used'])), fields=('truck',), name='unique_open_free_bucket_acceptance_per_truck'),
        ),
        migrations.AddConstraint(
            model_name='freebucketacceptance',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('requested_by__isnull', True), ('requesting_shift__isnull', True)), models.Q(('requested_by__isnull', False), ('requesting_shift__isnull', False)), _connector='OR'), name='free_bucket_request_fields_consistent'),
        ),
        migrations.AddConstraint(
            model_name='freebucketacceptance',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('accepted_at__isnull', True), ('loading_shift__isnull', True), ('operator__isnull', True)), models.Q(('accepted_at__isnull', False), ('loading_shift__isnull', False), ('operator__isnull', False)), _connector='OR'), name='free_bucket_accept_fields_consistent'),
        ),
        migrations.AddConstraint(
            model_name='freebucketacceptance',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('cancelled_at__isnull', True), ('closed_at__isnull', True), ('operator__isnull', True), ('requested_by__isnull', False), ('status', 'requested'), ('used_at__isnull', True), ('used_trip__isnull', True)), models.Q(('cancelled_at__isnull', True), ('closed_at__isnull', True), ('operator__isnull', False), ('status', 'accepted'), ('used_at__isnull', True), ('used_trip__isnull', True)), models.Q(('cancelled_at__isnull', False), ('closed_at__isnull', True), ('requested_by__isnull', False), ('status', 'cancelled'), ('used_at__isnull', True), ('used_trip__isnull', True)), models.Q(('cancelled_at__isnull', False), ('closed_at__isnull', True), ('operator__isnull', False), ('status', 'cancelled'), ('used_at__isnull', True), ('used_trip__isnull', True)), models.Q(('cancelled_at__isnull', True), ('closed_at__isnull', True), ('operator__isnull', False), ('status', 'used'), ('used_at__isnull', False), ('used_trip__isnull', False)), models.Q(('cancelled_at__isnull', True), ('closed_at__isnull', False), ('operator__isnull', False), ('status', 'closed'), ('used_at__isnull', False), ('used_trip__isnull', False)), _connector='OR'), name='free_bucket_status_fields_consistent'),
        ),
    ]
