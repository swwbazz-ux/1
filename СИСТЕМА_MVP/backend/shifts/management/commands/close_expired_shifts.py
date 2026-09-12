"""Автозакрытие смен техники: полчаса после конца смены (19:30 и 07:30).

Запускается таймером systemd раз в несколько минут; та же функция дёргается
при загрузке пульта диспетчера, чтобы хвосты закрывались и без таймера.
"""
from django.core.management.base import BaseCommand

from trips.views import auto_close_expired_equipment_shifts


class Command(BaseCommand):
    help = 'Закрыть смены техники, которые сотрудник не закрыл к 19:30 / 07:30.'

    def handle(self, *args, **options):
        closed = auto_close_expired_equipment_shifts()
        for shift in closed:
            self.stdout.write(
                f'закрыта смена #{shift.id}: {shift.employee} / {shift.equipment} / {shift.get_shift_type_display()}'
            )
        self.stdout.write(f'Закрыто автоматически: {len(closed)}')
