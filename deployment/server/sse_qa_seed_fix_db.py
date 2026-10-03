"""Narrow, fail-closed database transition for the installed SSE QA fixture.

The public CLI deliberately accepts only one of three fixed actions::

    python sse_qa_seed_fix_db.py inspect
    python sse_qa_seed_fix_db.py apply
    python sse_qa_seed_fix_db.py rollback

It must be executed by the protected controller from the installed QA backend
with the installed QA environment.  No database names, paths, SQL, object IDs,
or other caller-controlled parameters are accepted.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID


SCHEMA = "SSE_QA_SEED_FIX_DB_V1"
MARKER = "SSE_QA_DATABASE_V1"
DATABASE_NAME = "sseqa"
DATABASE_PORT = "55432"
EXCAVATOR_NUMBER = "SSE-QA-EXC-01"

LEGACY_ROCK_NAME = "Порода SSE QA"
LEGACY_ROCK_DENSITY = Decimal("2.5000")
LEGACY_ROCK_LOOSENING_FACTOR = Decimal("1.0000")

CANONICAL_ROCK_NAME = "Скальная порода"
CANONICAL_ROCK_DENSITY = Decimal("2.7100")
CANONICAL_ROCK_LOOSENING_FACTOR = Decimal("1.5100")

ALLOWED_ACTIONS = frozenset({"inspect", "apply", "rollback"})
REAL_BACKEND = Path("/srv/sse-qa/releases/r3/backend")
CURRENT_BACKEND = Path("/srv/sse-qa/current/backend")

# Every concrete managed model in these installed C2 applications is protected,
# not just a hand-picked subset of history tables.  The two intentional targets
# are masked/handled by dedicated digest functions below.
PROTECTED_APP_LABELS = frozenset(
    {"assignments", "core", "references", "shifts", "trips", "users"}
)
INTENTIONAL_MODEL_LABELS = frozenset(
    {"assignments.excavatorplacement", "references.rocktype"}
)


class SeedFixError(RuntimeError):
    """Expected fail-closed rejection with a fixed, non-sensitive code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RockRecord:
    pk: int
    name: str
    density: Decimal | None
    loosening_factor: Decimal | None
    is_active: bool


@dataclass(frozen=True)
class PlacementRecord:
    pk: int
    excavator_pk: int
    rock_pk: int | None


@dataclass(frozen=True)
class LockedState:
    legacy: RockRecord
    canonical: RockRecord | None
    placement: PlacementRecord


@dataclass(frozen=True)
class Result:
    action: str
    legacy_id: int
    canonical_id: int | None
    placement_id: int
    protected_digest: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "action": self.action,
            "legacy_id": self.legacy_id,
            "canonical_id": self.canonical_id,
            "placement_id": self.placement_id,
            "protected_digest": self.protected_digest,
        }


class Store(Protocol):
    def atomic(self) -> AbstractContextManager[Any]: ...

    def validate_target(self) -> None: ...

    def load_locked_state(self) -> LockedState: ...

    def protected_digest(self, placement_id: int) -> str: ...

    def create_canonical(self) -> RockRecord: ...

    def set_placement_rock(
        self, placement_id: int, expected_rock_id: int, new_rock_id: int
    ) -> None: ...

    def assert_canonical_only_target_reference(
        self, canonical_id: int, placement_id: int
    ) -> None: ...

    def delete_canonical(self, canonical_id: int) -> None: ...


def _is_loopback_host(value: Any) -> bool:
    host = str(value or "").strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_database_identity(connection: Any) -> None:
    """Validate the fixed QA PostgreSQL endpoint and installation marker."""

    settings_dict = connection.settings_dict
    if connection.vendor != "postgresql":
        raise SeedFixError("invalid_database_vendor")
    if str(settings_dict.get("NAME") or "") != DATABASE_NAME:
        raise SeedFixError("invalid_database_name")
    if not _is_loopback_host(settings_dict.get("HOST")):
        raise SeedFixError("invalid_database_host")
    if str(settings_dict.get("PORT") or "") != DATABASE_PORT:
        raise SeedFixError("invalid_database_port")

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT identity FROM sse_qa_install_marker WHERE identity = %s",
                [MARKER],
            )
            row = cursor.fetchone()
    except Exception as error:
        raise SeedFixError("missing_database_marker") from error
    if row != (MARKER,):
        raise SeedFixError("missing_database_marker")


