"""Equipment categories for the deputy's crew board, independent of app access."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CrewCategory:
    label: str
    equipment_type: str
    specialization: str
    numbers: tuple[str, ...]


AUXILIARY_CATEGORIES = {
    'crew_bus': CrewCategory('Вахтовые автобусы', 'Вахтовый автобус', 'watch_bus_driver', ('149', '103', '007')),
    'crew_kdm': CrewCategory('КДМ', 'КДМ', 'cargo_kdm_driver', ('055', '003', '207', '061')),
    'crew_prm': CrewCategory('ПРМ', 'ПРМ', 'cargo_prm_driver', ('035',)),
    'crew_planner': CrewCategory('Планировщики', 'Планировщик', 'excavator_operator', ('8803', '8804', '8805', '8806')),
    'crew_dozer': CrewCategory('Бульдозеры', 'Бульдозер', 'bulldozer_operator', ('1', '2', '3', '4')),
    'crew_wheel_dozer': CrewCategory('Колёсники', 'Колёсный бульдозер', 'bulldozer_operator', ('1', '2')),
    'crew_grader': CrewCategory('Грейдеры', 'Автогрейдер', 'grader_operator', ('1', '2')),
    'crew_roller': CrewCategory('Катки', 'Каток', 'roller_operator', ('',)),
}

CATEGORY_LABELS = {
    'driver': 'Самосвалы',
    'excavator_operator': 'Экскаваторы',
    **{code: category.label for code, category in AUXILIARY_CATEGORIES.items()},
}


def crew_equipment_number(equipment):
    # Garage numbers are globally unique in the directory. For equipment sharing
    # a number, the directory stores the type prefix; the board shows the sheet number.
    number = equipment.garage_number
    prefix = equipment.equipment_type.name + ' № '
    return number.removeprefix(prefix)
