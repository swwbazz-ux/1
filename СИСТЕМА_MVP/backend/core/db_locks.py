from django.db import connection


def lock_idempotency_key(action_type, client_action_id):
    """Serialize one client action key for the lifetime of the transaction."""
    if not action_type or not client_action_id:
        return
    if connection.vendor != 'postgresql':
        return
    if not connection.in_atomic_block:
        raise RuntimeError('Idempotency advisory lock requires transaction.atomic().')
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))',
            [str(action_type), str(client_action_id)],
        )
