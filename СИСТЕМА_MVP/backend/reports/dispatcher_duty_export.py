"""Выгрузка тех же данных, что и в отчёте, за весь выбранный период."""

from datetime import datetime, timedelta

from django.http import HttpResponse
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill

from core.production_time import business_localtime

from .views import style_management_export_sheet


def period_workbook_response(report):
    workbook = Workbook()
    workbook.remove(workbook.active)
    caption = (f"{report['date_from']:%d.%m.%Y} — {report['date_to']:%d.%m.%Y} · Владивосток · "
               f"Срез {business_localtime(report['now']):%d.%m.%Y %H:%M}")

    def sheet(title, headers, records):
        ws = workbook.create_sheet(title)
        ws.append([caption])
        ws.append(headers)
        for row, record in enumerate(records, 3):
            for column, value in enumerate(record, 1):
                if isinstance(value, datetime):
                    value = business_localtime(value).replace(tzinfo=None)
                elif isinstance(value, str):
                    value = ILLEGAL_CHARACTERS_RE.sub('', value)
                cell = ws.cell(row, column, value)
                if isinstance(cell.value, str):
                    cell.data_type = 's'
                elif isinstance(cell.value, timedelta):
                    cell.number_format = '[h]:mm:ss'
                elif isinstance(cell.value, datetime):
                    cell.number_format = 'dd.mm.yyyy hh:mm:ss'
                elif cell.value is not None and headers[column - 1] == 'В фоне, % времени в смене':
                    cell.value = cell.value / 100
                    cell.number_format = '0.0%'
        style_management_export_sheet(ws)
        for cell in ws[2]:
            cell.fill = PatternFill('solid', fgColor='12232E')
            cell.font = Font(color='FFFFFF', bold=True)
            note = {
                'На экране': 'Браузер сообщал о видимой вкладке пульта. Чтение экрана может проходить без команд.',
                'В фоне': 'Все наблюдаемые окна пульта были скрыты. Суммируются подтверждённые интервалы.',
                'Нет данных': 'Время без достоверных сигналов приложения в пределах личной смены.',
            }.get(cell.value)
            if note:
                cell.comment = Comment(note, 'Руководство')
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        ws.freeze_panes = 'A3'
        ws.auto_filter.ref = f'A2:{ws.cell(ws.max_row, len(headers)).coordinate}'

    def times(row):
        return [timedelta(seconds=row['seconds'][state]) for state in ('visible', 'background', 'away', 'unknown')]

    def bounds(span):
        return (span.start, span.end, timedelta(seconds=span.seconds)) if span else (None, None, None)

    columns = ['На экране', 'В фоне', 'Закрыто / выход', 'Нет данных']
    sheet('По диспетчерам', ['Диспетчер', 'Личных смен', 'Время в смене', *columns, 'В фоне, % времени в смене', 'Операций'],
          ([p['person'], p['duties'], timedelta(seconds=p['duration']), *times(p), p['background_percent'], p['count']] for p in report['people']))
    sheet('Личные смены', ['Диспетчер', 'Смена', 'Фактическое открытие', 'Фактическое закрытие', 'Статус закрытия',
          'Начало расчёта', 'Конец расчёта', *columns, 'В фоне, % времени в смене', 'Операций', 'Первая операция', 'Последняя операция',
          'Дольше всего в фоне с', 'Дольше всего в фоне по', 'Длительность фона', 'На экране без команд с', 'На экране без команд по', 'Длительность без команд'],
          ([r['person'], r['kind'], r['opened'], r['closed'], r['closure'] or ('Закрыта' if r['closed'] else 'Открыта'),
            r['span'].start, r['span'].end, *times(r), r['background_percent'], r['count'], r['first_op'], r['last_op'],
            *bounds(r['longest_background']), *bounds(r['visible_gap'])] for r in report['rows']))
    sheet('Интервалы пульта', ['Диспетчер', 'Открытие смены', 'Состояние', 'С', 'По', 'Длительность'],
          ([r['person'], r['opened'], label, span.start, span.end, timedelta(seconds=span.seconds)]
           for r in report['rows'] for span, label in r['intervals']))
    def command_rows():
        for row in report['rows']:
            for event in row['commands']:
                yield [event['person'], row['opened'], event['when'], event['title'], event['detail']]
        for event in report['unassigned']:
            yield [event['person'], 'Без привязки к смене', event['when'], event['title'], event['detail']]
    sheet('Операции', ['Исполнитель', 'Открытие смены', 'Время действия', 'Действие', 'Подробности'], command_rows())
    sheet('Пост без открытой смены', ['С', 'По', 'Длительность'],
          ([gap.start, gap.end, timedelta(seconds=gap.seconds)] for gap in report['gaps']))
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="dispatcher-{report["date_from"]}-{report["date_to"]}.xlsx"'
    workbook.save(response)
    return response
