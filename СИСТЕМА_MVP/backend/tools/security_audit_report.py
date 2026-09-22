from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class SecurityReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipPackageFinding:
    name: str
    version: str
    advisory_ids: tuple[str, ...]
    fix_versions: tuple[str, ...]


def read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecurityReportError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SecurityReportError(f"JSON root must be an object: {path}")
    if "error" in payload:
        raise SecurityReportError(f"audit tool returned an error object: {payload['error']}")
    return payload


def parse_pip_audit(payload: dict[str, object]) -> list[PipPackageFinding]:
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        raise SecurityReportError("pip-audit JSON is missing dependencies")
    findings: list[PipPackageFinding] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise SecurityReportError("pip-audit dependency must be an object")
        name = dependency.get("name")
        version = dependency.get("version")
        vulnerabilities = dependency.get("vulns")
        if not isinstance(name, str) or not isinstance(version, str):
            raise SecurityReportError("pip-audit dependency is missing name or version")
        if not isinstance(vulnerabilities, list):
            raise SecurityReportError("pip-audit dependency is missing vulns")
        advisory_ids: set[str] = set()
        fix_versions: set[str] = set()
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict) or not isinstance(vulnerability.get("id"), str):
                raise SecurityReportError("pip-audit vulnerability is missing id")
            advisory_ids.add(str(vulnerability["id"]))
            fixes = vulnerability.get("fix_versions", [])
            if not isinstance(fixes, list) or not all(isinstance(item, str) for item in fixes):
                raise SecurityReportError("pip-audit fix_versions must be a string list")
            fix_versions.update(fixes)
        if advisory_ids:
            findings.append(
                PipPackageFinding(
                    name=name,
                    version=version,
                    advisory_ids=tuple(sorted(advisory_ids)),
                    fix_versions=tuple(sorted(fix_versions)),
                )
            )
    return findings


def parse_npm_audit(payload: dict[str, object]) -> dict[str, int]:
    if payload.get("auditReportVersion") != 2:
        raise SecurityReportError("npm audit JSON version must be 2")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise SecurityReportError("npm audit JSON is missing metadata")
    vulnerabilities = metadata.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise SecurityReportError("npm audit JSON is missing vulnerability totals")
    result: dict[str, int] = {}
    for severity in ("info", "low", "moderate", "high", "critical", "total"):
        value = vulnerabilities.get(severity)
        if not isinstance(value, int) or value < 0:
            raise SecurityReportError(f"npm audit count is invalid: {severity}")
        result[severity] = value
    return result


def validate_tool_exit(name: str, exit_code: int | None, finding_count: int) -> None:
    if exit_code is None:
        return
    if exit_code not in {0, 1}:
        raise SecurityReportError(f"{name} returned unexpected exit code {exit_code}")
    if exit_code == 0 and finding_count:
        raise SecurityReportError(f"{name} returned clean exit with findings")
    if exit_code == 1 and not finding_count:
        raise SecurityReportError(f"{name} returned findings exit without findings")


def build_report(
    *,
    target_sha: str,
    pip_payload: dict[str, object] | None,
    npm_production_payload: dict[str, object] | None,
    npm_all_payload: dict[str, object] | None,
    pip_audit_version: str | None,
    npm_version: str | None,
    pip_exit_code: int | None = None,
    npm_production_exit_code: int | None = None,
    npm_all_exit_code: int | None = None,
) -> dict[str, object]:
    if pip_payload is None and npm_production_payload is None and npm_all_payload is None:
        raise SecurityReportError("at least one audit payload is required")
    report: dict[str, object] = {
        "schema": 1,
        "mode": "report-only",
        "target_sha": target_sha,
        "mechanism_status": "success",
        "findings_status": "clean",
    }
    finding_count = 0
    if pip_payload is not None:
        pip_findings = parse_pip_audit(pip_payload)
        pip_finding_count = sum(len(item.advisory_ids) for item in pip_findings)
        validate_tool_exit("pip-audit", pip_exit_code, pip_finding_count)
        finding_count += pip_finding_count
        report["python"] = {
            "tool": "pip-audit",
            "tool_version": pip_audit_version,
            "vulnerable_packages": len(pip_findings),
            "unique_advisories": sum(len(item.advisory_ids) for item in pip_findings),
            "packages": [
                {
                    "name": item.name,
                    "version": item.version,
                    "advisory_ids": list(item.advisory_ids),
                    "fix_versions": list(item.fix_versions),
                }
                for item in pip_findings
            ],
        }
    if npm_production_payload is not None or npm_all_payload is not None:
        if npm_production_payload is None or npm_all_payload is None:
            raise SecurityReportError("both production and complete npm reports are required")
        production = parse_npm_audit(npm_production_payload)
        complete = parse_npm_audit(npm_all_payload)
        validate_tool_exit("npm production audit", npm_production_exit_code, production["total"])
        validate_tool_exit("npm complete audit", npm_all_exit_code, complete["total"])
        finding_count += production["total"] + complete["total"]
        report["npm"] = {
            "tool": "npm audit",
            "tool_version": npm_version,
            "production": production,
            "complete": complete,
        }
    if finding_count:
        report["findings_status"] = "findings"
    return report


