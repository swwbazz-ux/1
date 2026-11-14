from functools import wraps

from django.db import connection, transaction


def operational_snapshot_view(view):
    """Render an opted-in operational fragment from one MVCC snapshot.

    PostgreSQL's default READ COMMITTED takes a new snapshot per statement and
    therefore cannot bind a multi-query HTML fragment to its returned version.
    The pilot asks explicitly for ``_operational_snapshot=1``; legacy fragment
    requests keep their existing behaviour.
    """

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        is_snapshot = (
            request.method == 'GET'
            and request.GET.get('_operational_snapshot', '').strip() == '1'
            and request.GET.get('_operational_fragment', '').strip()
            in {'driver', 'excavator'}
        )
        if not is_snapshot:
            return view(request, *args, **kwargs)
        with transaction.atomic():
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            response = view(request, *args, **kwargs)
            response['X-Operational-Snapshot'] = 'repeatable-read-v1'
            return response

    return wrapped
