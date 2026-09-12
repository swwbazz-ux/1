"""Автозакрытие смен: техника отрубается в 08:00 и 20:00, диспетчеры и горные
мастера — в 08:30 и 20:30 (полчаса на сдачу дел после своей смены).

Запускается таймером systemd раз в несколько минут; та же функция дёргается
при загрузке пульта диспетчера, чтобы хвосты закрывались и без таймера.
"""
from django.core.management.base import BaseCommand

from trips.views import auto_close_expired_equipment_shifts


class Command(BaseCommand):
    help = 'Закрыть смены, которые сотрудник не закрыл к своей отсечке (08:00 / 20:00).'

    def handle(self, *args, **options):
        closed = auto_close_expired_equipment_shifts()
        for shift in closed:
            self.stdout.write(
                f'закрыта смена #{shift.id}: {shift.employee} / {shift.equipment} / {shift.get_shift_type_display()}'
            )
        self.stdout.write(f'Закрыто автоматически: {len(closed)}')
