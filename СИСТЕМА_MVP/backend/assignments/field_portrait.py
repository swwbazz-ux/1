"""Bounded personal thumbnail for an authenticated field tablet read."""

import base64
import io
import logging

from django.http import JsonResponse
from django.utils.crypto import salted_hmac
from PIL import Image, ImageOps

from users.forms import MAX_EMPLOYEE_PHOTO_PIXELS, MAX_EMPLOYEE_PHOTO_UPLOAD_SIZE

from .field_signing import PROTOCOL


def revision(employee):
    if not employee.photo:
        return ""
    # Old files can be replaced with the same name. Normal personnel edits update
    # updated_at, so snapshots can detect the replacement without touching storage.
    value = f"{employee.pk}|{employee.updated_at.isoformat()}|{employee.photo.name}"
    return salted_hmac("mining-master.portrait.v1", value, algorithm="sha256").hexdigest()


def _thumbnail(photo):
    with photo.storage.open(photo.name, "rb") as source:
        raw = source.read(MAX_EMPLOYEE_PHOTO_UPLOAD_SIZE + 1)
    if len(raw) > MAX_EMPLOYEE_PHOTO_UPLOAD_SIZE:
        raise ValueError("source size")
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > MAX_EMPLOYEE_PHOTO_PIXELS:
            raise ValueError("source dimensions")
        image.draft("RGB", (160, 160))
        oriented = ImageOps.exif_transpose(image)
        oriented.thumbnail((160, 160), Image.Resampling.LANCZOS)
        with io.BytesIO() as output:
            oriented.convert("RGB").save(output, format="JPEG", quality=82)
            if output.tell() > 64 * 1024:
                raise ValueError("thumbnail size")
            return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def response(access):
    employee = access.employee
    version = revision(employee)
    data = None
    if version:
        try:
            data = _thumbnail(employee.photo)
        except Exception as error:
            # Storage backends and decoders have different exception classes.
            # An existing field must never become a deletion because a read failed.
            logging.getLogger(__name__).warning(
                "Field portrait unavailable (%s)", type(error).__name__
            )
            return JsonResponse({"status": "retry", "code": "portrait_unavailable"}, status=503)
    return JsonResponse(
        {
            "protocol": PROTOCOL,
            "portrait": {
                "employee_id": employee.pk,
                "access_id": access.pk,
                "revision": version,
                "data": data,
            },
        }
    )
