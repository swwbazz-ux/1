from django.db import migrations, models


def _reverse_fail_closed(apps, schema_editor):
    Version = apps.get_model('rotations', 'ArrivalRosterVersion')
    duplicate_groups = list(
        Version.objects.filter(source_kind='excel')
        .values('watch_period_id', 'source_file_id', 'parser_profile_id')
        .annotate(version_count=models.Count('pk'))
        .filter(version_count__gt=1)
        .order_by('watch_period_id', 'source_file_id', 'parser_profile_id')
    )
    if not duplicate_groups:
        return
    duplicate_filter = models.Q()
    for group in duplicate_groups:
        duplicate_filter |= models.Q(
            watch_period_id=group['watch_period_id'],
            source_file_id=group['source_file_id'],
            parser_profile_id=group['parser_profile_id'],
        )
    pks = list(
        Version.objects.filter(source_kind='excel').filter(duplicate_filter)
        .order_by('pk').values_list('pk', flat=True)[:50]
    )
    count = sum(group['version_count'] for group in duplicate_groups)
    raise RuntimeError(
        'Fail-closed reverse rotations.0006: duplicate Excel revisions exist; '
        f'count={count}; PK={pks}'
    )


class Migration(migrations.Migration):
    dependencies = [('rotations', '0005_arrival_roster_confirmation')]

    operations = [
        migrations.RemoveConstraint(
            model_name='arrivalrosterversion',
            name='uniq_arrival_period_file_profile',
        ),
        migrations.AddConstraint(
            model_name='arrivalrosterversion',
            constraint=models.UniqueConstraint(
                fields=('watch_period', 'source_file', 'parser_profile'),
                condition=models.Q(
                    source_kind='excel',
                    based_on_version__isnull=True,
                ),
                name='uniq_arrival_period_file_profile',
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, _reverse_fail_closed),
    ]
