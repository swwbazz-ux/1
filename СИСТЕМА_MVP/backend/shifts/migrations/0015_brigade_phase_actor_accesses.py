from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shifts', '0014_watch_period_brigade_phases'),
    ]

    operations = [
        migrations.AddField(
            model_name='watchperiodbrigadephaseversion',
            name='created_by_access',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='created_brigade_phase_versions',
                to='users.employeeaccess',
                verbose_name='Доступ создателя',
            ),
        ),
        migrations.AddField(
            model_name='watchperiodbrigadephaseversion',
            name='confirmed_by_access',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='confirmed_brigade_phase_versions',
                to='users.employeeaccess',
                verbose_name='Доступ подтвердившего',
            ),
        ),
        migrations.AddField(
            model_name='watchperiodbrigadephaseversion',
            name='superseded_by_access',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='superseded_brigade_phase_versions',
                to='users.employeeaccess',
                verbose_name='Доступ заменившего',
            ),
        ),
        migrations.RemoveConstraint(
            model_name='watchperiodbrigadephaseversion',
            name='watch_phase_status_dates',
        ),
        migrations.AddConstraint(
            model_name='watchperiodbrigadephaseversion',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        ('confirmed_at__isnull', True),
                        ('confirmed_by_access__isnull', True),
                        ('status', 'draft'),
                        ('superseded_at__isnull', True),
                        ('superseded_by_access__isnull', True),
                    )
                    | models.Q(
                        ('confirmed_at__isnull', False),
                        ('confirmed_by_access__isnull', False),
                        ('status', 'confirmed'),
                        ('superseded_at__isnull', True),
                        ('superseded_by_access__isnull', True),
                    )
                    | models.Q(
                        ('confirmed_at__isnull', False),
                        ('confirmed_by_access__isnull', False),
                        ('status', 'superseded'),
                        ('superseded_at__isnull', False),
                        ('superseded_by_access__isnull', False),
                    )
                ),
                name='watch_phase_status_audit',
            ),
        ),
    ]
