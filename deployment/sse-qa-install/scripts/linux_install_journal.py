from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable, Sequence


INSTALL_UNIT = "sse-qa-install.service"
INVOCATION_RE = re.compile(r"[0-9a-fA-F]{32}")
OUTPUT_FORMATS = {"cat", "short-iso-precise"}


def collect_invocation_journal(
    invocation_id: str,
    output_format: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    if not INVOCATION_RE.fullmatch(invocation_id):
        raise ValueError("invalid systemd InvocationID")
    if output_format not in OUTPUT_FORMATS:
        raise ValueError("invalid journal output format")
    run(
        ["/usr/bin/journalctl", "--sync"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    result = run(
        [
            "/usr/bin/journalctl",
            "--no-pager",
            "-o",
            output_format,
            "-u",
            INSTALL_UNIT,
            f"_SYSTEMD_INVOCATION_ID={invocation_id.lower()}",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--invocation", required=True)
    parser.add_argument("--output", required=True, choices=sorted(OUTPUT_FORMATS))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        collect_invocation_journal(args.invocation, args.output),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
