from .role_apps import get_role_app_for_request


def role_app(request):
    app = get_role_app_for_request(request)
    return {
        'role_app': app,
        'role_app_isolated': app is not None,
        'role_app_pwa_scope': '/' if app else '',
    }
