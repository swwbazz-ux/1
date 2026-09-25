"""Команды управления назначениями с Диспетчерского пульта."""

from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from assignments.services import HaulAssignmentStateConflict, schedule_haul_release
from core.models import lock_production_state
from users.models import EmployeeAccess

from .dispatcher_guards import (
    dispatcher_shift_required_redirect,
    get_dispatcher_control_url,
)
from .models import DispatcherActionType


def execute_dispatcher_cancel_assignment(
    request,
    assignment_id,
    *,
    lock_mutation_access,
    action_logger,
):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True)
        .first()
    )
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    access = lock_mutation_access(request, access)
    if not access:
        messages.error(request, 'Роль неактивна — доступен только просмотр.')
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    lock_production_state()
    reason = request.POST.get('reason', '').strip()

    assignment = (
        HaulAssignment.objects
        .select_for_update(of=('self',))
        .select_related('truck', 'excavator')
        .filter(
            id=assignment_id,
            ended_at__isnull=True,
            status__in={AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED},
        )
        .first()
    )
    if not assignment:
        messages.error(request, 'Активное назначение для отмены не найдено.')
        return redirect(redirect_url)

    if assignment.status == AssignmentStatus.ACCEPTED:
        try:
            pending_release, _ = schedule_haul_release(
                truck=assignment.truck,
                assigned_by=access.employee,
                now=timezone.now(),
                expected_state_id=assignment.id,
            )
        except HaulAssignmentStateConflict:
            messages.error(
                request,
                'Назначение уже изменилось. Обновите пульт и повторите действие.',
            )
            return redirect(redirect_url)
        logged_assignment = pending_release or assignment
    else:
        assignment.status = AssignmentStatus.CANCELLED
        assignment.ended_at = timezone.now()
        assignment.save(update_fields=['status', 'ended_at'])
        logged_assignment = assignment
    action_logger(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        haul_assignment=logged_assignment,
        target_summary=f'{assignment.truck} под {assignment.excavator}',
        reason=reason,
    )
    messages.success(
        request,
        f'Назначение {assignment.truck} под {assignment.excavator} отменено.',
    )
    return redirect(redirect_url)
