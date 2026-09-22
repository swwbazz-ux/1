from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import quality_diff_gate as gate


class TemporaryGitRepository(unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git("init", "-q")
        self.git("config", "user.email", "quality@example.invalid")
        self.git("config", "user.name", "Quality Test")

    def git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if completed.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {completed.stderr}")
        return completed.stdout.strip()

    def write(self, path: str, content: str, *, crlf: bool = False) -> None:
        target = self.repo / Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        newline = "\r\n" if crlf else "\n"
        target.write_bytes(content.replace("\n", newline).encode("utf-8"))

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


class QualityDiffParsingTests(unittest.TestCase):
    def test_name_status_z_preserves_unicode_paths_and_rename(self):
        payload = ("M\0обычный файл.py\0R100\0старый.py\0новый файл.py\0D\0удалён.py\0").encode(
            "utf-8"
        )

        entries = gate.parse_name_status_z(payload)

        self.assertEqual(entries[0].path, "обычный файл.py")
        self.assertEqual(entries[1].status, "R")
        self.assertEqual(entries[1].old_path, "старый.py")
        self.assertEqual(entries[1].path, "новый файл.py")
        self.assertEqual(entries[2].status, "D")

    def test_name_status_rejects_parent_path(self):
        with self.assertRaisesRegex(gate.QualityGateError, "unsafe Git path"):
            gate.parse_name_status_z(b"A\0../outside.py\0")

    def test_added_lines_support_multiple_hunks_and_deletion_only_hunk(self):
        patch_text = "\n".join(
            [
                "@@ -1,0 +2,2 @@",
                "+one",
                "+two",
                "@@ -8,2 +10,0 @@",
                "-old",
                "@@ -20 +20 @@",
                "-before",
                "+after",
            ]
        )

        self.assertEqual(gate.parse_added_lines(patch_text), frozenset({2, 3, 20}))

    def test_multiline_diagnostic_intersects_added_range(self):
        diagnostic = {
            "location": {"row": 8, "column": 1},
            "end_location": {"row": 11, "column": 2},
        }

        self.assertTrue(gate.diagnostic_intersects(diagnostic, frozenset({10})))
        self.assertFalse(gate.diagnostic_intersects(diagnostic, frozenset({12})))

    def test_exclusions_are_component_based_not_name_substrings(self):
        self.assertTrue(gate.is_excluded_path("app/migrations/0001.py"))
        self.assertTrue(gate.is_excluded_path("vendor/tool.py"))
        self.assertTrue(gate.is_excluded_path("generated/schema.py"))
        self.assertFalse(gate.is_excluded_path("app/quality_qa_helper.py"))

    def test_report_only_and_enforce_exit_codes_differ(self):
        finding = gate.Finding(
            path="demo.py",
            line=1,
            column=1,
            code="F821",
            message="undefined name",
            kind="lint",
            scope="added-line",
            blocking=True,
        )

        self.assertEqual(gate.result_exit_code(report_only=True, findings=[finding]), 0)
        self.assertEqual(gate.result_exit_code(report_only=False, findings=[finding]), 1)
        self.assertEqual(gate.result_exit_code(report_only=False, findings=[]), 0)

    def test_github_annotation_escaping_is_stable(self):
        self.assertEqual(
            gate.github_escape("a:b,c%", property_value=True),
            "a%3Ab%2Cc%25",
        )


class QualityDiffGitIntegrationTests(TemporaryGitRepository):
    def test_collect_changes_classifies_new_modified_deleted_and_rename(self):
        self.write("keep.py", "value = 1\n")
        self.write("old.py", "\n".join(f"value_{i} = {i}" for i in range(12)) + "\n")
        self.write("delete.py", "gone = True\n")
        self.write("app/migrations/0001.py", "legacy = True\n")
        base = self.commit("base")

        self.write("keep.py", "value = 1\nnext_value = 2\n")
        self.git("mv", "old.py", "новый файл.py")
        with (self.repo / "новый файл.py").open("a", encoding="utf-8") as handle:
            handle.write("added = True\n")
        (self.repo / "delete.py").unlink()
        self.write("brand_new.py", "new_value = 1\n")
        self.write("app/migrations/0001.py", "legacy = True\nchanged = True\n")
        head = self.commit("changes")

        entries, changes, excluded = gate.collect_changes(
            self.repo,
            effective_base=base,
            head_sha=head,
        )

        by_path = {change.path: change for change in changes}
        self.assertIn("keep.py", by_path)
        self.assertEqual(by_path["keep.py"].added_lines, frozenset({2}))
        self.assertFalse(by_path["новый файл.py"].is_new)
        self.assertIn(13, by_path["новый файл.py"].added_lines)
        self.assertTrue(by_path["brand_new.py"].is_new)
        self.assertIn("delete.py", {entry.path for entry in excluded})
        self.assertIn("app/migrations/0001.py", {entry.path for entry in excluded})
        self.assertGreaterEqual(len(entries), 5)

    def test_crlf_does_not_shift_added_line_number(self):
        self.write("windows.py", "first = 1\nthird = 3\n", crlf=True)
        base = self.commit("base")
        self.write("windows.py", "first = 1\nsecond = 2\nthird = 3\n", crlf=True)
        head = self.commit("insert")

        _, changes, _ = gate.collect_changes(
            self.repo,
            effective_base=base,
            head_sha=head,
        )

        self.assertEqual(changes[0].added_lines, frozenset({2}))

    def test_rename_from_excluded_path_is_new_file(self):
        self.write("vendor/legacy.py", "legacy = True\n")
        base = self.commit("base")
        (self.repo / "app").mkdir()
        self.git("mv", "vendor/legacy.py", "app/current.py")
        head = self.commit("move into scope")

        _, changes, _ = gate.collect_changes(
            self.repo,
            effective_base=base,
            head_sha=head,
        )

        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].is_new)

    def test_merge_base_and_direct_modes_resolve_different_bases(self):
        self.write("base.py", "base = True\n")
        common = self.commit("common")
        self.git("branch", "feature", common)
        self.write("main.py", "main = True\n")
        main_head = self.commit("main")
        self.git("checkout", "-q", "feature")
        self.write("feature.py", "feature = True\n")
        feature_head = self.commit("feature")

        direct, direct_effective = gate.resolve_diff_base(
            self.repo,
            base=main_head,
            head_sha=feature_head,
            diff_mode="direct",
        )
        merge_base, merge_effective = gate.resolve_diff_base(
            self.repo,
            base=main_head,
            head_sha=feature_head,
            diff_mode="merge-base",
        )

        self.assertEqual(direct, main_head)
        self.assertEqual(direct_effective, main_head)
        self.assertEqual(merge_base, main_head)
        self.assertEqual(merge_effective, common)

    def test_zero_direct_base_uses_head_parent(self):
        self.write("base.py", "base = True\n")
        base = self.commit("base")
        self.write("head.py", "head = True\n")
        head = self.commit("head")

        resolved = gate.resolve_direct_base(self.repo, "0" * 40, head)

        self.assertEqual(resolved, base)

    def test_dirty_and_untracked_worktree_is_rejected(self):
        self.write("clean.py", "clean = True\n")
        head = self.commit("clean")
        self.write("untracked.py", "dirty = True\n")

        with self.assertRaisesRegex(gate.QualityGateError, "clean worktree"):
            gate.ensure_exact_clean_checkout(self.repo, head)


