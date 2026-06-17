from django.contrib import messages
from django.shortcuts import redirect, render

from users.models import EmployeeAccess

from .forms import HaulAssignmentForm
from .models import HaulAssignment


def mining_master_assignments_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code not in {'mining_master', 'dispatcher', 'admin'}:
        return redirect('role_home')

    if request.method == 'POST':
        form = HaulAssignmentForm(request.POST)
        if form.is_valid():
            assignment = form.save(commit=False)
            assignment.assigned_by = access.employee
            assignment.save()
            messages.success(request, 'Назначение создано. Водитель должен подтвердить его кнопкой Принял.')
            return redirect('mining_master_assignments')
    else:
        form = HaulAssignmentForm()

    assignments = HaulAssignment.objects.select_related('truck', 'excavator').order_by('-assigned_at')[:20]
    return render(
        request,
        'assignments/mining_master_assignments.html',
        {
            'access': access,
            'form': form,
            'assignments': assignments,
        },
    )
