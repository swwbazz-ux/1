from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from users.app_catalog import (
    APP_CATALOG_ROLE_CODES,
    render_role_app_qr_png,
    role_app_qr_asset_path,
    role_app_qr_target_url,
)


class Command(BaseCommand):
    help = 'Generate the eight immutable public PWA catalog QR assets.'

    def handle(self, *args, **options):
        static_root = Path(settings.BASE_DIR) / 'static'
        for role_code in APP_CATALOG_ROLE_CODES:
            output_path = static_root / role_app_qr_asset_path(role_code)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(
                render_role_app_qr_png(role_app_qr_target_url(role_code))
            )
            self.stdout.write(f'{role_code}: {output_path}')