class QualityDiffFindingTests(unittest.TestCase):
    def test_existing_file_filters_legacy_diagnostic(self):
        change = gate.PythonChange(
            status="M",
            old_path=None,
            path="demo.py",
            scope="added-line",
            added_lines=frozenset({10}),
        )
        diagnostics = [
            {
                "filename": "demo.py",
                "code": "F821",
                "message": "legacy",
                "location": {"row": 2, "column": 1},
                "end_location": {"row": 2, "column": 2},
            },
            {
                "filename": "demo.py",
                "code": "F821",
                "message": "new",
                "location": {"row": 9, "column": 1},
                "end_location": {"row": 10, "column": 2},
            },
        ]

        with patch.object(gate, "invoke_ruff_json", return_value=diagnostics):
            findings = gate.ruff_findings(
                Path.cwd(),
                config_path=Path("pyproject.toml"),
                changes=[change],
            )

        self.assertEqual([finding.message for finding in findings], ["new"])

    def test_new_file_keeps_every_diagnostic(self):
        change = gate.PythonChange(
            status="A",
            old_path=None,
            path="demo.py",
            scope="new-file",
            added_lines=frozenset(),
        )
        diagnostic = {
            "filename": "demo.py",
            "code": "F401",
            "message": "unused import",
            "location": {"row": 1, "column": 1},
            "end_location": {"row": 1, "column": 7},
        }

        with patch.object(gate, "invoke_ruff_json", return_value=[diagnostic]):
            findings = gate.ruff_findings(
                Path.cwd(),
                config_path=Path("pyproject.toml"),
                changes=[change],
            )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].scope, "new-file")

    def test_size_warnings_only_apply_to_new_production_code(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        production_path = "СИСТЕМА_MVP/backend/demo/service.py"
        target = root / Path(production_path)
        target.parent.mkdir(parents=True)
        body = "\n".join(["def huge():", *["    value = 1"] * 101]) + "\n"
        target.write_text(body, encoding="utf-8")
        change = gate.PythonChange(
            status="A",
            old_path=None,
            path=production_path,
            scope="new-file",
            added_lines=frozenset(),
        )

        findings = gate.size_findings(root, [change])

        self.assertEqual([finding.code for finding in findings], ["FUNC100"])
        self.assertFalse(findings[0].blocking)


if __name__ == "__main__":
    unittest.main()
