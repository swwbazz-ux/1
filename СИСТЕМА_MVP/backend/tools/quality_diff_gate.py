from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
EXCLUDED_PARTS = {
    ".venv",
    "artifacts",
    "generated",
    "media",
    "migrations",
    "node_modules",
    "private_media",
    "qa_artifacts",
    "staticfiles",
    "vendor",
    "venv",
}
EXISTING_RULES = "E9,F63,F7,F82"
NEW_FILE_RULES = "E4,E7,E9,F,I"
MAX_ANNOTATIONS = 50


class QualityGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiffEntry:
    status: str
    old_path: str | None
    path: str


@dataclass(frozen=True)
class PythonChange:
    status: str
    old_path: str | None
    path: str
    scope: str
    added_lines: frozenset[int]

    @property
    def is_new(self) -> bool:
        return self.scope == "new-file"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    column: int
    code: str
    message: str
    kind: str
    scope: str
    blocking: bool


def run_process(
    args: Sequence[str],
    *,
    cwd: Path,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def decode_output(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def git_bytes(repo_root: Path, *args: str) -> bytes:
    completed = run_process(("git", *args), cwd=repo_root)
    if completed.returncode != 0:
        detail = decode_output(completed.stderr).strip()
        raise QualityGateError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def git_text(repo_root: Path, *args: str) -> str:
    return decode_output(git_bytes(repo_root, *args)).strip()


def repository_root(start: Path | None = None) -> Path:
    root = git_text(start or Path.cwd(), "rev-parse", "--show-toplevel")
    return Path(root).resolve()


def resolve_commit(repo_root: Path, revision: str) -> str:
    resolved = git_text(repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if not FULL_SHA_RE.fullmatch(resolved):
        raise QualityGateError(f"revision did not resolve to a full commit SHA: {revision}")
    return resolved


def resolve_direct_base(repo_root: Path, base: str, head_sha: str) -> str:
    if not base or set(base) == {"0"}:
        parent = run_process(
            ("git", "rev-parse", "--verify", f"{head_sha}^"),
            cwd=repo_root,
        )
        if parent.returncode == 0:
            resolved = decode_output(parent.stdout).strip()
            if FULL_SHA_RE.fullmatch(resolved):
                return resolved
        return EMPTY_TREE_SHA
    return resolve_commit(repo_root, base)


def resolve_diff_base(
    repo_root: Path,
    *,
    base: str,
    head_sha: str,
    diff_mode: str,
) -> tuple[str, str]:
    if diff_mode == "direct":
        base_sha = resolve_direct_base(repo_root, base, head_sha)
        return base_sha, base_sha
    if diff_mode != "merge-base":
        raise QualityGateError(f"unsupported diff mode: {diff_mode}")
    base_sha = resolve_commit(repo_root, base)
    effective = git_text(repo_root, "merge-base", base_sha, head_sha)
    if not FULL_SHA_RE.fullmatch(effective):
        raise QualityGateError("git merge-base did not return a full commit SHA")
    return base_sha, effective


def ensure_exact_clean_checkout(repo_root: Path, head_sha: str) -> None:
    checkout_sha = resolve_commit(repo_root, "HEAD")
    if checkout_sha != head_sha:
        raise QualityGateError(
            f"checkout HEAD {checkout_sha} does not match requested head {head_sha}"
        )
    dirty = git_bytes(
        repo_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if dirty:
        raise QualityGateError("quality diff requires a clean worktree")


def canonical_git_path(raw: bytes) -> str:
    value = raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value:
        raise QualityGateError(f"unsafe Git path: {value!r}")
    return path.as_posix()


def parse_name_status_z(payload: bytes) -> list[DiffEntry]:
    tokens = payload.split(b"\0")
    if tokens and not tokens[-1]:
        tokens.pop()
    entries: list[DiffEntry] = []
    index = 0
    while index < len(tokens):
        status_token = tokens[index].decode("ascii", errors="strict")
        index += 1
        status = status_token[:1]
        if status in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise QualityGateError("truncated rename/copy record from git diff")
            old_path = canonical_git_path(tokens[index])
            path = canonical_git_path(tokens[index + 1])
            index += 2
        else:
            if index >= len(tokens):
                raise QualityGateError("truncated path record from git diff")
            old_path = None
            path = canonical_git_path(tokens[index])
            index += 1
        entries.append(DiffEntry(status=status, old_path=old_path, path=path))
    return entries


def is_excluded_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    return parsed.suffix != ".py" or any(part in EXCLUDED_PARTS for part in parsed.parts)


def parse_added_lines(patch: str) -> frozenset[int]:
    added: set[int] = set()
    for line in patch.splitlines():
        match = HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or "1")
        if count > 0:
            added.update(range(start, start + count))
    return frozenset(added)


def added_lines_for_entry(
    repo_root: Path,
    *,
    effective_base: str,
    head_sha: str,
    entry: DiffEntry,
) -> frozenset[int]:
    paths = [entry.path]
    if entry.old_path and entry.old_path != entry.path:
        paths.insert(0, entry.old_path)
    patch = git_text(
        repo_root,
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames=50%",
        effective_base,
        head_sha,
        "--",
        *paths,
    )
    return parse_added_lines(patch)


def collect_changes(
    repo_root: Path,
    *,
    effective_base: str,
    head_sha: str,
) -> tuple[list[DiffEntry], list[PythonChange], list[DiffEntry]]:
    payload = git_bytes(
        repo_root,
        "diff",
        "--name-status",
        "-z",
        "--find-renames=50%",
        "--diff-filter=ACMRTD",
        effective_base,
        head_sha,
        "--",
        "*.py",
    )
    entries = parse_name_status_z(payload)
    changes: list[PythonChange] = []
    excluded: list[DiffEntry] = []

    for entry in entries:
        destination_excluded = is_excluded_path(entry.path)
        source_excluded = bool(entry.old_path and is_excluded_path(entry.old_path))
        if entry.status == "D" or destination_excluded:
            excluded.append(entry)
            continue
        if entry.status == "T":
            excluded.append(entry)
            continue

        is_new = entry.status in {"A", "C"} or (entry.status == "R" and source_excluded)
        added_lines = frozenset()
        if not is_new:
            added_lines = added_lines_for_entry(
                repo_root,
                effective_base=effective_base,
                head_sha=head_sha,
                entry=entry,
            )
        changes.append(
            PythonChange(
                status=entry.status,
                old_path=entry.old_path,
                path=entry.path,
                scope="new-file" if is_new else "added-line",
                added_lines=added_lines,
            )
        )
    return entries, changes, excluded


def normalize_ruff_path(repo_root: Path, filename: str) -> str:
    candidate = Path(filename)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(repo_root)
        except ValueError as exc:
            raise QualityGateError(f"Ruff returned a path outside repository: {filename}") from exc
    value = candidate.as_posix()
    if value.startswith("./"):
        value = value[2:]
    return canonical_git_path(value.encode("utf-8", errors="surrogateescape"))


def diagnostic_intersects(diagnostic: dict[str, object], lines: frozenset[int]) -> bool:
    location = diagnostic.get("location")
    end_location = diagnostic.get("end_location")
    if not isinstance(location, dict):
        raise QualityGateError("Ruff diagnostic is missing location")
    start = location.get("row")
    if not isinstance(start, int):
        raise QualityGateError("Ruff diagnostic row is invalid")
    end = start
    if isinstance(end_location, dict) and isinstance(end_location.get("row"), int):
        end = int(end_location["row"])
    return any(line in lines for line in range(start, max(start, end) + 1))


def invoke_ruff_json(
    repo_root: Path,
    *,
    config_path: Path,
    paths: Sequence[str],
    select: str,
) -> list[dict[str, object]]:
    if not paths:
        return []
    completed = run_process(
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format",
            "json",
            "--config",
            str(config_path),
            "--select",
            select,
            "--",
            *paths,
        ),
        cwd=repo_root,
    )
    if completed.returncode not in {0, 1}:
        detail = decode_output(completed.stderr).strip()
        raise QualityGateError(f"Ruff check failed with exit {completed.returncode}: {detail}")
    try:
        payload = json.loads(decode_output(completed.stdout) or "[]")
    except json.JSONDecodeError as exc:
        raise QualityGateError("Ruff returned malformed JSON") from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise QualityGateError("Ruff JSON payload must be a list of objects")
    return payload


def ruff_findings(
    repo_root: Path,
    *,
    config_path: Path,
    changes: Sequence[PythonChange],
) -> list[Finding]:
    by_path = {change.path: change for change in changes}
    findings: list[Finding] = []
    groups = (
        ([change.path for change in changes if change.is_new], NEW_FILE_RULES),
        ([change.path for change in changes if not change.is_new], EXISTING_RULES),
    )
    for paths, select in groups:
        if not paths:
            continue
        for diagnostic in invoke_ruff_json(
            repo_root,
            config_path=config_path,
            paths=paths,
            select=select,
        ):
            filename = diagnostic.get("filename")
            code = diagnostic.get("code")
            message = diagnostic.get("message")
            location = diagnostic.get("location")
            if not isinstance(filename, str) or not isinstance(code, str):
                raise QualityGateError("Ruff diagnostic is missing filename or code")
            if not isinstance(message, str) or not isinstance(location, dict):
                raise QualityGateError("Ruff diagnostic is missing message or location")
            path = normalize_ruff_path(repo_root, filename)
            change = by_path.get(path)
            if change is None:
                raise QualityGateError(f"Ruff returned an unexpected file: {path}")
            if not change.is_new and not diagnostic_intersects(
                diagnostic,
                change.added_lines,
            ):
                continue
            row = location.get("row")
            column = location.get("column")
            if not isinstance(row, int) or not isinstance(column, int):
                raise QualityGateError("Ruff diagnostic location is invalid")
            findings.append(
                Finding(
                    path=path,
                    line=row,
                    column=column,
                    code=code,
                    message=message,
                    kind="lint",
                    scope=change.scope,
                    blocking=True,
                )
            )
    return findings


def format_findings(
    repo_root: Path,
    *,
    config_path: Path,
    changes: Sequence[PythonChange],
) -> list[Finding]:
    findings: list[Finding] = []
    for change in changes:
        if not change.is_new:
            continue
        completed = run_process(
            (
                sys.executable,
                "-m",
                "ruff",
                "format",
                "--check",
                "--no-cache",
                "--config",
                str(config_path),
                "--",
                change.path,
            ),
            cwd=repo_root,
        )
        if completed.returncode == 0:
            continue
        if completed.returncode != 1:
            detail = decode_output(completed.stderr).strip()
            raise QualityGateError(
                f"Ruff format check failed with exit {completed.returncode}: {detail}"
            )
        findings.append(
            Finding(
                path=change.path,
                line=1,
                column=1,
                code="FORMAT",
                message="New Python file is not formatted by Ruff",
                kind="format",
                scope="new-file",
                blocking=True,
            )
        )
    return findings


def is_production_backend_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    parts = parsed.parts
    if len(parts) < 4 or parts[:2] != ("СИСТЕМА_MVP", "backend"):
        return False
    if "tools" in parts or "tests" in parts or "migrations" in parts:
        return False
    filename = parsed.name
    return filename != "tests.py" and not filename.startswith("test_")


def size_findings(repo_root: Path, changes: Sequence[PythonChange]) -> list[Finding]:
    findings: list[Finding] = []
    for change in changes:
        if not is_production_backend_path(change.path):
            continue
        path = repo_root / Path(change.path)
        source = path.read_text(encoding="utf-8")
        physical_lines = len(source.splitlines())
        if change.is_new and physical_lines > 500:
            findings.append(
                Finding(
                    path=change.path,
                    line=1,
                    column=1,
                    code="SIZE500",
                    message=f"New production file has {physical_lines} lines (warning threshold: 500)",
                    kind="size",
                    scope="new-file",
                    blocking=False,
                )
            )
        try:
            tree = ast.parse(source, filename=change.path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not change.is_new and node.lineno not in change.added_lines:
                continue
            end_lineno = getattr(node, "end_lineno", node.lineno)
            length = end_lineno - node.lineno + 1
            if length <= 100:
                continue
            findings.append(
                Finding(
                    path=change.path,
                    line=node.lineno,
                    column=node.col_offset + 1,
                    code="FUNC100",
                    message=f"New function {node.name} has {length} lines (warning threshold: 100)",
                    kind="size",
                    scope=change.scope,
                    blocking=False,
                )
            )
    return findings


def ruff_version(repo_root: Path) -> str:
    completed = run_process((sys.executable, "-m", "ruff", "--version"), cwd=repo_root)
    if completed.returncode != 0:
        detail = decode_output(completed.stderr).strip()
        raise QualityGateError(f"Ruff version check failed: {detail}")
    version = decode_output(completed.stdout).strip()
    if version != "ruff 0.16.8":
        raise QualityGateError(f"unexpected Ruff version: {version}")
    return version.removeprefix("ruff ")


def github_escape(value: str, *, property_value: bool = False) -> str:
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        escaped = escaped.replace(":", "%3A").replace(",", "%2C")
    return escaped


def emit_annotations(findings: Sequence[Finding]) -> None:
    for finding in findings[:MAX_ANNOTATIONS]:
        path = github_escape(finding.path, property_value=True)
        title = github_escape(f"{finding.code} ({finding.scope})", property_value=True)
        message = github_escape(finding.message)
        print(
            f"::warning file={path},line={finding.line},col={finding.column},"
            f"title={title}::{message}"
        )
    remaining = len(findings) - MAX_ANNOTATIONS
    if remaining > 0:
        print(f"::warning title=Ruff ratchet::Full JSON report contains {remaining} more findings")


def markdown_report(report: dict[str, object]) -> str:
    counts = report["counts"]
    assert isinstance(counts, dict)
    lines = [
        "## Ruff ratchet — REPORT ONLY",
        "",
        "Этот этап не входит в Required quality gate и пока не блокирует merge.",
        "",
        f"- Status: `{report['status']}`",
        f"- Diff: `{report['resolved_diff_base_sha']}` → `{report['head_sha']}` ({report['diff_mode']})",
        f"- Ruff: `{report['ruff_version']}`",
        f"- Python files: `{counts['python_files']}`; new: `{counts['new_python_files']}`; existing: `{counts['existing_python_files']}`",
        f"- Lint findings: `{counts['lint_findings']}`; format findings: `{counts['format_findings']}`; size warnings: `{counts['size_warnings']}`",
    ]
    findings = report.get("findings")
    if isinstance(findings, list) and findings:
        lines.extend(["", "### Первые замечания", ""])
        for item in findings[:20]:
            if not isinstance(item, dict):
                continue
            lines.append(f"- `{item['path']}:{item['line']}` `{item['code']}` — {item['message']}")
        if len(findings) > 20:
            lines.append(f"- Ещё {len(findings) - 20} замечаний находятся в JSON artifact.")
    return "\n".join(lines) + "\n"


def write_text(path: Path | None, content: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def write_json(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def result_exit_code(*, report_only: bool, findings: Sequence[Finding]) -> int:
    has_blocking = any(finding.blocking for finding in findings)
    if report_only or not has_blocking:
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report Ruff findings only in added Python lines and new files."
    )
    parser.add_argument("--base", required=True, help="Base commit or commit-ish")
    parser.add_argument("--head", required=True, help="Exact checked out head commit")
    parser.add_argument(
        "--diff-mode",
        choices=("merge-base", "direct"),
        required=True,
    )
    parser.add_argument("--event", default="local")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--github-annotations", action="store_true")
    parser.add_argument("--config", type=Path)
    return parser


def execute(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    repo_root = repository_root()
    head_sha = resolve_commit(repo_root, args.head)
    ensure_exact_clean_checkout(repo_root, head_sha)
    base_sha, effective_base = resolve_diff_base(
        repo_root,
        base=args.base,
        head_sha=head_sha,
        diff_mode=args.diff_mode,
    )
    config_path = (args.config or (repo_root / "pyproject.toml")).resolve()
    if not config_path.is_file():
        raise QualityGateError(f"Ruff config was not found: {config_path}")
    version = ruff_version(repo_root)
    entries, changes, excluded = collect_changes(
        repo_root,
        effective_base=effective_base,
        head_sha=head_sha,
    )
    findings = [
        *ruff_findings(repo_root, config_path=config_path, changes=changes),
        *format_findings(repo_root, config_path=config_path, changes=changes),
        *size_findings(repo_root, changes),
    ]
    lint_count = sum(finding.kind == "lint" for finding in findings)
    format_count = sum(finding.kind == "format" for finding in findings)
    size_count = sum(finding.kind == "size" for finding in findings)
    report: dict[str, object] = {
        "schema": 1,
        "mode": "report-only" if args.report_only else "enforce",
        "status": "findings" if findings else "clean",
        "event": args.event,
        "diff_mode": args.diff_mode,
        "base_sha": base_sha,
        "resolved_diff_base_sha": effective_base,
        "head_sha": head_sha,
        "ruff_version": version,
        "counts": {
            "changed_files": len(entries),
            "python_files": len(changes),
            "new_python_files": sum(change.is_new for change in changes),
            "existing_python_files": sum(not change.is_new for change in changes),
            "excluded_files": len(excluded),
            "lint_findings": lint_count,
            "format_findings": format_count,
            "size_warnings": size_count,
        },
        "findings": [asdict(finding) for finding in findings],
    }
    if args.github_annotations:
        emit_annotations(findings)
    return result_exit_code(report_only=args.report_only, findings=findings), report


def error_report(args: argparse.Namespace, message: str) -> dict[str, object]:
    return {
        "schema": 1,
        "mode": "report-only" if args.report_only else "enforce",
        "status": "indeterminate",
        "event": args.event,
        "error": message,
    }


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        exit_code, report = execute(args)
    except (OSError, QualityGateError, UnicodeError, json.JSONDecodeError) as exc:
        report = error_report(args, str(exc))
        write_json(args.json_output, report)
        write_text(
            args.markdown_output,
            f"## Ruff ratchet — REPORT NOT PRODUCED\n\nInfrastructure error: `{exc}`\n",
        )
        if args.github_annotations:
            print(f"::error title=Ruff ratchet infrastructure::{github_escape(str(exc))}")
        print(f"quality diff gate failed: {exc}", file=sys.stderr)
        return 2

    write_json(args.json_output, report)
    write_text(args.markdown_output, markdown_report(report))
    counts = report["counts"]
    assert isinstance(counts, dict)
    print(
        "Ruff ratchet report: "
        f"status={report['status']} files={counts['python_files']} "
        f"lint={counts['lint_findings']} format={counts['format_findings']} "
        f"size={counts['size_warnings']}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
