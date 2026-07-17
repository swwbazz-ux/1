from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PortalPrivateStorage(FileSystemStorage):
    """Файлы портала без прямого публичного URL."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('location', settings.PORTAL_PRIVATE_MEDIA_ROOT)
        kwargs.setdefault('base_url', None)
        super().__init__(*args, **kwargs)

    def url(self, name):
        raise ValueError('У файлов портала нет прямого публичного URL.')


portal_private_storage = PortalPrivateStorage()
