from .active_role import role_session_state
from .role_apps import (
    APP_CONTRACT_VERSION,
    STATIC_ASSET_RELEASE,
    get_role_app_for_path,
    get_role_app_for_request,
)


def role_app(request):
    app = get_role_app_for_request(request)
    metadata_app = app or get_role_app_for_path(request.path)
    state = getattr(request, 'role_session_state', None) or role_session_state(request)
    return {
        'role_app': app,
        'role_app_isolated': app is not None,
        'role_app_pwa_scope': '/' if app else '',
        'role_access_is_active': state['is_active'],
        'active_role_code': state.get('active_role_code', ''),
        'active_role_changed_at': state.get('active_role_changed_at'),
        'app_contract_version': APP_CONTRACT_VERSION,
        'static_asset_release': STATIC_ASSET_RELEASE,
        'app_shell_version': metadata_app.shell_version if metadata_app else '',
        'app_role_code': metadata_app.role_code if metadata_app else '',
        'app_service_worker_url': (
            metadata_app.service_worker_url if metadata_app else ''
        ),
        'app_service_worker_scope': (
            '/' if app else metadata_app.legacy_scope if metadata_app else ''
        ),
    }
