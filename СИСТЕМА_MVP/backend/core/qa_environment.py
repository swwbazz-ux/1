from dataclasses import dataclass

from django.conf import settings
from django.core.management.base import CommandError
from django.db import connection


@dataclass(frozen=True)
class ExcavatorQAEnvironment:
    database_name: str
    label: str


def require_excavator_qa_environment() -> ExcavatorQAEnvironment:
    """Refuse every QA mutation unless this exact database was opted in."""
    if not getattr(settings, 'EXCAVATOR_QA_ENABLED', False):
        raise CommandError('EXCAVATOR_QA_ENABLED is not enabled.')

    expected_name = str(
        getattr(settings, 'EXCAVATOR_QA_DATABASE_NAME', '') or ''
    ).strip()
    actual_name = str(connection.settings_dict.get('NAME') or '').strip()
    if not expected_name:
        raise CommandError('EXCAVATOR_QA_DATABASE_NAME is required.')
    if actual_name != expected_name:
        raise CommandError(
            'QA database guard rejected the command: '
            f'actual={actual_name!r}, expected={expected_name!r}.'
        )

    production_names = {'accounting_mvp', 'accounting-mvp', 'db.sqlite3'}
    if actual_name.lower() in production_names:
        raise CommandError('QA commands cannot use a production database name.')

    return ExcavatorQAEnvironment(
        database_name=actual_name,
        label=str(getattr(settings, 'EXCAVATOR_QA_LABEL', '') or 'ТЕСТОВЫЙ СТЕНД'),
    )
