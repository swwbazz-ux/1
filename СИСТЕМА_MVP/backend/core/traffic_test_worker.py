import os
from pathlib import Path
import time


def run_reconcile_server_gate_worker(lock_path, result_path, ready_queue, start_event):
    import django

    django.setup()
    from assignments import services

    services.RECONCILE_LOCK_PATH = Path(lock_path)
    services.RECONCILE_INTERVAL_SECONDS = 5
    services._reconcile_next_check = 0

    def record_reconcile():
        with Path(result_path).open('a', encoding='utf-8') as result:
            result.write(f'{os.getpid()}\n')
            result.flush()
        time.sleep(0.5)
        return 0

    services.reconcile_due_haul_assignments = record_reconcile
    ready_queue.put('ready')
    if not start_event.wait(timeout=10):
        raise RuntimeError('reconcile process gate start timeout')
    services.reconcile_due_haul_assignments_throttled()
