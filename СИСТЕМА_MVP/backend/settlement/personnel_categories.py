from users.models import PersonnelPosition

from .models import PhysicalRoom


MISSING_CATEGORY = 'missing_settlement_personnel_category'
ROOM_CATEGORY_CONFLICT = 'settlement_room_personnel_category_conflict'
MISSING_CATEGORY_MESSAGE = 'Уточните категорию кадровой должности: рабочий или ИТР.'
ROOM_CATEGORY_CONFLICT_MESSAGE = (
    'В одну комнату нельзя заселять рабочих и ИТР. '
    'Проверьте также категорию тех, кто уже проживает или закреплён за комнатой.'
)


def employee_settlement_category(employee):
    if employee is not None and employee.personnel_position_id:
        value = employee.personnel_position.settlement_category
        if value in (
            PersonnelPosition.SettlementCategory.WORKER,
            PersonnelPosition.SettlementCategory.ITR,
        ):
            return value
    return None


def room_allows_personnel_category(room_type, category, roommate_categories=()):
    return (
        category is not None
        and (
            room_type != PhysicalRoom.RoomType.ITR
            or category == PersonnelPosition.SettlementCategory.ITR
        )
        and all(other == category for other in roommate_categories)
    )