def _rock_matches(
    rock: RockRecord,
    *,
    name: str,
    density: Decimal,
    loosening_factor: Decimal,
) -> bool:
    return (
        rock.name == name
        and rock.density == density
        and rock.loosening_factor == loosening_factor
        and rock.is_active is True
    )


def classify_state(state: LockedState) -> str:
    """Return ``old`` or ``fixed``; reject every partial/mixed state."""

    if not _rock_matches(
        state.legacy,
        name=LEGACY_ROCK_NAME,
        density=LEGACY_ROCK_DENSITY,
        loosening_factor=LEGACY_ROCK_LOOSENING_FACTOR,
    ):
        raise SeedFixError("legacy_rock_mismatch")

    if state.canonical is None:
        if state.placement.rock_pk != state.legacy.pk:
            raise SeedFixError("mixed_seed_state")
        return "old"

    if not _rock_matches(
        state.canonical,
        name=CANONICAL_ROCK_NAME,
        density=CANONICAL_ROCK_DENSITY,
        loosening_factor=CANONICAL_ROCK_LOOSENING_FACTOR,
    ):
        raise SeedFixError("canonical_rock_mismatch")
    if state.placement.rock_pk != state.canonical.pk:
        raise SeedFixError("mixed_seed_state")
    return "fixed"


def _assert_digest_unchanged(before: str, after: str) -> None:
    if before != after:
        raise SeedFixError("protected_state_changed")


def run_store_action(store: Store, action: str) -> Result:
    """Run one transition within one DB transaction.

    ``rollback`` is intentionally stateless at the CLI boundary.  The protected
    controller must call it only when the same controller invocation observed
    ``inspect=old`` and then received ``apply=applied``.  The helper adds a
    second safety boundary: rollback accepts only the exact fixed DB state and
    refuses deletion if any row other than the exact target placement refers
    to the canonical rock.
    """

    if action not in ALLOWED_ACTIONS:
        raise SeedFixError("invalid_action")

    with store.atomic():
        store.validate_target()
        before_state = store.load_locked_state()
        before_phase = classify_state(before_state)
        before_digest = store.protected_digest(before_state.placement.pk)

        if action == "inspect":
            after_state = store.load_locked_state()
            after_phase = classify_state(after_state)
            after_digest = store.protected_digest(after_state.placement.pk)
            if after_phase != before_phase:
                raise SeedFixError("concurrent_seed_state_change")
            _assert_digest_unchanged(before_digest, after_digest)
            return Result(
                action=after_phase,
                legacy_id=after_state.legacy.pk,
                canonical_id=(
                    after_state.canonical.pk if after_state.canonical else None
                ),
                placement_id=after_state.placement.pk,
                protected_digest=after_digest,
            )

        if action == "apply":
            if before_phase == "fixed":
                after_state = store.load_locked_state()
                after_digest = store.protected_digest(after_state.placement.pk)
                if classify_state(after_state) != "fixed":
                    raise SeedFixError("concurrent_seed_state_change")
                _assert_digest_unchanged(before_digest, after_digest)
                return Result(
                    action="already_applied",
                    legacy_id=after_state.legacy.pk,
                    canonical_id=after_state.canonical.pk,
                    placement_id=after_state.placement.pk,
                    protected_digest=after_digest,
                )

            canonical = store.create_canonical()
            if not _rock_matches(
                canonical,
                name=CANONICAL_ROCK_NAME,
                density=CANONICAL_ROCK_DENSITY,
                loosening_factor=CANONICAL_ROCK_LOOSENING_FACTOR,
            ):
                raise SeedFixError("canonical_create_mismatch")
            store.set_placement_rock(
                before_state.placement.pk,
                before_state.legacy.pk,
                canonical.pk,
            )
            after_state = store.load_locked_state()
            if (
                classify_state(after_state) != "fixed"
                or after_state.canonical is None
                or after_state.canonical.pk != canonical.pk
            ):
                raise SeedFixError("apply_verification_failed")
            after_digest = store.protected_digest(after_state.placement.pk)
            _assert_digest_unchanged(before_digest, after_digest)
            return Result(
                action="applied",
                legacy_id=after_state.legacy.pk,
                canonical_id=after_state.canonical.pk,
                placement_id=after_state.placement.pk,
                protected_digest=after_digest,
            )

        # rollback
        if before_phase != "fixed" or before_state.canonical is None:
            raise SeedFixError("rollback_requires_fixed_state")
        canonical_id = before_state.canonical.pk
        store.assert_canonical_only_target_reference(
            canonical_id, before_state.placement.pk
        )
        store.set_placement_rock(
            before_state.placement.pk,
            canonical_id,
            before_state.legacy.pk,
        )
        store.delete_canonical(canonical_id)
        after_state = store.load_locked_state()
        if classify_state(after_state) != "old":
            raise SeedFixError("rollback_verification_failed")
        after_digest = store.protected_digest(after_state.placement.pk)
        _assert_digest_unchanged(before_digest, after_digest)
        return Result(
            action="rolled_back",
            legacy_id=after_state.legacy.pk,
            canonical_id=canonical_id,
            placement_id=after_state.placement.pk,
            protected_digest=after_digest,
        )


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return {"decimal": format(value, "f")}
    if isinstance(value, (datetime, date, time)):
        return {"iso8601": value.isoformat()}
    if isinstance(value, UUID):
        return {"uuid": str(value)}
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, float):
        return {"float": repr(value)}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, Path):
        return str(value)
    raise SeedFixError("unsupported_protected_value")


