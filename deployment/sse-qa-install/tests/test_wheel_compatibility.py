from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import packaging


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctl = load("sse_qa_ctl_wheel_compatibility", PACKAGE_ROOT / "scripts/sse_qa_ctl.py")


TARGET_CP312_LINUX_X86_64 = {
    ("cp312", "cp312", "manylinux_2_34_x86_64"),
    ("cp312", "cp312", "manylinux_2_28_x86_64"),
    ("cp312", "abi3", "manylinux_2_34_x86_64"),
    ("cp311", "abi3", "manylinux_2_34_x86_64"),
    ("py3", "none", "any"),
}


def packaging_wheel_bytes() -> bytes:
    source = Path(packaging.__file__).resolve().parent
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*.py")):
            archive.write(path, Path("packaging") / path.relative_to(source))
    return target.getvalue()


PACKAGING_WHEEL_NAME = f"packaging-{packaging.__version__}-py3-none-any.whl"
PACKAGING_WHEEL_BYTES = packaging_wheel_bytes()


class WheelCompatibilityTests(unittest.TestCase):
    def make_bundle(
        self,
        wheel_names: list[str],
        *,
        include_packaging: bool = True,
        packaging_bytes: bytes = PACKAGING_WHEEL_BYTES,
    ) -> tuple[tempfile.TemporaryDirectory, Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        bundle = Path(temporary.name)
        wheelhouse = bundle / "generated/wheelhouse"
        wheelhouse.mkdir(parents=True)
        if include_packaging:
            (wheelhouse / PACKAGING_WHEEL_NAME).write_bytes(packaging_bytes)
        for name in wheel_names:
            (wheelhouse / name).write_bytes(f"synthetic:{name}".encode("ascii"))
        manifest = bundle / "generated/wheelhouse.sha256"
        manifest.write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                for path in sorted(wheelhouse.glob("*.whl"))
            ),
            encoding="utf-8",
        )
        return temporary, bundle, wheelhouse

    def validate(self, wheel_names: list[str], **kwargs) -> Path:
        temporary, bundle, wheelhouse = self.make_bundle(wheel_names, **kwargs)
        self.addCleanup(temporary.cleanup)
        return ctl.validate_wheelhouse(
            bundle,
            target_tags=TARGET_CP312_LINUX_X86_64,
            target_python=sys.executable,
        )

    def test_accepts_cp311_abi3_problem_wheel_and_supported_cp312(self):
        wheelhouse = self.validate(
            [
                "cryptography-50.0.1-cp311-abi3-manylinux_2_34_x86_64.whl",
                "example-1.0-cp312-cp312-manylinux_2_34_x86_64.whl",
            ]
        )
        self.assertTrue(wheelhouse.is_dir())

    def test_accepts_universal_and_composite_tags(self):
        self.validate(
            [
                "universal-1.0-py3-none-any.whl",
                "dual-1.0-py2.py3-none-any.whl",
                (
                    "composite-1.0-cp312-cp312-"
                    "manylinux_2_40_x86_64.manylinux_2_28_x86_64.whl"
                ),
            ]
        )

    def test_rejects_wrong_python_abi_or_platform_with_safe_reason(self):
        rejected = [
            "oldabi-1.0-cp311-cp311-manylinux_2_34_x86_64.whl",
            "future-1.0-cp313-abi3-manylinux_2_34_x86_64.whl",
            "windows-1.0-cp312-cp312-win_amd64.whl",
            "macos-1.0-cp312-cp312-macosx_14_0_x86_64.whl",
            "arm-1.0-cp312-cp312-manylinux_2_34_aarch64.whl",
            "newglibc-1.0-cp312-cp312-manylinux_2_40_x86_64.whl",
        ]
        for wheel_name in rejected:
            with self.subTest(wheel_name=wheel_name):
                with self.assertRaisesRegex(
                    ctl.QaError,
                    rf"incompatible wheel {wheel_name}: "
                    r"no supported Python/ABI/platform tag",
                ):
                    self.validate([wheel_name])

    def test_rejects_invalid_wheel_filename(self):
        wheel_name = "not-a-valid-wheel.whl"
        with self.assertRaisesRegex(
            ctl.QaError,
            rf"incompatible wheel {wheel_name}: invalid wheel filename",
        ):
            self.validate([wheel_name])

    def test_rejects_missing_validator_dependency_before_application_venv(self):
        temporary, bundle, _ = self.make_bundle(
            ["example-1.0-py3-none-any.whl"], include_packaging=False
        )
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(
            ctl.QaError,
            r"validator dependency unavailable before application venv",
        ):
            ctl.validate_wheelhouse(
                bundle,
                target_tags=TARGET_CP312_LINUX_X86_64,
                target_python=sys.executable,
            )

    def test_rejects_unimportable_validator_dependency_without_fallback(self):
        temporary, bundle, _ = self.make_bundle(
            ["example-1.0-py3-none-any.whl"], packaging_bytes=b"not-a-wheel"
        )
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(
            ctl.QaError,
            r"validator dependency unavailable before application venv",
        ):
            ctl.validate_wheelhouse(
                bundle,
                target_tags=TARGET_CP312_LINUX_X86_64,
                target_python=sys.executable,
            )

    def test_rejects_duplicate_manifest_entry(self):
        temporary, bundle, _ = self.make_bundle(["example-1.0-py3-none-any.whl"])
        self.addCleanup(temporary.cleanup)
        manifest = bundle / "generated/wheelhouse.sha256"
        first = manifest.read_text(encoding="utf-8").splitlines()[0]
        manifest.write_text(first + "\n" + first + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ctl.QaError, "invalid wheelhouse hash manifest"):
            ctl.validate_wheelhouse(
                bundle,
                target_tags=TARGET_CP312_LINUX_X86_64,
                target_python=sys.executable,
            )

    def test_rejects_malformed_manifest(self):
        temporary, bundle, _ = self.make_bundle(["example-1.0-py3-none-any.whl"])
        self.addCleanup(temporary.cleanup)
        (bundle / "generated/wheelhouse.sha256").write_text(
            "not-a-sha256-manifest\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ctl.QaError, "invalid wheelhouse hash manifest"):
            ctl.validate_wheelhouse(
                bundle,
                target_tags=TARGET_CP312_LINUX_X86_64,
                target_python=sys.executable,
            )

    def test_rejects_hash_mismatch_before_compatibility_check(self):
        temporary, bundle, wheelhouse = self.make_bundle(
            ["example-1.0-py3-none-any.whl"]
        )
        self.addCleanup(temporary.cleanup)
        (wheelhouse / "example-1.0-py3-none-any.whl").write_bytes(b"changed")
        with self.assertRaisesRegex(ctl.QaError, "wheelhouse hash mismatch"):
            ctl.validate_wheelhouse(
                bundle,
                target_tags=TARGET_CP312_LINUX_X86_64,
                target_python=sys.executable,
            )

    def test_default_uses_isolated_exact_python312_and_target_sys_tags(self):
        temporary, _, wheelhouse = self.make_bundle(
            ["example-1.0-py3-none-any.whl"]
        )
        self.addCleanup(temporary.cleanup)
        completed = subprocess.CompletedProcess(
            [], 0, json.dumps({"ok": True, "rejected": []}), ""
        )
        with (
            mock.patch.object(ctl.Path, "is_file", return_value=True),
            mock.patch.object(ctl.subprocess, "run", return_value=completed) as runner,
        ):
            ctl._validate_wheel_compatibility(sorted(wheelhouse.glob("*.whl")))

        command = runner.call_args.args[0]
        self.assertEqual(command[:4], ["/usr/bin/python3.12", "-I", "-S", "-c"])
        request = json.loads(runner.call_args.kwargs["input"])
        self.assertIsNone(request["target_tags"])
        self.assertIn("supported_tags = set(sys_tags())", ctl._WHEEL_COMPATIBILITY_CHECK)


if __name__ == "__main__":
    unittest.main()
