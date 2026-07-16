from django.conf import settings


class PersonalSessionRenewalMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.session.get('device_kind') == 'personal'
            and request.session.get('employee_access_id')
        ):
            request.session.set_expiry(settings.ROLE_APP_PERSONAL_SESSION_AGE)
        return response
