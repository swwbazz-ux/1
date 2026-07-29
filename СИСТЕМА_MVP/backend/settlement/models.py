from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class PhysicalRoom(models.Model):
    class RoomType(models.TextChoices):
        STANDARD = 'standard', 'Стандартная'
        ITR = 'itr', 'ИТР'

    class TransferStatus(models.TextChoices):
        TRANSFERRED = 'transferred', 'Передана'
        NOT_TRANSFERRED = 'not_transferred', 'Не передана'

    class CorridorSide(models.TextChoices):
        LEFT = 'left', 'Левая сторона'
        RIGHT = 'right', 'Правая сторона'

    dormitory = models.ForeignKey(
        'references.Dormitory',
        verbose_name='Общежитие',
        on_delete=models.PROTECT,
        related_name='physical_rooms',
    )
    floor = models.PositiveSmallIntegerField(
        'Этаж',
        validators=[MinValueValidator(1), MaxValueValidator(2)],
    )
    number = models.PositiveSmallIntegerField('Номер комнаты')
    room_type = models.CharField(
        'Тип комнаты',
        max_length=16,
        choices=RoomType.choices,
        default=RoomType.STANDARD,
    )
    transfer_status = models.CharField(
        'Статус передачи',
        max_length=24,
        choices=TransferStatus.choices,
        default=TransferStatus.NOT_TRANSFERRED,
        db_index=True,
    )
    capacity = models.PositiveSmallIntegerField(
        'Вместимость',
        validators=[MinValueValidator(1), MaxValueValidator(6)],
    )
    corridor_side = models.CharField(
        'Сторона коридора',
        max_length=8,
        choices=CorridorSide.choices,
    )
    side_position = models.PositiveSmallIntegerField(
        'Позиция на стороне этажа',
        validators=[MinValueValidator(1)],
        help_text=(
            'Порядок комнаты только внутри стороны выбранного этажа. '
            'Не задаёт соответствие номеров между этажами.'
        ),
    )

    class Meta:
        verbose_name = 'Физическая комната'
        verbose_name_plural = 'Физические комнаты'
        ordering = [
            'dormitory__number',
            'floor',
            'corridor_side',
            'side_position',
            'number',
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['dormitory', 'floor', 'number'],
                name='unique_physical_room_number',
            ),
            models.UniqueConstraint(
                fields=['dormitory', 'floor', 'corridor_side', 'side_position'],
                name='unique_physical_room_side_position',
            ),
            models.CheckConstraint(
                condition=models.Q(floor__gte=1, floor__lte=2),
                name='physical_room_floor_1_2',
            ),
            models.CheckConstraint(
                condition=models.Q(capacity__gte=1, capacity__lte=6),
                name='physical_room_capacity_1_6',
            ),
        ]

    @property
    def is_transferred(self):
        return self.transfer_status == self.TransferStatus.TRANSFERRED

    def __str__(self):
        return f'КИС-{self.dormitory.number}, этаж {self.floor}, комната {self.number}'


class PhysicalBed(models.Model):
    class Block(models.TextChoices):
        A = 'A', 'А'
        B = 'B', 'Б'
        ITR = 'ITR', 'ИТР'

    room = models.ForeignKey(
        PhysicalRoom,
        verbose_name='Комната',
        on_delete=models.CASCADE,
        related_name='beds',
    )
    stable_id = models.CharField(
        'Стабильный идентификатор',
        max_length=64,
        unique=True,
        editable=False,
    )
    block = models.CharField('Блок', max_length=3, choices=Block.choices)
    position = models.PositiveSmallIntegerField(
        'Номер койко-места в блоке',
        validators=[MinValueValidator(1), MaxValueValidator(3)],
    )

    class Meta:
        verbose_name = 'Физическое койко-место'
        verbose_name_plural = 'Физические койко-места'
        ordering = ['room', 'block', 'position']
        constraints = [
            models.UniqueConstraint(
                fields=['room', 'block', 'position'],
                name='unique_physical_bed_in_room',
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1, position__lte=3),
                name='physical_bed_position_1_3',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(block__in=['A', 'B'])
                    | models.Q(block='ITR', position__lte=2)
                ),
                name='physical_bed_block_position_valid',
            ),
        ]

    @property
    def is_available(self):
        return self.room.is_transferred

    def save(self, *args, **kwargs):
        if self.pk:
            original_stable_id = (
                type(self).objects
                .filter(pk=self.pk)
                .values_list('stable_id', flat=True)
                .first()
            )
            if original_stable_id and original_stable_id != self.stable_id:
                raise ValidationError({
                    'stable_id': 'Стабильный идентификатор койко-места менять нельзя.',
                })
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.room}, {self.get_block_display()}-{self.position}'
