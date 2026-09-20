from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = BACKEND_ROOT / "config" / "settings.py"
TEST_LABELS_RE = re.compile(r"[A-Za-z0-9_.]+(?: [A-Za-z0-9_.]+)*")


def _installed_first_party_apps() -> set[str]:
    tree = ast.parse(SETTINGS_PATH.read_text(encoding="utf-8"))
    installed_apps: list[str] | None = None

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "INSTALLED_APPS"
            for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError("INSTALLED_APPS must be a literal list of strings")
        installed_apps = value
        break

    if installed_apps is None:
        raise ValueError("INSTALLED_APPS was not found in config/settings.py")

    return {
        item.split(".apps.", 1)[0]
        for item in installed_apps
        if not item.startswith("django.")
    }


def _validate_groups(groups: object, *, matrix_name: str) -> list[dict[str, object]]:
    if not isinstance(groups, list) or not groups:
        raise ValueError(f"{matrix_name} matrix must be a non-empty list")

    names: set[str] = set()
    validated: list[dict[str, object]] = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError(f"every {matrix_name} group must be an object")
        name = group.get("name")
        tests = group.get("tests")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"every {matrix_name} group needs a non-empty name")
        if name in names:
            raise ValueError(f"duplicate {matrix_name} group name: {name}")
        if not isinstance(tests, str) or not tests.strip():
            raise ValueError(f"{matrix_name} group {name} needs test labels")
        if not TEST_LABELS_RE.fullmatch(tests):
            raise ValueError(
                f"{matrix_name} group {name} has unsafe test labels: {tests!r}"
            )
        names.add(name)
        validated.append(group)
    return validated


def _discovered_test_modules(app: str) -> set[str]:
    app_root = BACKEND_ROOT / app
    modules: set[str] = set()
    for test_path in app_root.rglob("test*.py"):
        if "__pycache__" in test_path.parts:
            continue
        relative = test_path.relative_to(BACKEND_ROOT).with_suffix("")
        modules.add(".".join(relative.parts))
    return modules


def validate_manifest(manifest_path: Path) -> dict[str, list[dict[str, object]]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("test matrix manifest must be a JSON object")

    sqlite = _validate_groups(payload.get("sqlite"), matrix_name="sqlite")
    postgresql = _validate_groups(
        payload.get("postgresql"), matrix_name="postgresql"
    )

    covered_apps: list[str] = []
    for group in sqlite:
        apps = group.get("apps")
        if not isinstance(apps, list) or not all(
            isinstance(app, str) and app for app in apps
        ):
            raise ValueError(f"sqlite group {group['name']} needs an apps list")
        covered_apps.extend(apps)

    duplicate_apps = sorted(
        app for app in set(covered_apps) if covered_apps.count(app) > 1
    )
    if duplicate_apps:
        raise ValueError(
            "first-party apps assigned more than once: " + ", ".join(duplicate_apps)
        )

    expected_apps = _installed_first_party_apps()
    actual_apps = set(covered_apps)
    if actual_apps != expected_apps:
        missing = sorted(expected_apps - actual_apps)
        unexpected = sorted(actual_apps - expected_apps)
        raise ValueError(
            f"SQLite app coverage mismatch; missing={missing}, unexpected={unexpected}"
        )

    sqlite_labels = {
        label
        for group in sqlite
        for label in str(group["tests"]).split()
    }
    uncovered_test_modules: list[str] = []
    for app in sorted(expected_apps):
        if app in sqlite_labels:
            continue
        uncovered_test_modules.extend(
            sorted(_discovered_test_modules(app) - sqlite_labels)
        )
    if uncovered_test_modules:
        raise ValueError(
            "SQLite test modules are not covered by an app label or an explicit "
            "module label: " + ", ".join(uncovered_test_modules)
        )

    return {"sqlite": sqlite, "postgresql": postgresql}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and export the GitHub Actions Django test matrix."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    matrix = validate_manifest(args.manifest.resolve())
    compact = {
        name: json.dumps(groups, ensure_ascii=False, separators=(",", ":"))
        for name, groups in matrix.items()
    }

    if args.github_output:
        with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
            for name, value in compact.items():
                output.write(f"{name}={value}\n")
    else:
        for name, value in compact.items():
            print(f"{name}={value}")


if __name__ == "__main__":
    main()
