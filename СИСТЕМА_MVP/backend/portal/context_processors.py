from .auth import get_portal_employee, is_system_admin, staff_capabilities


def portal_context(request):
    employee = get_portal_employee(request)
    if not employee:
        return {
            'portal_can_manage': False,
            'portal_can_assign_permissions': False,
        }
    capabilities = staff_capabilities(employee)
    return {
        'portal_session_employee': employee,
        'portal_capabilities': capabilities,
        'portal_can_manage': capabilities['is_staff'],
        'portal_can_assign_permissions': is_system_admin(employee),
    }
