from .role_apps import role_app_manifest_response, role_app_service_worker_response


def oup_manifest_view(request):
    return role_app_manifest_response(request, 'oup')


def oup_service_worker_view(request):
    return role_app_service_worker_response(request, 'oup')


def mechanic_manifest_view(request):
    return role_app_manifest_response(request, 'mechanic')


def mechanic_service_worker_view(request):
    return role_app_service_worker_response(request, 'mechanic')


def management_manifest_view(request):
    return role_app_manifest_response(request, 'manager')


def management_service_worker_view(request):
    return role_app_service_worker_response(request, 'manager')


def system_admin_manifest_view(request):
    return role_app_manifest_response(request, 'admin')


def system_admin_service_worker_view(request):
    return role_app_service_worker_response(request, 'admin')
