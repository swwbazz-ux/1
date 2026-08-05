from decimal import Decimal

from .driver_watch_rating import DRIVER_RATING_LEVELS


RATING_TV_REFRESH_SECONDS = 300
RATING_TV_ROTATION_SECONDS = 15
RATING_TV_QA_DAY_COUNT = 30
RATING_TV_QA_ENTRY_COUNT = 53


def build_rating_tv_qa_preview():
    """Возвращает только локальный визуальный макет TV-экрана.

    Этот payload не является результатом KPI и вызывается только отдельным
    QA-view при одновременных ``DEBUG=True`` и включённом QA-флаге.
    """

    truck_labels = (
        'БелАЗ',
        'БелАЗ',
        'NHL',
    )
    test_names = (
        'Сергей',
        'Алексей',
        'Дмитрий',
        'Игорь',
        'Евгений',
        'Андрей',
        'Олег',
        'Михаил',
    )
    entries = []
    for index in range(1, RATING_TV_QA_ENTRY_COUNT + 1):
        score = (
            Decimal('98.50')
            - Decimal(index - 1) * Decimal('0.71')
        ).quantize(Decimal('0.01'))
        entries.append({
            'employee_id': -index,
            'full_name': (
                f'Тестов {test_names[(index - 1) % len(test_names)]} '
                f'{index:02d}'
            ),
            'equipment': [
                f'{truck_labels[(index - 1) % len(truck_labels)]} №{index:02d}'
            ],
            'shift_count': 12,
            'score': str(score),
            'place': index,
            'shared_score_place': index,
            'display_order': index,
            'level': DRIVER_RATING_LEVELS.get(index, ''),
            'position_delta': (2, 1, -1, 3, -2, 0)[(index - 1) % 6],
        })

    return {
        'available': True,
        'official': False,
        'official_rating_eligible': False,
        'rating_mode': 'qa_visual_preview',
        'scope_type': 'rating_period',
        'formula_version': 'TV_QA_VISUAL_PREVIEW',
        'formula_label': 'Визуальный макет, не расчёт KPI',
        'status': (
            'Тестовый визуальный снимок. Баллы не являются результатом KPI.'
        ),
        'generated_at': '2026-05-25T22:00:00+04:00',
        'rating_period': {
            'id': -1,
            'name': '14.05.2026 — 14.06.2026',
            'starts_on': '2026-05-14',
            'ends_before': '2026-06-14',
            'is_active': True,
        },
        'watch_composition': {
            'id': -1,
            'code': 'qa-tv-composition',
            'name': 'Тестовый состав вахты',
            'is_active': True,
        },
        'shift_type': 'night',
        'shift_type_label': 'Ночная',
        'available_rating_periods': [
            {
                'id': -1,
                'name': '14.05.2026 — 14.06.2026',
                'starts_on': '2026-05-14',
                'ends_before': '2026-06-14',
                'is_active': True,
            }
        ],
        'available_watch_compositions': [
            {
                'id': -1,
                'code': 'qa-tv-composition',
                'name': 'Тестовый состав вахты',
                'is_active': True,
            }
        ],
        'summary': {
            'employee_count': len(entries),
            'rated_shift_count': len(entries) * 12,
            'withheld_shift_count': 0,
            'withheld_reasons': {},
        },
        'entries': entries,
        'qa_day': 12,
        'qa_day_count': RATING_TV_QA_DAY_COUNT,
    }