def markdown_report(report: dict[str, object]) -> str:
    lines = [
        "## Dependency security baseline — REPORT ONLY",
        "",
        "Находки не блокируют merge. Ошибка построения отчёта не маскируется.",
        "",
        f"- SHA: `{report['target_sha']}`",
        f"- Mechanism: `{report['mechanism_status']}`",
        f"- Findings: `{report['findings_status']}`",
    ]
    python = report.get("python")
    if isinstance(python, dict):
        lines.extend(
            [
                f"- pip-audit: `{python.get('tool_version')}`",
                f"- Python: vulnerable packages `{python.get('vulnerable_packages')}`, unique advisories `{python.get('unique_advisories')}`",
            ]
        )
        packages = python.get("packages")
        if isinstance(packages, list):
            for package in packages:
                if not isinstance(package, dict):
                    continue
                fixes = ", ".join(package.get("fix_versions", [])) or "нет опубликованной версии"
                lines.append(
                    f"  - `{package.get('name')} {package.get('version')}`: "
                    f"{len(package.get('advisory_ids', []))} advisory; исправления: `{fixes}`"
                )
    npm = report.get("npm")
    if isinstance(npm, dict):
        production = npm.get("production")
        complete = npm.get("complete")
        lines.append(f"- npm: `{npm.get('tool_version')}`")
        if isinstance(production, dict):
            lines.append(
                f"- npm production: total `{production.get('total')}`, high `{production.get('high')}`, critical `{production.get('critical')}`"
            )
        if isinstance(complete, dict):
            lines.append(
                f"- npm complete tree: total `{complete.get('total')}`, moderate `{complete.get('moderate')}`, high `{complete.get('high')}`, critical `{complete.get('critical')}`"
            )
    return "\n".join(lines) + "\n"


def write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and normalize dependency audit JSON.")
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--pip-json", type=Path)
    parser.add_argument("--npm-production-json", type=Path)
    parser.add_argument("--npm-all-json", type=Path)
    parser.add_argument("--pip-audit-version")
    parser.add_argument("--npm-version")
    parser.add_argument("--pip-exit-code", type=int)
    parser.add_argument("--npm-production-exit-code", type=int)
    parser.add_argument("--npm-all-exit-code", type=int)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        report = build_report(
            target_sha=args.target_sha,
            pip_payload=read_json(args.pip_json) if args.pip_json else None,
            npm_production_payload=(
                read_json(args.npm_production_json) if args.npm_production_json else None
            ),
            npm_all_payload=read_json(args.npm_all_json) if args.npm_all_json else None,
            pip_audit_version=args.pip_audit_version,
            npm_version=args.npm_version,
            pip_exit_code=args.pip_exit_code,
            npm_production_exit_code=args.npm_production_exit_code,
            npm_all_exit_code=args.npm_all_exit_code,
        )
        write_report(args.json_output, report)
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_report(report), encoding="utf-8")
    except (OSError, SecurityReportError, json.JSONDecodeError) as exc:
        print(f"security audit report failed: {exc}", file=sys.stderr)
        return 2
    print(
        "security audit report: "
        f"mechanism={report['mechanism_status']} findings={report['findings_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