class DjangoStore:
    """Real ORM adapter.  Imports happen only after ``django.setup()``."""

    def __init__(self) -> None:
        from django.apps import apps
        from django.db import connection, transaction

        self._apps = apps
        self._connection = connection
        self._transaction = transaction

    def atomic(self) -> AbstractContextManager[Any]:
        return self._transaction.atomic()

    def validate_target(self) -> None:
        validate_database_identity(self._connection)

    def _model(self, app_label: str, model_name: str) -> Any:
        model = self._apps.get_model(app_label, model_name)
        if model is None:
            raise SeedFixError("required_model_missing")
        return model

    @staticmethod
    def _one(rows: list[Any], *, missing: str, multiple: str) -> Any:
        if not rows:
            raise SeedFixError(missing)
        if len(rows) != 1:
            raise SeedFixError(multiple)
        return rows[0]

    @staticmethod
    def _rock_record(rock: Any) -> RockRecord:
        return RockRecord(
            pk=int(rock.pk),
            name=str(rock.name),
            density=rock.density,
            loosening_factor=rock.loosening_factor,
            is_active=rock.is_active,
        )

    def load_locked_state(self) -> LockedState:
        Equipment = self._model("references", "Equipment")
        RockType = self._model("references", "RockType")
        ExcavatorPlacement = self._model("assignments", "ExcavatorPlacement")

        equipment_rows = list(
            Equipment._base_manager.select_for_update()
            .select_related("equipment_type")
            .filter(garage_number=EXCAVATOR_NUMBER)
        )
        excavator = self._one(
            equipment_rows,
            missing="qa_excavator_missing",
            multiple="qa_excavator_not_unique",
        )
        if (
            excavator.is_active is not True
            or excavator.equipment_type is None
            or excavator.equipment_type.name != "Экскаватор"
        ):
            raise SeedFixError("qa_excavator_mismatch")

        placement_rows = list(
            ExcavatorPlacement._base_manager.select_for_update().filter(
                excavator_id=excavator.pk
            )
        )
        placement = self._one(
            placement_rows,
            missing="qa_placement_missing",
            multiple="qa_placement_not_unique",
        )

        rock_rows = list(
            RockType._base_manager.select_for_update().filter(
                name__in=(LEGACY_ROCK_NAME, CANONICAL_ROCK_NAME)
            )
        )
        by_name = {rock.name: rock for rock in rock_rows}
        if len(by_name) != len(rock_rows):
            raise SeedFixError("qa_rock_not_unique")
        legacy_model = by_name.get(LEGACY_ROCK_NAME)
        if legacy_model is None:
            raise SeedFixError("legacy_rock_missing")
        canonical_model = by_name.get(CANONICAL_ROCK_NAME)

        return LockedState(
            legacy=self._rock_record(legacy_model),
            canonical=(
                self._rock_record(canonical_model)
                if canonical_model is not None
                else None
            ),
            placement=PlacementRecord(
                pk=int(placement.pk),
                excavator_pk=int(excavator.pk),
                rock_pk=(
                    int(placement.work_rock_type_id)
                    if placement.work_rock_type_id is not None
                    else None
                ),
            ),
        )

    @staticmethod
    def _digest_row(hasher: Any, label: str, fields: list[str], row: Any) -> None:
        payload = [label, fields, [_json_value(value) for value in row]]
        hasher.update(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        )
        hasher.update(b"\n")

    def _protected_models(self) -> list[Any]:
        models = []
        for model in self._apps.get_models(include_auto_created=False):
            options = model._meta
            if (
                options.app_label in PROTECTED_APP_LABELS
                and options.managed
                and not options.proxy
                and options.label_lower not in INTENTIONAL_MODEL_LABELS
            ):
                models.append(model)
        models.sort(key=lambda item: item._meta.label_lower)
        if not models:
            raise SeedFixError("protected_model_set_empty")
        return models

    def _digest_model_class(self, hasher: Any, model: Any) -> None:
        fields = [field.attname for field in model._meta.concrete_fields]
        pk_name = model._meta.pk.attname
        rows = (
            model._base_manager.select_for_update()
            .order_by(pk_name)
            .values_list(*fields)
        )
        label = model._meta.label_lower
        for row in rows.iterator(chunk_size=256):
            self._digest_row(hasher, label, fields, row)

    def _digest_placements(self, hasher: Any, placement_id: int) -> None:
        model = self._model("assignments", "ExcavatorPlacement")
        fields = [field.attname for field in model._meta.concrete_fields]
        rock_index = fields.index("work_rock_type_id")
        rows = (
            model._base_manager.select_for_update()
            .order_by(model._meta.pk.attname)
            .values_list(*fields)
        )
        for original in rows.iterator(chunk_size=256):
            row = list(original)
            if int(row[fields.index(model._meta.pk.attname)]) == placement_id:
                row[rock_index] = "<intentional-seed-fix-field>"
            self._digest_row(
                hasher, "assignments.excavatorplacement", fields, row
            )

    def _digest_other_rocks(self, hasher: Any) -> None:
        model = self._model("references", "RockType")
        fields = [field.attname for field in model._meta.concrete_fields]
        rows = (
            model._base_manager.select_for_update()
            .exclude(name=CANONICAL_ROCK_NAME)
            .order_by(model._meta.pk.attname)
            .values_list(*fields)
        )
        for row in rows.iterator(chunk_size=256):
            self._digest_row(hasher, "references.rocktype", fields, row)

    def protected_digest(self, placement_id: int) -> str:
        hasher = hashlib.sha256()
        for model in self._protected_models():
            self._digest_model_class(hasher, model)
        self._digest_placements(hasher, placement_id)
        self._digest_other_rocks(hasher)
        return hasher.hexdigest()

    def create_canonical(self) -> RockRecord:
        RockType = self._model("references", "RockType")
        try:
            # save()/create() emits the installed C2 post_save signal and would
            # create an OperationalStateEvent.  This fixed data repair must not
            # manufacture a business event, so use Django's signal-free bulk
            # insert and require PostgreSQL to return the generated primary key.
            rock = RockType(
                name=CANONICAL_ROCK_NAME,
                density=CANONICAL_ROCK_DENSITY,
                loosening_factor=CANONICAL_ROCK_LOOSENING_FACTOR,
                is_active=True,
            )
            created = RockType._base_manager.bulk_create([rock])
        except Exception as error:
            raise SeedFixError("canonical_create_failed") from error
        if created != [rock] or rock.pk is None:
            raise SeedFixError("canonical_create_failed")
        return self._rock_record(rock)

    def set_placement_rock(
        self, placement_id: int, expected_rock_id: int, new_rock_id: int
    ) -> None:
        ExcavatorPlacement = self._model("assignments", "ExcavatorPlacement")
        updated = ExcavatorPlacement._base_manager.filter(
            pk=placement_id,
            work_rock_type_id=expected_rock_id,
        ).update(work_rock_type_id=new_rock_id)
        if updated != 1:
            raise SeedFixError("placement_concurrent_change")

    def assert_canonical_only_target_reference(
        self, canonical_id: int, placement_id: int
    ) -> None:
        RockType = self._model("references", "RockType")
        canonical_rows = list(
            RockType._base_manager.select_for_update().filter(pk=canonical_id)
        )
        canonical = self._one(
            canonical_rows,
            missing="canonical_rock_missing",
            multiple="qa_rock_not_unique",
        )
        allowed_seen = False
        for relation in canonical._meta.related_objects:
            field = relation.field
            related_model = relation.related_model
            if getattr(field, "many_to_many", False):
                raise SeedFixError("canonical_rock_has_unexpected_relation")
            pk_name = related_model._meta.pk.attname
            related_ids = list(
                related_model._base_manager.select_for_update()
                .filter(**{field.attname: canonical_id})
                .order_by(pk_name)
                .values_list(pk_name, flat=True)
            )
            is_target_relation = (
                related_model._meta.label_lower
                == "assignments.excavatorplacement"
                and field.name == "work_rock_type"
            )
            if is_target_relation:
                if related_ids != [placement_id]:
                    raise SeedFixError("canonical_rock_placement_mismatch")
                allowed_seen = True
            elif related_ids:
                raise SeedFixError("canonical_rock_in_use")
        if not allowed_seen:
            raise SeedFixError("canonical_rock_placement_relation_missing")

    def delete_canonical(self, canonical_id: int) -> None:
        RockType = self._model("references", "RockType")
        queryset = RockType._base_manager.filter(
            pk=canonical_id,
            name=CANONICAL_ROCK_NAME,
            density=CANONICAL_ROCK_DENSITY,
            loosening_factor=CANONICAL_ROCK_LOOSENING_FACTOR,
            is_active=True,
        )
        # All reverse relations were locked and proven empty above.  The
        # regular delete() path emits C2 post_delete and would append an
        # operational event, so perform one exact signal-free SQL deletion.
        deleted = queryset._raw_delete(using=self._connection.alias)
        if deleted != 1:
            raise SeedFixError("canonical_delete_failed")


