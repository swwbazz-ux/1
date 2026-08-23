from __future__ import annotations

from django.conf import settings
from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache

from .pwa_performance_qa import (
    pwa_performance_qa_request_gate,
    verify_pwa_performance_qa_database,
)


def _secure_json(payload, *, status):
    response = JsonResponse(payload, status=status)
    response['Cache-Control'] = 'private, no-store'
    response['Pragma'] = 'no-cache'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@never_cache
def pwa_performance_qa_preflight_view(request):
    """Prove that the actual loopback server uses the isolated QA DB."""

    if not pwa_performance_qa_request_gate(request):
        raise Http404
    run_id = str(settings.PWA_TRAFFIC_QA_RUN_ID).strip()
    try:
        payload = verify_pwa_performance_qa_database(run_id)
    except Exception:
        return _secure_json(
            {'status': 'unavailable'},
            status=503,
        )
    return _secure_json(
        {'status': 'ok', **payload},
        status=200,
    )
