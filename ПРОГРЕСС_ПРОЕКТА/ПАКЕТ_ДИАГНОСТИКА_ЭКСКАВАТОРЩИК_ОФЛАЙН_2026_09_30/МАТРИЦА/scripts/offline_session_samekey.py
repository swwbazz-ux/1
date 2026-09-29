"""Put the release stand's session key into this stand's DB (same employee access 3), so a server swap keeps the login like production's shared DB."""
from datetime import timedelta
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone
from users.active_role import ACTIVE_ROLE_CODE_SESSION_KEY, ACTIVE_ROLE_GENERATION_SESSION_KEY, ACTIVE_ROLE_SESSION_KEY
from users.models import EmployeeAccess

key = open(__import__('os').environ['SAME_KEY_FILE']).read().strip()
access = EmployeeAccess.objects.select_related('role').get(pk=3)
now = timezone.now()
data = {'employee_access_id': access.id, ACTIVE_ROLE_SESSION_KEY: access.id,
        ACTIVE_ROLE_GENERATION_SESSION_KEY: now.isoformat(), ACTIVE_ROLE_CODE_SESSION_KEY: access.role.code}
Session.objects.update_or_create(session_key=key, defaults={'session_data': SessionStore().encode(data), 'expire_date': now + timedelta(days=1)})
access.last_login_at = now
access.save(update_fields=['last_login_at'])
print('same key stored:', Session.objects.filter(session_key=key).exists())
