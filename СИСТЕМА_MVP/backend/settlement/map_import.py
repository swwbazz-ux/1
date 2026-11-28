"""Общие помощники CSV-импорта карты M4 и пола комнат.

Модуль не содержит бизнес-правил расселения: только разбор ссылок CSV
(вахта, комната, койка), дат и чтение файла. Команды используют его, чтобы
одинаково понимать формат и одинаково сообщать об ошибках с номером строки.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError

from shifts.models import WatchPeriod

from .models import PhysicalRoom


ROOM_CODE_RE = re.compile(r'^KIS(?P<dormitory>\d+)-F(?P<floor>\d+)-R(?P<number>\d+)$')
BED_CODE_RE = re.compile(r'^(?P<block>A|B|ITR)(?P<position>\d+)$')


class CsvFormatError(ValueError):
    """Ошибка формата CSV без привязки к конкретной строке."""


@dataclass(frozen=True, slots=True)
class CsvIssue:
    line: int
    message: str

    def format(self) -> str:
        return f'строка {self.line}: {self.message}'


@dataclass(frozen=True, slots=True)
class CsvTable:
    header: tuple[str, ...]
    rows: tuple[tuple[int, dict[str, str]], ...]


def read_csv(
    path: str | Path,
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> CsvTable:
    """Прочитать CSV с обязательными и необязательными колонками.

    Заголовок обязателен; лишние колонки запрещены, чтобы опечатка в имени не
    превращалась в молчаливый пропуск. Номер строки — реальный номер в файле
    (первая строка данных = 2).
    """

    csv_path = Path(path)
    if not csv_path.is_file():
        raise CsvFormatError(f'файл CSV не найден: {csv_path}')
    try:
        text = csv_path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError as error:
        raise CsvFormatError('CSV должен быть в UTF-8') from error
    reader = csv.reader(text.splitlines())
    try:
        header = tuple(cell.strip() for cell in next(reader))
    except StopIteration as error:
        raise CsvFormatError('CSV пуст: нет строки заголовка') from error
    expected = (*required, *optional)
    missing = [name for name in required if name not in header]
    extra = [name for name in header if name not in expected]
    if missing:
        raise CsvFormatError('в заголовке нет колонок: ' + ', '.join(missing))
    if extra:
        raise CsvFormatError('неизвестные колонки: ' + ', '.join(extra))
    if len(set(header)) != len(header):
        raise CsvFormatError('в заголовке повторяются колонки')
    rows = []
    for line_number, raw in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw):
            continue
        if len(raw) > len(header):
            raise CsvFormatError(
                f'строка {line_number}: ожидалось {len(header)} колонок, '
                f'получено {len(raw)}'
            )
        if len(raw) < len(header):
            # Разрешено опускать только пустые хвостовые колонки.
            raw = raw + [''] * (len(header) - len(raw))
        rows.append((
            line_number,
            {
                name: value.strip()
                for name, value in zip(header, raw, strict=True)
            },
        ))
    if not rows:
        raise CsvFormatError('CSV не содержит строк данных')
    return CsvTable(header=header, rows=tuple(rows))


def resolve_watch_period(value: str) -> WatchPeriod:
    """Найти вахту по ID или точному названию (fail-closed при неоднозначности)."""

    raw = (value or '').strip()
    if not raw:
        raise ValidationError('Не указана вахта.')
    if raw.isdigit():
        try:
            return WatchPeriod.objects.select_related('watch_composition').get(pk=int(raw))
        except WatchPeriod.DoesNotExist as error:
            raise ValidationError(f'Вахта с ID {raw} не найдена.') from error
    periods = list(
        WatchPeriod.objects.select_related('watch_composition').filter(name=raw).order_by('pk'),
    )
    if not periods:
        raise ValidationError(f'Вахта «{raw}» не найдена.')
    if len(periods) > 1:
        raise ValidationError(
            f'Название вахты «{raw}» неоднозначно; укажите ID.',
        )
    return periods[0]


def resolve_room(value: str) -> PhysicalRoom:
    """Найти комнату по коду ``KIS5-F1-R01`` или ``5/1/1``."""

    raw = (value or '').strip()
    if not raw:
        raise ValidationError('Не указана комната.')
    match = ROOM_CODE_RE.fullmatch(raw.upper())
    if match:
        dormitory = match.group('dormitory')
        floor = int(match.group('floor'))
        number = int(match.group('number'))
    else:
        parts = [part for part in raw.replace('КИС-', '').split('/') if part]
        if len(parts) != 3:
            raise ValidationError(
                f'Комната «{raw}»: ожидается KIS5-F1-R01 или 5/1/1.',
            )
        try:
            dormitory = parts[0]
            floor = int(parts[1])
            number = int(parts[2])
        except ValueError as error:
            raise ValidationError(
                f'Комната «{raw}»: корпус, этаж и номер должны быть числами.',
            ) from error
    rooms = list(
        PhysicalRoom.objects.select_related('dormitory').filter(
            dormitory__number=dormitory,
            floor=floor,
            number=number,
        ),
    )
    if not rooms:
        raise ValidationError(f'Комната «{raw}» не найдена.')
    if len(rooms) > 1:
        raise ValidationError(f'Комната «{raw}» неоднозначна.')
    return rooms[0]


def parse_iso_date(value: str, *, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValidationError(
            f'{field}: ожидается дата YYYY-MM-DD, получено «{value}».',
        ) from error


def write_report(path: str | None, payload: dict) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf-8',
    )


__all__ = [
    'BED_CODE_RE',
    'CsvFormatError',
    'CsvIssue',
    'CsvTable',
    'parse_iso_date',
    'read_csv',
    'resolve_room',
    'resolve_watch_period',
    'write_report',
]
