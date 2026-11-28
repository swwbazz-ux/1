from django import template

from users.active_role import active_access_for_employee_role


register = template.Library()


@register.simple_tag
def deputy_header_identity(request, access):
    """Choose the viewer shown in the header without changing workplace permissions."""
    observer = getattr(request, 'observer_actor_access', None)
    if observer and getattr(request, 'observer_mode', False):
        return observer
    if not access or access.role.code != 'deputy_mining_manager':
        return access
    return active_access_for_employee_role(access.employee, 'admin') or access
