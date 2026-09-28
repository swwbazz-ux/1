"""Copy the three named release regressions into an isolated PR #106 worktree.

The copied method/test bodies are byte-for-byte slices of the release sources.
The script intentionally does not adapt renamed helpers or production code: an
incompatibility is evidence and must remain visible in the raw run.
"""

import argparse
import re
from pathlib import Path


BACKEND_TEST = Path('СИСТЕМА_MVP/backend/core/test_free_bucket_sync.py')
DRIVER_TEST = Path('СИСТЕМА_MVP/backend/static/js/tests/driver-offline-outbox-v2.test.js')


def method_block(text, name):
    pattern = re.compile(
        rf'^    def {re.escape(name)}\(.*?(?=^    def |^@override_settings|^class |\Z)',
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise RuntimeError(f'method not found: {name}')
    return match.group(0).rstrip() + '\n\n'


def js_test_block(text, title):
    start_marker = f'test("{title}", async () => {{'
    start = text.index(start_marker)
    next_start = text.index('\ntest("', start + len(start_marker))
    return text[start:next_start].rstrip() + '\n\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('release_root', type=Path)
    parser.add_argument('pr106_root', type=Path)
    args = parser.parse_args()

    release_backend_path = args.release_root / BACKEND_TEST
    target_backend_path = args.pr106_root / BACKEND_TEST
    release_backend = release_backend_path.read_text(encoding='utf-8')
    target_backend = target_backend_path.read_text(encoding='utf-8')
    names = [
        'backdate_truck_shift',
        'driver_manual_load_under_bucket',
        'driver_manual_complete_under_bucket',
        '_expired_bucket_load_pair',
        'test_manual_load_under_bucket_cancelled_by_server_ttl_is_recorded_by_fact',
        'test_already_rejected_cancelled_bucket_load_is_accepted_on_resend',
    ]
    blocks = ''.join(method_block(release_backend, name) for name in names).rstrip()
    anchor = (
        '\n\n@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)\n'
        'class FreeBucketPostgreSQLConcurrencyTests'
    )
    if anchor not in target_backend:
        raise RuntimeError('PR backend insertion anchor not found')
    target_backend = target_backend.replace(anchor, f'\n\n{blocks}{anchor}', 1)
    target_backend_path.write_text(target_backend, encoding='utf-8', newline='\n')

    release_driver_path = args.release_root / DRIVER_TEST
    target_driver_path = args.pr106_root / DRIVER_TEST
    release_driver = release_driver_path.read_text(encoding='utf-8')
    target_driver = target_driver_path.read_text(encoding='utf-8')
    js_block = js_test_block(
        release_driver,
        'restart resends a manual load refused as free_bucket_not_available and its chain',
    ).rstrip()
    js_anchor = '\ntest("restart never retries a real domain conflict", async () => {'
    if js_anchor not in target_driver:
        raise RuntimeError('PR JS insertion anchor not found')
    target_driver = target_driver.replace(js_anchor, f'\n{js_block}\n{js_anchor}', 1)
    target_driver_path.write_text(target_driver, encoding='utf-8', newline='\n')

    print('overlay=PASS')
    print(f'release_backend={release_backend_path}')
    print(f'pr106_backend={target_backend_path}')
    print(f'release_driver={release_driver_path}')
    print(f'pr106_driver={target_driver_path}')
    print('copied_backend_methods=' + ','.join(names))
    print('copied_js_test=restart resends a manual load refused as free_bucket_not_available and its chain')


if __name__ == '__main__':
    main()
