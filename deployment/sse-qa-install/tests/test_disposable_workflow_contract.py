from __future__ import annotations

import re
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github/workflows/sse-qa-disposable-linux.yml"
PACKAGED_WORKFLOW = PACKAGE_ROOT / "github-actions/sse-qa-disposable-linux.yml"


class DisposableWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_review_package_contains_exact_workflow_copy(self):
        self.assertEqual(WORKFLOW.read_bytes(), PACKAGED_WORKFLOW.read_bytes())

    def test_manual_only_least_privilege_and_pinned_actions(self):
        self.assertRegex(self.text, r"(?m)^on:\n  workflow_dispatch:\n")
        for forbidden_trigger in ("push:", "pull_request:", "schedule:", "repository_dispatch:"):
            self.assertNotIn(forbidden_trigger, self.text)
        self.assertIn("permissions:\n  contents: read", self.text)
        self.assertNotIn("environment:", self.text)
        self.assertNotIn("${{ secrets.", self.text)
        self.assertRegex(
            self.text,
            r"uses: actions/checkout@[0-9a-f]{40}(?:[^\n]*)\n(?:.*\n)*?\s+persist-credentials: false",
        )
        self.assertNotIn("actions/upload-artifact", self.text)

    def test_event_sha_and_no_paid_capacity_are_explicit_gates(self):
        for required in (
            '[[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]]',
            'test "$actual_sha" = "$GITHUB_SHA"',
            "DISPOSABLE_SSE_QA_ONLY",
            "AVAILABLE_ACTIONS_QUOTA_CONFIRMED",
            "runs-on: ubuntu-24.04",
        ):
            self.assertIn(required, self.text)
        self.assertNotIn("inputs.source_sha", self.text)
        self.assertNotIn("REQUESTED_SHA", self.text)
        checkout_block = self.text.split("- name: Checkout exact reviewed SHA", 1)[1].split(
            "- name: Prove exact source", 1
        )[0]
        self.assertNotIn("ref:", checkout_block)

    def test_no_production_transport_or_external_credentials(self):
        lower = self.text.lower()
        for forbidden in (
            "production-deploy.yml",
            "accounting_github_deploy_receiver",
            "ssh-action",
            "ssh-key",
            "ssh_private",
            "firebase",
            "fcm_service_account",
        ):
            self.assertNotIn(forbidden, lower)
        self.assertIn("127.0.0.1 sse-qa.driverform.ru driverform.ru", self.text)
        self.assertIn("allow_cidr:\"127.0.0.1/32\"", self.text)
        self.assertIn("/run/sse-qa-disposable-secrets.json", self.text)
        self.assertIn("/run/sse-qa-network-smoke.json", self.text)
        self.assertIn("chmod 0600", self.text)
        self.assertIn("htpasswd -nbBC 12", self.text)

    def test_capabilities_fail_closed_as_infra_blocked_not_run(self):
        for required in (
            "systemd_not_pid1",
            "cgroup_v2_missing",
            "cgroup_${controller}_missing",
            "loop_control_missing",
            "python_3_12_missing",
            "postgresql_16_missing",
            "redis_7_missing",
            "disk_below_8_gib",
            "memory_below_3_gib",
            "no_free_loop_device",
            "transient_systemd_cgroup_mismatch",
            "port_${port}_busy",
            "RESULT=INFRA_BLOCKED/NOT_RUN",
            "exit 78",
        ):
            self.assertIn(required, self.text)

    def test_real_cycle_evidence_and_cleanup_are_mandatory(self):
        for required in (
            "scripts/linux_disposable_cycle.sh",
            "SSE_QA_DISPOSABLE_CYCLE_OK normal=1 faults=2 cancel=1 zero_residue=4 production_access=0 load_clients=0",
            "SSE_QA_BUSINESS_SMOKE_OK",
            "CPUQuotaPerSecUSec",
            "memory.current memory.events pids.current io.stat",
            "if: always()",
            "ZERO_RESIDUE_OK",
            "SSE_QA_EVIDENCE_BEGIN",
            "SSE_QA_EVIDENCE_MANIFEST_BEGIN",
            "SHA256SUMS",
            "ip6tables -I OUTPUT 1 ! -o lo",
            "scripts/linux_zero_residue_scan.sh",
            "codex-disposable-validation.service",
            "--property=KillMode=control-group",
            "--no-block --collect --service-type=exec",
            "systemctl kill --signal=SIGKILL --kill-who=all",
        ):
            self.assertIn(required, self.text)
        self.assertNotIn("sleep 2", self.text)

    def test_scope_excludes_load_and_external_deploy(self):
        lower = self.text.lower()
        self.assertNotRegex(lower, r"\b(k6|locust|firebase|deploy production)\b")
        self.assertNotRegex(lower, r"(?:clients?|connections?)\s*[=:]\s*(?:80|96)\b")

    def test_cycle_script_has_phase_aware_cancel_and_per_scenario_scans(self):
        script = (PACKAGE_ROOT / "scripts/linux_disposable_cycle.sh").read_text(encoding="utf-8")
        self.assertNotIn("sleep 2", script)
        for required in (
            "MainPID",
            "OWNERSHIP.json",
            "ControlGroup",
            "ZERO_SCAN",
            "normal-zero-residue",
            "fault-$point-zero-residue",
            "cancel-zero-residue",
            "normal",
            "after_image_before_marker",
            "after_postgres_redis_start",
            "cancel",
        ):
            self.assertIn(required, script)


if __name__ == "__main__":
    unittest.main()
