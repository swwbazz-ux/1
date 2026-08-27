"""Защищённая карточка: её нельзя изменить обычными путями системы.

Карточка владельца системы — единственный вход, который обязан пережить всё
остальное. Массовый импорт из отдела кадров переписывает карточки сотрудников
целиком, включая должность и статус; администратор может по ошибке снять
доступ; увольнение закрывает карточку. Если это случится с владельцем, войти в
систему и всё починить будет уже неоткуда.

Запрет стоит на уровне модели, а не в формах и не во вьюхах: путей записи в
системе много, и завтра появится ещё один. Единственная дверь — явный
`allow_protected_card_write()`, и открывает её только тот код, который знает,
что действует от имени самого владельца.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from django.core.exceptions import ValidationError


PROTECTED_WRITE_CODE = 'users.employee.card_protected'
PROTECTED_WRITE_MESSAGE = (
    'Эта карточка защищена: изменить её может только её владелец.'
)

_state = threading.local()


def protected_writes_allowed() -> bool:
    return getattr(_state, 'allowed', 0) > 0


@contextmanager
def allow_protected_card_write():
    """Открывает запись в защищённую карточку на время блока.

    Вложенность считается, потому что владелец может править свою карточку
    из кода, который сам вызывает такой же блок.
    """
    _state.allowed = getattr(_state, 'allowed', 0) + 1
    try:
        yield
    finally:
        _state.allowed -= 1


def raise_protected():
    raise ValidationError(PROTECTED_WRITE_MESSAGE, code=PROTECTED_WRITE_CODE)


def guard_employee_write(employee_pk, model):
    """Проверяем защиту по базе, а не по объекту в памяти.

    Иначе снять защиту можно было бы, просто присвоив полю False и сохранив:
    объект в памяти сказал бы, что карточка уже незащищённая.
    """
    if protected_writes_allowed() or not employee_pk:
        return
    is_protected = (
        model._base_manager
        .filter(pk=employee_pk)
        .values_list('is_protected', flat=True)
        .first()
    )
    if is_protected:
        raise_protected()


# Отметка последнего входа ничего не даёт и не отнимает — это след
# активности, а не право. Запретив её, мы заперли бы владельца снаружи: вход
# обновляет её при каждом входе в приложение.
ACCESS_BOOKKEEPING_FIELDS = frozenset({'last_login_at'})


def guard_access_write(employee_id, employee_model, update_fields=None):
    """Доступы — часть карточки: сняв доступ, владельца тоже запрут снаружи."""
    if protected_writes_allowed() or not employee_id:
        return
    if update_fields is not None and set(update_fields) <= ACCESS_BOOKKEEPING_FIELDS:
        return
    is_protected = (
        employee_model._base_manager
        .filter(pk=employee_id)
        .values_list('is_protected', flat=True)
        .first()
    )
    if is_protected:
        raise_protected()
