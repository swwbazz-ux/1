from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone

from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from users.models import EmployeeAccess

access = EmployeeAccess.objects.select_related('role').get(pk=3)
now = timezone.now()
session = SessionStore()
session['employee_access_id'] = access.id
session[ACTIVE_ROLE_SESSION_KEY] = access.id
session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = now.isoformat()
session[ACTIVE_ROLE_CODE_SESSION_KEY] = access.role.code
session.save()
access.last_login_at = now
access.save(update_fields=['last_login_at'])
print('SESSION_KEY=' + session.session_key)
