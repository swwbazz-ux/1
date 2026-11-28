"""Очистка исторических записей журнала от кодов доступа (T-021).

До T-021 PIN попадал в `AdminActionLog.new_value`, а снимок для отмены ОУП
(`undo_payload`) хранит `access_code` и сейчас. Новые записи код не содержат;
эта команда убирает код из уже накопленных строк. Схема БД не меняется,
миграция не нужна.

Режимы:
  * без флагов — dry-run: только считает и печатает объём, ничего не пишет;
  * `--apply` — записывает маску.

Важно: после очистки `undo_payload` отмена таких старых действий ОУП перестаёт
быть доступной (снимок больше не совпадает с текущим состоянием — отказ
fail-closed, без «тихого» восстановления неверного PIN). Команда печатает
количество затронутых записей, значения PIN не выводит.
"""

from django.core.management.base import BaseCommand

from users.models import AdminActionLog
from users.redaction import REDACTED_ACCESS_CODE, redact_access_codes

ACCESS_CODE_PAYLOAD_KEY = 'access_code'


def _redact_payload(value):
    """Рекурсивно заменяет только значения ключа `access_code`."""
    changed = False
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if (
                key == ACCESS_CODE_PAYLOAD_KEY
                and isinstance(item, str)
                and item
            ):
                result[key] = REDACTED_ACCESS_CODE
                changed = True
                continue
            redacted, nested_changed = _redact_payload(item)
            result[key] = redacted
            changed = changed or nested_changed
        return result, changed
    if isinstance(value, list):
        result = []
        for item in value:
            redacted, nested_changed = _redact_payload(item)
            result.append(redacted)
            changed = changed or nested_changed
        return result, changed
    return value, False


class Command(BaseCommand):
    help = (
        'Убирает коды доступа из исторических записей AdminActionLog '
        '(new_value/old_value и undo_payload). По умолчанию dry-run.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Записать маску в БД (без флага — только показать объём).',
        )

    def handle(self, *args, **options):
        apply_changes = bool(options['apply'])
        checked = 0
        value_updates = 0
        payload_updates = 0

        for log in AdminActionLog.objects.iterator(chunk_size=500):
            checked += 1
            update_fields = []

            new_value = redact_access_codes(log.new_value)
            if new_value != log.new_value:
                log.new_value = new_value
                update_fields.append('new_value')

            old_value = redact_access_codes(log.old_value)
            if old_value != log.old_value:
                log.old_value = old_value
                update_fields.append('old_value')

            if log.undo_payload:
                redacted_payload, payload_changed = _redact_payload(log.undo_payload)
                if payload_changed:
                    log.undo_payload = redacted_payload
                    update_fields.append('undo_payload')
                    payload_updates += 1

            if update_fields:
                value_updates += 1
                if apply_changes:
                    log.save(update_fields=update_fields)

        mode = 'применено' if apply_changes else 'dry-run, изменения не записаны'
        self.stdout.write(
            f'Проверено записей: {checked}; требуют очистки: {value_updates} '
            f'(из них undo_payload: {payload_updates}); {mode}.'
        )
        if not apply_changes and value_updates:
            self.stdout.write('Повторите с --apply, чтобы обезличить журнал.')
