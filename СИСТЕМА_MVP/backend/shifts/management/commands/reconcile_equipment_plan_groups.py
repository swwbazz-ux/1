from django.core.management.base import BaseCommand

from shifts.equipment_plan_groups import (
    find_open_shift_plan_group_mismatches,
    reconcile_default_equipment_plan_groups,
)


class Command(BaseCommand):
    help = 'Safely reconciles default equipment plan group membership.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only print the report, do not save changes.')

    def handle(self, *args, **options):
        report = reconcile_default_equipment_plan_groups(dry_run=options['dry_run'])
        self.stdout.write(f'Найдено техники: {report["equipment_total"]}')
        self.stdout.write(f'Добавлено связей: {report["added_total"]}')
        self.stdout.write(f'Удалено неправильных связей: {report["removed_total"]}')
        for group_code, group_report in report['groups'].items():
            self.stdout.write(
                f'{group_report["name"]}: целевой состав {group_report["target_count"]}, '
                f'было {group_report["before_count"]}, добавлено {group_report["added_count"]}, '
                f'удалено {group_report["removed_count"]}'
            )
            if group_report['removed']:
                self.stdout.write(f'  удалены: {", ".join(group_report["removed"])}')
            if group_report['added']:
                self.stdout.write(f'  добавлены: {", ".join(group_report["added"])}')
        if report['unassigned']:
            self.stdout.write(f'Без стандартной группы: {", ".join(report["unassigned"])}')
        else:
            self.stdout.write('Без стандартной группы: нет')

        mismatches = find_open_shift_plan_group_mismatches()
        if mismatches:
            self.stdout.write('Открытые смены с несовпадающим snapshot:')
            for item in mismatches:
                self.stdout.write(
                    f'  shift #{item["shift_id"]}: {item["equipment"]}; '
                    f'сейчас {item["actual_group"]}, ожидается {item["expected_group"]}; '
                    f'статус {item["plan_status"]}'
                )
        else:
            self.stdout.write('Открытые смены с несовпадающим snapshot: нет')
