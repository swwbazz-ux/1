from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = "origin/codex/github-production-deploy-20260916"
MANIFEST_PATH = Path(".github/deploy/production-files.txt")
SHELL_PATTERN = re.compile(r"dispatcher-desktop-shell-v\d+")
TEMPLATE_ASSET_PATTERN = re.compile(
    r"\{%\s*static\s+'((?:css|js)/dispatcher-[^']+\.(?:css|js))'\s*%\}"
)
SERVICE_WORKER_ASSET_PATTERN = re.compile(r'"(/static/[^"?]+)(?:\?[^" ]+)?"')


class AuditError(RuntimeError):
    pass


def read_manifest(root: Path = ROOT) -> list[str]:
    return [
        line.strip()
        for line in (root / MANIFEST_PATH).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def is_non_production_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    name = PurePosixPath(normalized).name
    return bool(
        normalized == MANIFEST_PATH.as_posix()
        or normalized.startswith(".github/")
        or normalized.startswith("ПРОГРЕСС_ПРОЕКТА/")
        or normalized.startswith("docs/")
        or "tests" in parts
        or "tools" in parts
        or re.fullmatch(r"tests?\.py", name)
        or re.fullmatch(r"test_.*\.py", name)
        or normalized.endswith(".md")
    )


def run_git(root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise AuditError(f"git {' '.join(args)} failed: {message}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def changed_paths(root: Path, base: str) -> tuple[list[str], list[str]]:
    changed = run_git(
        root,
        "diff",
        "--name-only",
        "--diff-filter=ACMRD",
        base,
        "--",
    )
    deleted = run_git(
        root,
        "diff",
        "--name-only",
        "--diff-filter=D",
        base,
        "--",
    )
    untracked = run_git(root, "ls-files", "--others", "--exclude-standard")
    return sorted(set(changed + untracked)), sorted(set(deleted))


def validate_manifest(root: Path, manifest: list[str]) -> None:
    duplicates = sorted({path for path in manifest if manifest.count(path) > 1})
    missing = sorted(path for path in manifest if not (root / path).is_file())
    errors = []
    if duplicates:
        errors.append("manifest duplicates:\n" + "\n".join(duplicates))
    if missing:
        errors.append("manifest paths missing from repository:\n" + "\n".join(missing))
    if errors:
        raise AuditError("\n\n".join(errors))


def validate_changed_scope(
    root: Path,
    manifest: list[str],
    *,
    base: str,
) -> dict[str, int]:
    changed, deleted = changed_paths(root, base)
    production = [path for path in changed if not is_non_production_path(path)]
    deleted_production = [
        path for path in deleted if not is_non_production_path(path)
    ]
    missing = sorted(path for path in production if manifest.count(path) != 1)
    errors = []
    if missing:
        errors.append(
            "changed production files not packaged exactly once:\n"
            + "\n".join(missing)
        )
    if deleted_production:
        errors.append(
            "deleted production files require an explicit server removal plan:\n"
            + "\n".join(deleted_production)
        )
    if errors:
        raise AuditError("\n\n".join(errors))
    return {
        "changed": len(changed),
        "production_changed": len(production),
        "deleted_production": len(deleted_production),
    }


def validate_dispatcher_shell(root: Path, manifest: list[str]) -> dict[str, object]:
    backend = root / "СИСТЕМА_MVP" / "backend"
    source_paths = (
        backend / "trips" / "dispatcher_pwa.py",
        backend / "users" / "role_apps.py",
        backend / "templates" / "trips" / "dispatcher_control.html",
        backend / "templates" / "reports" / "dispatcher_shift_report.html",
    )
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in source_paths
    }
    versions = sorted({
        version
        for source in sources.values()
        for version in SHELL_PATTERN.findall(source)
    })
    if len(versions) != 1:
        raise AuditError(
            "dispatcher shell version mismatch: "
            + (", ".join(versions) if versions else "no version found")
        )

    template_source = sources[
        backend / "templates" / "trips" / "dispatcher_control.html"
    ]
    service_worker_source = sources[backend / "trips" / "dispatcher_pwa.py"]
    template_assets = set(TEMPLATE_ASSET_PATTERN.findall(template_source))
    service_worker_assets = set(
        SERVICE_WORKER_ASSET_PATTERN.findall(service_worker_source)
    )
    uncached = sorted(
        asset
        for asset in template_assets
        if f"/static/{asset}" not in service_worker_assets
    )
    missing_sources = sorted(
        asset
        for asset in service_worker_assets
        if not (backend / "static" / asset.removeprefix("/static/")).is_file()
    )
    unpackaged_runtime = sorted(
        f"СИСТЕМА_MVP/backend/static/{asset}"
        for asset in template_assets
        if manifest.count(f"СИСТЕМА_MVP/backend/static/{asset}") != 1
    )
    errors = []
    if uncached:
        errors.append(
            "dispatcher template assets missing from CORE_ASSETS:\n"
            + "\n".join(uncached)
        )
    if missing_sources:
        errors.append(
            "dispatcher CORE_ASSETS missing source files:\n"
            + "\n".join(missing_sources)
        )
    if unpackaged_runtime:
        errors.append(
            "dispatcher runtime assets not packaged exactly once:\n"
            + "\n".join(unpackaged_runtime)
        )
    if errors:
        raise AuditError("\n\n".join(errors))
    return {
        "shell_version": versions[0],
        "template_runtime_assets": len(template_assets),
        "service_worker_static_assets": len(service_worker_assets),
    }


def audit_repository(root: Path, *, base: str) -> dict[str, object]:
    manifest = read_manifest(root)
    validate_manifest(root, manifest)
    scope = validate_changed_scope(root, manifest, base=base)
    shell = validate_dispatcher_shell(root, manifest)
    return {
        "base": base,
        "manifest_entries": len(manifest),
        **scope,
        **shell,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit dispatcher release scope before PR, VERIFY or DEPLOY."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--base", default=DEFAULT_BASE)
    args = parser.parse_args()
    try:
        result = audit_repository(args.root.resolve(), base=args.base)
    except AuditError as error:
        print("RELEASE_SCOPE_ERROR")
        print(error)
        return 1
    print("RELEASE_SCOPE_OK")
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
