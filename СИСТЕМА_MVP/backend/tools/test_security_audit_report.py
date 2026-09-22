from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import security_audit_report as report


class SecurityAuditReportTests(unittest.TestCase):
    def test_pip_findings_are_deduplicated_per_package(self):
        payload = {
            "dependencies": [
                {
                    "name": "demo",
                    "version": "1.0",
                    "vulns": [
                        {"id": "PYSEC-1", "fix_versions": ["1.1"]},
                        {"id": "PYSEC-1", "fix_versions": ["1.1"]},
                        {"id": "PYSEC-2", "fix_versions": ["1.2"]},
                    ],
                },
                {"name": "clean", "version": "2.0", "vulns": []},
            ]
        }

        findings = report.parse_pip_audit(payload)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].advisory_ids, ("PYSEC-1", "PYSEC-2"))
        self.assertEqual(findings[0].fix_versions, ("1.1", "1.2"))

    def test_npm_totals_require_the_complete_v2_schema(self):
        payload = {
            "auditReportVersion": 2,
            "metadata": {
                "vulnerabilities": {
                    "info": 0,
                    "low": 0,
                    "moderate": 3,
                    "high": 0,
                    "critical": 0,
                    "total": 3,
                }
            },
        }

        totals = report.parse_npm_audit(payload)

        self.assertEqual(totals["moderate"], 3)
        self.assertEqual(totals["total"], 3)

    def test_findings_remain_report_only(self):
        pip_payload = {
            "dependencies": [
                {
                    "name": "demo",
                    "version": "1.0",
                    "vulns": [{"id": "PYSEC-1", "fix_versions": ["1.1"]}],
                }
            ]
        }
        clean_npm = {
            "auditReportVersion": 2,
            "metadata": {
                "vulnerabilities": {
                    "info": 0,
                    "low": 0,
                    "moderate": 0,
                    "high": 0,
                    "critical": 0,
                    "total": 0,
                }
            },
        }

        result = report.build_report(
            target_sha="a" * 40,
            pip_payload=pip_payload,
            npm_production_payload=clean_npm,
            npm_all_payload=clean_npm,
            pip_audit_version="2.10.1",
            npm_version="12.0.2",
        )

        self.assertEqual(result["mechanism_status"], "success")
        self.assertEqual(result["findings_status"], "findings")
        self.assertEqual(result["python"]["unique_advisories"], 1)

    def test_tool_exit_must_agree_with_findings(self):
        with self.assertRaisesRegex(report.SecurityReportError, "clean exit with findings"):
            report.validate_tool_exit("scanner", 0, 1)
        with self.assertRaisesRegex(report.SecurityReportError, "without findings"):
            report.validate_tool_exit("scanner", 1, 0)

    def test_error_object_is_an_infrastructure_failure(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "error.json"
        path.write_text('{"error": {"code": "EAUDIT"}}', encoding="utf-8")

        with self.assertRaisesRegex(report.SecurityReportError, "error object"):
            report.read_json(path)

    def test_missing_npm_severity_is_rejected(self):
        payload = {
            "auditReportVersion": 2,
            "metadata": {"vulnerabilities": {"total": 0}},
        }

        with self.assertRaisesRegex(report.SecurityReportError, "count is invalid"):
            report.parse_npm_audit(payload)


if __name__ == "__main__":
    unittest.main()
