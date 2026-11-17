import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from file_transaction import FileImage, apply_file_plan, restore_backup


class RollbackTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.targets = {"site": Path("/site"), "unit": Path("/unit")}
        self.old_site = FileImage(b"old config", 0o644, 0, 0)
        self.new_site = FileImage(b"new config", 0o644, 0, 0)
        self.unit = FileImage(b"unit", 0o644, 0, 0)
        self.manifest = {
            "site": {"before": self.old_site.metadata(), "planned": self.new_site.metadata()},
            "unit": {"before": None, "planned": self.unit.metadata()},
        }
        (self.directory / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        (self.directory / "site.before").write_bytes(self.old_site.content)

    def verify_restore(self, current):
        def write_image(path, image):
            current[path] = image

        with (
            patch("file_transaction.read_image", side_effect=current.__getitem__),
            patch("file_transaction.replace_image", side_effect=write_image),
        ):
            restore_backup(self.directory, self.targets)

    def test_partial_apply_and_repeated_restore(self):
        for active_unit in (None, self.unit):
            with self.subTest(active_unit=active_unit):
                current = {Path("/site"): self.new_site, Path("/unit"): active_unit}
                self.verify_restore(current)
                self.verify_restore(current)
                self.assertEqual(current, {Path("/site"): self.old_site, Path("/unit"): None})

    def test_unrelated_change_leaves_all_managed_targets_in_place(self):
        current = {Path("/site"): FileImage(b"other release", 0o644, 0, 0), Path("/unit"): self.unit}
        before = dict(current)
        with self.assertRaisesRegex(ValueError, "changed outside"):
            self.verify_restore(current)
        self.assertEqual(current, before)

    def test_corrupt_backup_rejected_before_any_restore(self):
        current = {Path("/site"): self.new_site, Path("/unit"): self.unit}
        before = dict(current)
        (self.directory / "site.before").write_bytes(b"bad backup")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.verify_restore(current)
        self.assertEqual(current, before)

    def test_failed_second_write_restores_first_file(self):
        current = {Path("/site"): self.old_site, Path("/unit"): None}

        def write_image(path, image):
            if path == Path("/unit"):
                raise OSError("unit write failed")
            current[path] = image

        with (
            patch("file_transaction.read_image", side_effect=current.__getitem__),
            patch("file_transaction.replace_image", side_effect=write_image),
            patch("file_transaction.save_backup") as backup,
        ):
            with self.assertRaisesRegex(OSError, "unit write failed"):
                apply_file_plan(
                    self.directory, self.targets,
                    {"site": self.old_site, "unit": None},
                    {"site": self.new_site, "unit": self.unit},
                )
        backup.assert_called_once()
        self.assertEqual(current, {Path("/site"): self.old_site, Path("/unit"): None})


@unittest.skipUnless(os.name == "posix", "requires POSIX file ownership and fsync")
class FileSystemRollbackTests(unittest.TestCase):
    def test_backup_permissions_and_restore_real_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "nginx.conf"
            unit = root / "sse.service"
            backup = root / "before"
            owner, group = os.geteuid(), os.getegid()
            config.write_bytes(b"old config")
            config.chmod(0o640)
            targets = {"site": config, "unit": unit}
            expected = {"site": FileImage(b"old config", 0o640, owner, group), "unit": None}
            planned = {
                "site": FileImage(b"updated config", 0o640, owner, group),
                "unit": FileImage(b"new unit", 0o644, owner, group),
            }
            apply_file_plan(backup, targets, expected, planned)
            self.assertEqual(config.read_bytes(), b"updated config")
            self.assertEqual(unit.read_bytes(), b"new unit")
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((backup / "site.before").stat().st_mode), 0o600)
            restore_backup(backup, targets)
            self.assertEqual(config.read_bytes(), b"old config")
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o640)
            self.assertFalse(unit.exists())


if __name__ == "__main__":
    unittest.main()
