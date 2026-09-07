from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('assignments', '0011_haul_assignment_open_state_constraints'),
        ('shifts', '0015_brigade_phase_actor_accesses'),
        ('trips', '0008_trip_cancelled_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='HaulAssignmentHandoff',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('open', 'Можно завершить погрузку'), ('resolved', 'Завершено рейсом'), ('expired', 'Смена завершена')], default='open', max_length=16, verbose_name='Статус')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('resolved_at', models.DateTimeField(blank=True, null=True, verbose_name='Завершено')),
                ('resolved_by_trip', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='resolved_haul_handoffs', to='trips.trip', verbose_name='Рейс, завершивший передачу')),
                ('source_assignment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='outgoing_handoffs', to='assignments.haulassignment', verbose_name='Прежнее назначение')),
                ('source_excavator', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='outgoing_haul_handoffs', to='references.equipment', verbose_name='Прежний экскаватор')),
                ('source_shift', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='haul_handoff_completions', to='shifts.employeeshift', verbose_name='Смена, завершающая погрузку')),
                ('target_assignment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='incoming_handoffs', to='assignments.haulassignment', verbose_name='Новое решение диспетчера')),
                ('truck', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='haul_assignment_handoffs', to='references.equipment', verbose_name='Самосвал')),
            ],
            options={
                'verbose_name': 'Передача погрузки между экскаваторами',
                'verbose_name_plural': 'Передачи погрузки между экскаваторами',
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='haulassignmenthandoff',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'open')), fields=('source_assignment', 'target_assignment', 'source_shift'), name='uniq_open_haul_handoff_transition'),
        ),
        migrations.AddConstraint(
            model_name='haulassignmenthandoff',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('resolved_at__isnull', True), ('resolved_by_trip__isnull', True), ('status', 'open')), models.Q(('resolved_at__isnull', False), ('resolved_by_trip__isnull', False), ('status', 'resolved')), models.Q(('resolved_at__isnull', False), ('resolved_by_trip__isnull', True), ('status', 'expired')), _connector='OR'), name='haul_handoff_resolution_consistent'),
        ),
    ]
