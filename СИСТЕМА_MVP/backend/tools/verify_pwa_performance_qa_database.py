#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from core.pwa_performance_qa import (  # noqa: E402
    PwaPerformanceQaError,
    verify_pwa_performance_qa_database,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not settings.DEBUG:
        raise PwaPerformanceQaError('QA preflight требует DEBUG=True.')
    if not getattr(settings, 'PWA_TRAFFIC_QA_PREFLIGHT_ENABLED', False):
        raise PwaPerformanceQaError('QA preflight flag выключен.')
    configured_run_id = str(
        getattr(settings, 'PWA_TRAFFIC_QA_RUN_ID', '') or ''
    ).strip()
    if args.run_id != configured_run_id:
        raise PwaPerformanceQaError('QA run id не совпадает с настройкой.')
    payload = verify_pwa_performance_qa_database(configured_run_id)
    print(f"PWA_PERFORMANCE_QA_PREFLIGHT_OK {payload['fingerprint']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