def _prepare_backend_import_path() -> None:
    try:
        cwd = Path.cwd().resolve(strict=True)
        current = CURRENT_BACKEND.resolve(strict=True)
    except OSError as error:
        raise SeedFixError("invalid_backend_runtime") from error
    if cwd != REAL_BACKEND or current != REAL_BACKEND:
        raise SeedFixError("invalid_backend_runtime")
    if not (cwd / "manage.py").is_file() or not (
        cwd / "config" / "settings.py"
    ).is_file():
        raise SeedFixError("invalid_backend_runtime")
    cwd_text = str(cwd)
    if cwd_text not in sys.path:
        sys.path.insert(0, cwd_text)


def run_django_action(action: str) -> Result:
    _prepare_backend_import_path()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    return run_store_action(DjangoStore(), action)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or arguments[0] not in ALLOWED_ACTIONS:
        _emit({"schema": SCHEMA, "action": "error", "error": "invalid_action"})
        return 64
    try:
        result = run_django_action(arguments[0])
    except SeedFixError as error:
        _emit({"schema": SCHEMA, "action": "error", "error": error.code})
        return 65
    except Exception:
        # Do not expose SQL, settings, credentials, paths, or row contents.
        _emit({"schema": SCHEMA, "action": "error", "error": "internal_error"})
        return 70
    _emit(result.payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
