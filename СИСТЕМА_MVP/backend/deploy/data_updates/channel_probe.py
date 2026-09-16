#!/usr/bin/env python3
"""Harmless production data-channel probe: validates orchestration without changing data."""

from __future__ import annotations

import argparse
import json


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "operation": "github_data_channel_probe",
                "mode": "apply" if args.apply else "dry_run",
                "changes": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
