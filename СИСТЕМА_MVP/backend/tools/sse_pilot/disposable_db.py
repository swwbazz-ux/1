from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


ROOT_PREFIX = "sse-pilot-disposable-"
ROOT_MARKER_NAME = ".sse-pilot-disposable-root"
ROOT_MARKER_VALUE = "copper-resources-sse-pilot-disposable-v1\n"
DATABASE_SUFFIX = ".sqlite3"
DATABASE_MARKER_TABLE = "sse_pilot_disposable_marker"
DATABASE_MARKER_VALUE = "copper-resources-sse-pilot-db-v1"


class UnsafeDisposableDatabase(ValueError):
    pass


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _lexical_absolute(value: str | Path) -> Path:
    # abspath normalizes ``.``/``..`` without following a symlink or Windows
    # reparse point. Path.resolve() is deliberately deferred until after the
    # original path has passed the reparse checks.
    return Path(os.path.abspath(os.fspath(value)))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    if getattr(metadata, 'st_file_attributes', 0) & reparse_flag:
        return True
    is_junction = getattr(path, 'is_junction', None)
    return bool(is_junction and is_junction())


def _reject_reparse_components(path: Path, *, below: Path, label: str) -> None:
    try:
        relative = path.relative_to(below)
    except ValueError as error:
        raise UnsafeDisposableDatabase(
            f'{label} must be lexically below the OS temp directory'
        ) from error
    current = below
    for component in relative.parts:
        current /= component
        if _is_link_or_reparse(current):
            raise UnsafeDisposableDatabase(
                f'{label} must not contain a symlink or Windows reparse point'
            )


def validate_disposable_database(
    database: str | Path,
    disposable_root: str | Path,
    *,
    require_database_missing: bool,
) -> tuple[Path, Path]:
    """Fail closed before Django is imported or any database write occurs."""
    temp_root_lexical = _lexical_absolute(tempfile.gettempdir())
    root_lexical = _lexical_absolute(disposable_root)
    target_lexical = _lexical_absolute(database)

    if not _inside(root_lexical, temp_root_lexical) or root_lexical == temp_root_lexical:
        raise UnsafeDisposableDatabase("disposable root must be below the OS temp directory")
    _reject_reparse_components(
        root_lexical,
        below=temp_root_lexical,
        label='disposable root',
    )
    if target_lexical.parent != root_lexical:
        raise UnsafeDisposableDatabase("database must be a direct child of disposable root")
    _reject_reparse_components(
        target_lexical,
        below=temp_root_lexical,
        label='database target',
    )

    # Canonical containment remains a separate defence after the lexical path
    # and every user-controlled reparse component have been rejected.
    temp_root = temp_root_lexical.resolve()
    root = root_lexical.resolve()
    target = target_lexical.resolve()

    if not _inside(root, temp_root) or root == temp_root:
        raise UnsafeDisposableDatabase("disposable root must be below the OS temp directory")
    if not root.name.startswith(ROOT_PREFIX):
        raise UnsafeDisposableDatabase(f"disposable root must start with {ROOT_PREFIX!r}")
    if not root.is_dir():
        raise UnsafeDisposableDatabase("disposable root must be an existing non-symlink directory")
    marker = root / ROOT_MARKER_NAME
    if _is_link_or_reparse(root_lexical / ROOT_MARKER_NAME):
        raise UnsafeDisposableDatabase("disposable root marker must not be a link or reparse point")
    if not marker.is_file():
        raise UnsafeDisposableDatabase("disposable root marker is missing or is a symlink")
    if marker.read_text(encoding="utf-8") != ROOT_MARKER_VALUE:
        raise UnsafeDisposableDatabase("disposable root marker value is invalid")
    if target.parent != root or not _inside(target, root):
        raise UnsafeDisposableDatabase("database must be a direct child of disposable root")
    if target.suffix.lower() != DATABASE_SUFFIX:
        raise UnsafeDisposableDatabase(f"database must end with {DATABASE_SUFFIX}")
    if target.name in {"db.sqlite3", "production.sqlite3", "qa.sqlite3", "default.sqlite3"}:
        raise UnsafeDisposableDatabase("production/default-like database name is forbidden")
    if require_database_missing and target.exists():
        raise UnsafeDisposableDatabase("disposable database must not already exist")
    return target, root


def verify_database_marker(connection) -> None:
    if connection.vendor != "sqlite":
        raise UnsafeDisposableDatabase("load fixture requires the SQLite engine")
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT marker_value FROM "{DATABASE_MARKER_TABLE}" WHERE marker_key = %s',
            ["identity"],
        )
        row = cursor.fetchone()
    if row != (DATABASE_MARKER_VALUE,):
        raise UnsafeDisposableDatabase("database-level disposable marker is absent or invalid")
