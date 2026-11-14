"""Скрытие кода доступа (PIN) в журналах действий (T-021).

PIN — секрет, которым входят в рабочее приложение. В журнале он не нужен:
достаточно факта «PIN выдан/изменён». Здесь одна реализация маски, чтобы
экраны журналов и Django-admin не расходились в поведении.
"""

import re

REDACTED_ACCESS_CODE = '••••••'

# «пинкод: 123456», «пин код 123456», «PIN: 123456», «первичный PIN: 123456».
_LABELLED_ACCESS_CODE = re.compile(
    r'(?i)((?:pin|пин\s?код[а-яё]*)\s*[:：]?\s*)(\d{4,8})'
)
# Старые записи перевыпуска хранили голый код в new_value.
_BARE_ACCESS_CODE = re.compile(r'^\s*\d{4,8}\s*$')


def redact_access_codes(value):
    """Возвращает строку без кода доступа; не строки — без изменений."""
    if not value or not isinstance(value, str):
        return value
    if _BARE_ACCESS_CODE.match(value):
        return REDACTED_ACCESS_CODE
    return _LABELLED_ACCESS_CODE.sub(
        lambda match: f'{match.group(1)}{REDACTED_ACCESS_CODE}',
        value,
    )


def redact_log_values(log):
    """Маскирует значения журнала на объекте в памяти (без записи в БД)."""
    log.old_value = redact_access_codes(log.old_value)
    log.new_value = redact_access_codes(log.new_value)
    return log


def looks_like_access_code(value):
    """Похожа ли строка на код доступа: 4–8 цифр и ничего кроме разделителей.

    По такому запросу нельзя искать `new_value` в журнале: иначе PIN снова
    становится оракулом поиска (T-021). Обычный текст «Результата» ищется.
    """
    text = str(value or '').strip()
    if not text:
        return False
    digits = re.sub(r'\D', '', text)
    other = re.sub(r'[\d\s\-()]', '', text)
    return bool(digits) and not other and 4 <= len(digits) <= 8
