"""Private storage for one current housing draft per authorized access."""

from base64 import b64decode, b64encode
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from django.conf import settings
from django.core.files import locks


@dataclass(frozen=True)
class CurrentHousingDraft:
    filename: str
    workbook: bytes
    digest: str
    as_of: date
    revision: str
    review: dict

    @classmethod
    def from_workbook(cls, filename, workbook, as_of):
        return cls(filename, workbook, sha256(workbook).hexdigest(), as_of, uuid4().hex, {})

    def with_review(self, review):
        return replace(self, review=review, revision=uuid4().hex)


class CurrentHousingDraftStorage:
    def __init__(self, access_id):
        directory = Path(settings.PORTAL_PRIVATE_MEDIA_ROOT) / 'settlement' / 'current_housing'
        self.path = directory / f'{int(access_id)}.json'

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with self.path.with_suffix('.lock').open('a+b') as lock_file:
            if not locks.lock(lock_file, locks.LOCK_EX):
                raise OSError('Could not lock current housing draft')
            try:
                yield
            finally:
                locks.unlock(lock_file)

    def load(self):
        try:
            with self.path.open(encoding='utf-8') as source:
                data = json.load(source)
            data['workbook'] = b64decode(data['workbook'], validate=True)
            data['as_of'] = date.fromisoformat(data['as_of'])
            return CurrentHousingDraft(**data)
        except FileNotFoundError:
            return None
        except (KeyError, TypeError, ValueError) as error:
            raise OSError('Could not read current housing draft') from error

    def save(self, draft):
        data = {
            'filename': draft.filename, 'workbook': b64encode(draft.workbook).decode('ascii'),
            'digest': draft.digest, 'as_of': draft.as_of.isoformat(), 'revision': draft.revision,
            'review': draft.review,
        }
        temporary = None
        try:
            with NamedTemporaryFile(dir=self.path.parent, mode='w', encoding='utf-8', delete=False) as target:
                temporary = Path(target.name)
                json.dump(data, target, ensure_ascii=False, separators=(',', ':'))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
