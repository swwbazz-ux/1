from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.checks import Error, register


PHOTO_UPLOAD_SUBDIR = 'employee_photos'


def _directory_write_error(path):
    path = Path(path)
    if not path.exists():
        return f'directory does not exist: {path}'
    if not path.is_dir():
        return f'path is not a directory: {path}'

    probe = path / f'.write-check-{uuid4().hex}'
    try:
        probe.write_text('ok', encoding='utf-8')
    except OSError as exc:
        return f'directory is not writable by the Django process: {path}: {exc}'
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return None


@register()
def media_storage_writable_check(app_configs, **kwargs):
    errors = []
    media_root = Path(settings.MEDIA_ROOT)

    media_root_error = _directory_write_error(media_root)
    if media_root_error:
        errors.append(
            Error(
                media_root_error,
                hint='Fix MEDIA_ROOT owner and permissions, for example deploy:www-data with owner/group write access.',
                id='core.E001',
            )
        )
        return errors

    employee_photos_dir = media_root / PHOTO_UPLOAD_SUBDIR
    if employee_photos_dir.exists():
        employee_photos_error = _directory_write_error(employee_photos_dir)
        if employee_photos_error:
            errors.append(
                Error(
                    employee_photos_error,
                    hint='Fix media/employee_photos permissions; employee photo upload must be writable by the Django process user.',
                    id='core.E002',
                )
            )

    return errors
