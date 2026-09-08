from django.db import migrations


EXACT_REPLACEMENTS = {
    'Окисленная': 'Окисленная руда',
    'Рыхлая': 'Рыхлая порода',
}

AMBIGUOUS_OBSOLETE_NAMES = ('Руда', 'Негабарит')
OBSOLETE_NAMES = (*EXACT_REPLACEMENTS, *AMBIGUOUS_OBSOLETE_NAMES)
CANONICAL_NAMES = (
    'ПСП',
    'Рыхлая порода',
    'Скальная порода',
    'Окисленная руда',
    'Переходная руда',
    'Первичная сульфидная руда',
)


def remove_obsolete_cargo_aliases(apps, schema_editor):
    RockType = apps.get_model('references', 'RockType')
    TruckCapacityRule = apps.get_model('references', 'TruckCapacityRule')
    ExcavatorPlacement = apps.get_model('assignments', 'ExcavatorPlacement')
    Trip = apps.get_model('trips', 'Trip')
    TripClientAction = apps.get_model('trips', 'TripClientAction')

    # На чистой базе справочники загружаются уже после миграций.
    if not RockType.objects.exists():
        return

    canonical_rows = {
        rock.name: rock
        for rock in RockType.objects.filter(name__in=CANONICAL_NAMES)
    }
    missing_names = sorted(set(CANONICAL_NAMES) - set(canonical_rows))
    incomplete_names = sorted(
        name
        for name, rock in canonical_rows.items()
        if rock.density is None or rock.loosening_factor is None
    )
    configured_ids = set(
        TruckCapacityRule.objects
        .filter(rock_type_id__in=[rock.id for rock in canonical_rows.values()])
        .values_list('rock_type_id', flat=True)
    )
    without_capacity = sorted(
        name for name, rock in canonical_rows.items() if rock.id not in configured_ids
    )
    if missing_names or incomplete_names or without_capacity:
        raise RuntimeError(
            'Нельзя очистить справочник пород: '
            f'отсутствуют={missing_names}, '
            f'без_характеристик={incomplete_names}, '
            f'без_кубатуры={without_capacity}.'
        )

    for obsolete_name, canonical_name in EXACT_REPLACEMENTS.items():
        obsolete = RockType.objects.filter(name=obsolete_name).first()
        if obsolete is None:
            continue
        canonical = RockType.objects.filter(name=canonical_name).first()
        if canonical is None:
            raise RuntimeError(
                f'Нельзя удалить устаревшую породу {obsolete_name!r}: '
                f'не найдена полная запись {canonical_name!r}.'
            )
        Trip.objects.filter(rock_type_id=obsolete.id).update(rock_type_id=canonical.id)
        ExcavatorPlacement.objects.filter(work_rock_type_id=obsolete.id).update(
            work_rock_type_id=canonical.id
        )

    ambiguous_ids = list(
        RockType.objects
        .filter(name__in=AMBIGUOUS_OBSOLETE_NAMES)
        .values_list('id', flat=True)
    )
    if ambiguous_ids:
        ambiguous_trip_ids = Trip.objects.filter(
            rock_type_id__in=ambiguous_ids
        ).values_list('id', flat=True)
        TripClientAction.objects.filter(trip_id__in=ambiguous_trip_ids).delete()
        Trip.objects.filter(id__in=ambiguous_trip_ids).delete()
        ExcavatorPlacement.objects.filter(
            work_rock_type_id__in=ambiguous_ids
        ).update(work_rock_type_id=None)

    obsolete_ids = list(
        RockType.objects
        .filter(name__in=OBSOLETE_NAMES)
        .values_list('id', flat=True)
    )
    if obsolete_ids:
        TruckCapacityRule.objects.filter(rock_type_id__in=obsolete_ids).delete()
        RockType.objects.filter(id__in=obsolete_ids).delete()

    if RockType.objects.filter(name__in=OBSOLETE_NAMES).exists():
        raise RuntimeError('Устаревшие породы остались в справочнике после очистки.')
    unexpected_names = sorted(
        RockType.objects
        .exclude(name__in=CANONICAL_NAMES)
        .values_list('name', flat=True)
    )
    if unexpected_names:
        raise RuntimeError(
            f'В справочнике остались неканонические породы: {unexpected_names}.'
        )


class Migration(migrations.Migration):
    dependencies = [
        ('references', '0009_link_legacy_rock_capacity_rules'),
        ('assignments', '0010_excavatordumppointsetting'),
        ('trips', '0008_trip_cancelled_at'),
    ]

    operations = [
        migrations.RunPython(
            remove_obsolete_cargo_aliases,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
