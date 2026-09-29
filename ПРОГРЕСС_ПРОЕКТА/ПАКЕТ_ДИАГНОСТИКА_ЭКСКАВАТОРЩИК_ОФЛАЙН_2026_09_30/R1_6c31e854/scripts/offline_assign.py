from assignments.services import get_active_equipment_assignment, set_active_equipment_assignment
from references.models import Equipment
from trips.views import excavator_fuel_capacity_l
from users.models import EmployeeAccess

access = EmployeeAccess.objects.select_related('employee', 'role').get(pk=3)
excavator = Equipment.objects.get(pk=54)
set_active_equipment_assignment(
    employee=access.employee, role=access.role, equipment=excavator, shift_type='day',
)
work = get_active_equipment_assignment(access.employee, 'excavator_operator')
print('work assignment ->', work.equipment_id if work else None, 'fuel capacity', excavator_fuel_capacity_l(excavator))
