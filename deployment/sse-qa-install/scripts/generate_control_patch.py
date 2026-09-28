from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


BASELINE = "b222bfdbc0f739ead7d04cfd6e84f45e7f4ca419"
CONTROL_PATHS = (
    Path(".github/deploy/build_release.py"),
    Path(".github/deploy/test_release_protocol.py"),
    Path(".github/workflows/production-deploy.yml"),
    Path("deployment/server/accounting_github_deploy_receiver.py"),
    Path("deployment/server/sse_qa_ctl.py"),
)


def run(command: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, cwd=cwd, check=check, capture_output=True)


def main() -> int:
    package = Path(sys.argv[1]).resolve()
    repo = Path(sys.argv[2]).resolve()
    output = package / "github-actions/control-channel.patch"
    overlay = package / "github-actions/source-overlay"
    with tempfile.TemporaryDirectory(prefix="sse-qa-control-patch-") as raw:
        work = Path(raw)
        run(["git", "init", "-q"], cwd=work)
        run(["git", "config", "user.name", "SSE QA patch builder"], cwd=work)
        run(["git", "config", "user.email", "sse-qa@example.invalid"], cwd=work)
        run(["git", "config", "core.autocrlf", "false"], cwd=work)
        for relative in CONTROL_PATHS:
            probe = run(
                ["git", "cat-file", "-e", f"{BASELINE}:{relative.as_posix()}"],
                cwd=repo,
                check=False,
            )
            if probe.returncode == 0:
                target = work / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(
                    run(
                        ["git", "show", f"{BASELINE}:{relative.as_posix()}"],
                        cwd=repo,
                    ).stdout
                )
        run(["git", "add", "--all"], cwd=work)
        run(["git", "commit", "-qm", "control baseline"], cwd=work)
        for relative in CONTROL_PATHS:
            source = overlay / relative
            if not source.is_file():
                raise SystemExit(f"missing control overlay: {relative.as_posix()}")
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
        run(["git", "add", "--intent-to-add", "--", *[p.as_posix() for p in CONTROL_PATHS]], cwd=work)
        patch = run(
            ["git", "diff", "--binary", "--full-index", "--no-ext-diff", "--"],
            cwd=work,
        ).stdout
        patch = b"\n".join(
            line.rstrip(b" \t") if line.startswith(b"+") and not line.startswith(b"+++") else line
            for line in patch.splitlines()
        ) + b"\n"
        run(["git", "reset", "--hard", "HEAD"], cwd=work)
        candidate = work / "control-channel.patch"
        candidate.write_bytes(patch)
        run(["git", "apply", "--check", str(candidate)], cwd=work)
    if not patch:
        raise SystemExit("empty control patch")
    output.write_bytes(patch)
    print(f"SSE_QA_CONTROL_PATCH_OK bytes={len(patch)} baseline={BASELINE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
