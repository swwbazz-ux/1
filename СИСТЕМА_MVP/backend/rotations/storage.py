from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class ArrivalRosterPrivateStorage(FileSystemStorage):
    """Закрытое хранилище исходных реестров без прямого HTTP URL."""

    def __init__(self, *args, **kwargs):
        self._arrival_roster_location = kwargs.pop('location', None)
        kwargs.setdefault('base_url', None)
        super().__init__(*args, **kwargs)

    @property
    def base_location(self):
        return self._arrival_roster_location or settings.ROTATIONS_PRIVATE_MEDIA_ROOT

    @property
    def location(self):
        return str(self.base_location.resolve())

    def url(self, name):
        raise ValueError('У исходных реестров заезда нет прямого публичного URL.')


arrival_roster_private_storage = ArrivalRosterPrivateStorage()
