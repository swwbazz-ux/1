"""T-036 frontend-9: бейдж защищённых и резервных мест на карте 36–38.

Проверяется только контракт шаблона `settlement/_room_card.html`: бейдж
появляется, если сервер передал тип якоря в `room.anchor_types`, и не
появляется без данных. Серверный контракт (view) этим тестом не меняется.
"""

from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase


def make_room(*, anchor_types=None, with_anchor_attribute=True):
    room = SimpleNamespace(
        id=36,
        room_type='itr',
        floor=2,
        number=36,
        transfer_status='transferred',
        occupied_bed_count=0,
        free_bed_count=2,
        search_text='кис-5 этаж 2 комната 36',
        is_transferred=True,
        view_blocks=[],
        dormitory=SimpleNamespace(number='5'),
    )
    if with_anchor_attribute:
        room.anchor_types = anchor_types
    return room


def render_room(room):
    return render_to_string(
        'settlement/_room_card.html',
        {'room': room, 'side': 'left'},
    )


class RoomCardAnchorBadgeTests(SimpleTestCase):
    def test_reserve_anchor_renders_vip_reserve_badge(self):
        html = render_room(make_room(anchor_types={'reserve'}))
        self.assertIn('ВИП-резерв', html)
        self.assertIn('data-room-anchor="reserve"', html)
        self.assertNotIn('Защищённая', html)

    def test_protected_anchor_renders_protected_badge(self):
        html = render_room(make_room(anchor_types={'protected'}))
        self.assertIn('Защищённая', html)
        self.assertIn('data-room-anchor="protected"', html)
        self.assertNotIn('ВИП-резерв', html)

    def test_both_anchors_render_both_badges(self):
        html = render_room(make_room(anchor_types={'reserve', 'protected'}))
        self.assertIn('ВИП-резерв', html)
        self.assertIn('Защищённая', html)

    def test_other_anchor_types_do_not_render_badges(self):
        for anchor_types in (
            {'group'},
            {'function'},
            {'equipment'},
            {'service'},
            {'group', 'function', 'equipment'},
        ):
            with self.subTest(anchor_types=anchor_types):
                html = render_room(make_room(anchor_types=anchor_types))
                self.assertNotIn('settlement-room-anchor', html)
                self.assertNotIn('ВИП-резерв', html)
                self.assertNotIn('Защищённая', html)

    def test_missing_anchor_types_renders_no_badge(self):
        for room in (
            make_room(anchor_types=None),
            make_room(anchor_types=set()),
            make_room(with_anchor_attribute=False),
        ):
            with self.subTest(room_anchor_types=getattr(room, 'anchor_types', 'missing')):
                html = render_room(room)
                self.assertNotIn('settlement-room-anchor', html)
                self.assertNotIn('ВИП-резерв', html)
                self.assertNotIn('Защищённая', html)

    def test_badge_does_not_change_room_identity_or_bed_markup(self):
        html = render_room(make_room(anchor_types={'reserve'}))
        self.assertIn('data-room-card', html)
        self.assertIn('data-room-number="36"', html)
        self.assertIn('data-transfer-status="transferred"', html)
        self.assertIn('class="settlement-room-anchor is-anchor-reserve"', html)
