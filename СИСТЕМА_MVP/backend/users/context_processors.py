from .active_role import role_session_state
from .app_catalog import app_catalog_public_url
from .role_apps import (
    APP_CONTRACT_VERSION,
    STATIC_ASSET_RELEASE,
    get_role_app,
    get_role_app_for_path,
    get_role_app_for_request,
    role_app_scope,
)


def role_app(request):
    host_app = get_role_app_for_request(request)
    pending_activation_app = None
    resolver_match = getattr(request, 'resolver_match', None)
    if resolver_match and resolver_match.url_name == 'activate_access':
        pending_activation_app = get_role_app(
            request.session.get('pending_activation_target_app_code', '')
        )
    path_app = pending_activation_app or get_role_app_for_path(request.path)
    app = host_app
    if (
        host_app
        and path_app
        and host_app.role_code != path_app.role_code
    ):
        app = None
    metadata_app = path_app or host_app
    metadata_scope = (
        role_app_scope(request, metadata_app.role_code)
        if metadata_app
        else ''
    )
    state = getattr(request, 'role_session_state', None) or role_session_state(request)
    return {
        'role_app': app,
        'role_app_isolated': app is not None,
        'role_app_pwa_scope': metadata_scope if app else '',
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
        'app_service_worker_scope': metadata_scope,
        'app_catalog_url': app_catalog_public_url(request),
    }
