"""Проверка data-миграций категорий кадровых должностей.

Тест идёт после всех миграций тестовой БД и утверждает фактическое состояние
каталога, а не код миграций: ``users/0027`` (классификация каталога),
``users/0028`` (кладовщик → рабочий), ``users/0029`` (остальные → ИТР).
"""

from collections import Counter

from django.test import TestCase

from .models import PersonnelPosition


# Должности, которые до users/0029 оставались без категории, а теперь ИТР.
FORMERLY_UNCLASSIFIED_ITR_NAMES = {
    'Ведущий специалист',
    'Специалист',
    'Ведущий инженер по охране окружающей среды',
    'Механик по выпуску и ремонту горно-транспортного оборудования',
    'Механик по ремонту вспомогательной техники',
    'Механик по ремонту технологических самосвалов',
    'Механик по ремонту экскаваторов',
    'Механик по техническому обслуживанию',
    'Ведущий инженер по БДД',
    'Ведущий специалист по промышленной безопасности и охране труда',
}


class PersonnelCategoryMigrationTests(TestCase):
    def test_storekeeper_is_worker_after_migration(self):
        position = PersonnelPosition.objects.get(name='Кладовщик')

        self.assertEqual(
            position.settlement_category,
            PersonnelPosition.SettlementCategory.WORKER,
        )
        self.assertEqual(position.settlement_category_label, 'Рабочий')

    def test_itr_position_is_not_overwritten(self):
        position = PersonnelPosition.objects.get(name='Горный мастер')

        self.assertEqual(
            position.settlement_category,
            PersonnelPosition.SettlementCategory.ITR,
        )
        self.assertEqual(position.settlement_category_label, 'ИТР')

    def test_formerly_unclassified_positions_are_itr(self):
        rows = dict(
            PersonnelPosition.objects
            .filter(name__in=FORMERLY_UNCLASSIFIED_ITR_NAMES)
            .values_list('name', 'settlement_category'),
        )

        self.assertEqual(set(rows), FORMERLY_UNCLASSIFIED_ITR_NAMES)
        self.assertEqual(
            set(rows.values()),
            {PersonnelPosition.SettlementCategory.ITR},
        )

    def test_no_positions_remain_unclassified(self):
        actual = list(
            PersonnelPosition.objects
            .filter(settlement_category='')
            .values_list('name', flat=True)
        )

        self.assertEqual(actual, [])

    def test_every_position_has_known_category(self):
        values = Counter(
            PersonnelPosition.objects.values_list(
                'settlement_category', flat=True,
            ),
        )

        self.assertEqual(
            set(values),
            {PersonnelPosition.SettlementCategory.WORKER,
             PersonnelPosition.SettlementCategory.ITR},
        )
        self.assertEqual(values['worker'], 16)
        self.assertEqual(values['itr'], 25)
        self.assertEqual(sum(values.values()), 41)
