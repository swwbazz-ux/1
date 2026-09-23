from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
CANONICAL_BRANCH = "codex/github-production-deploy-20260916"


def workflow_source(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


class CiWorkflowSecurityContractTests(unittest.TestCase):
    def test_nightly_has_no_user_controlled_target_ref(self):
        source = workflow_source("nightly-quality.yml")

        self.assertNotIn("target_ref", source)
        self.assertIn(CANONICAL_BRANCH, source)

        if "pull_request:" in source:
            self.assertIn("github.event_name == 'pull_request'", source)
            self.assertIn("github.sha", source)
        if "push:" in source:
            self.assertIn("github.event_name == 'push'", source)
            self.assertIn("github.sha", source)

    def test_checkout_steps_do_not_persist_credentials(self):
        for workflow_name in ("django.yml", "nightly-quality.yml"):
            with self.subTest(workflow=workflow_name):
                lines = workflow_source(workflow_name).splitlines()
                checkout_indexes = [
                    index
                    for index, line in enumerate(lines)
                    if "uses: actions/checkout@" in line
                ]
                self.assertTrue(checkout_indexes)
                for index in checkout_indexes:
                    checkout_block = "\n".join(lines[index : index + 7])
                    self.assertIn("persist-credentials: false", checkout_block)

    def test_django_workflow_has_read_only_token_permissions(self):
        source = workflow_source("django.yml")
        self.assertIn("permissions:\n  contents: read", source)

    def test_watchdog_cannot_supply_a_target_ref(self):
        watchdog = WORKFLOWS / "nightly-scheduler-watchdog.yml"
        if not watchdog.exists():
            self.skipTest("This branch does not own the scheduler watchdog")

        source = watchdog.read_text(encoding="utf-8")
        self.assertNotIn("target_ref", source)
        self.assertNotIn("NIGHTLY_TARGET", source)


if __name__ == "__main__":
    unittest.main()
