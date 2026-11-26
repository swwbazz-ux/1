from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from core.production_time import business_localtime

from .bed_availability import active_prepared_bed_ids, active_reserved_bed_ids
from .intervals import occupancy_interval
from .models import PhysicalRoom


COLUMNS = (
    ('Этаж', 8), ('Комната', 11), ('Койка', 13), ('Статус', 23),
    ('Жилец', 37), ('Смена', 15), ('Техника / должность / организация', 43),
    ('Заселение', 22), ('Окончание проживания', 24), ('Закрепление', 28),
)


def build_current_housing_export(rooms, moment):
    workbook = Workbook()
    workbook.remove(workbook.active)
    beds_by_building = {'5': [], '6': []}
    for room in sorted(rooms, key=lambda room: (room.floor, room.number)):
        beds_by_building[room.dormitory.number].extend((room, bed) for bed in room.beds.all())
    bed_ids = [bed.pk for beds in beds_by_building.values() for _, bed in beds]
    prepared = active_prepared_bed_ids(bed_ids=bed_ids, moment=moment)
    reserved = active_reserved_bed_ids(bed_ids=bed_ids)
    for building, beds in beds_by_building.items():
        sheet = workbook.create_sheet(f'КИС-{building}')
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(COLUMNS))
        sheet['A1'] = f'КИС-{building} · Текущее расселение на {business_localtime(moment):%d.%m.%Y %H:%M} (Владивосток)'
        sheet['A1'].font = Font(bold=True, size=13)
        sheet.row_dimensions[1].height = 24
        sheet.append([label for label, _ in COLUMNS])
        for index, (_, width) in enumerate(COLUMNS, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
            sheet.cell(2, index).font = Font(bold=True, color='FFFFFF')
            sheet.cell(2, index).fill = PatternFill('solid', fgColor='244A5A')
        sheet.row_dimensions[2].height = 24

        for room, bed in beds:
            occupancy = bed.active_occupancy
            if occupancy:
                status = 'На продлении' if bed.on_extension else 'Занято'
            elif room.transfer_status != PhysicalRoom.TransferStatus.TRANSFERRED:
                status = 'Недоступно'
            elif bed.pk in prepared:
                status = 'Подготовлено'
            elif bed.pk in reserved:
                status = 'В резерве'
            else:
                status = 'Свободно'
            row = [room.floor, room.number, f'{bed.get_block_display()}-{bed.position}', status]
            if occupancy:
                start, end = occupancy_interval(occupancy)
                row.extend((
                    bed.occupant_name, bed.shift_label, bed.work_label,
                    business_localtime(start).replace(tzinfo=None),
                    business_localtime(end).replace(tzinfo=None) if end else None,
                    bed.assignment_type_label,
                ))
            sheet.append(row)
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
            if occupancy:
                for col in (8, 9):
                    sheet.cell(sheet.max_row, col).number_format = 'dd.mm.yyyy hh:mm'

        sheet.freeze_panes = 'D3'
        sheet.auto_filter.ref = f'A2:{get_column_letter(len(COLUMNS))}{sheet.max_row}'

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return output
