import mimetypes
from decimal import Decimal

from django.http import FileResponse, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from shifts.models import AchievementPrize, AchievementUnlock, EmployeeShift
from shifts.services import calculate_open_shift_progress
from users.models import EmployeeAccess


def format_achievement_percent(value):
    if value is None:
        return 0
    try:
        return int(max(Decimal('0'), Decimal(value)).quantize(Decimal('1')))
    except Exception:
        return 0


def achievement_access_from_request(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            id=access_id,
            is_active=True,
            employee__is_active=True,
            role__is_active=True,
        )
        .exclude(status__in=[EmployeeAccess.Status.BLOCKED, EmployeeAccess.Status.DEACTIVATED])
        .first()
    )
    if not access or access.role.code not in {'driver', 'excavator_operator'}:
        return None
    return access


def achievement_current_shift(access):
    if not access:
        return None
    return (
        EmployeeShift.objects
        .filter(employee=access.employee, closed_at__isnull=True)
        .select_related('equipment', 'equipment__equipment_type', 'plan_group')
        .order_by('-opened_at')
        .first()
    )


def achievement_progress_payload(shift):
    progress = calculate_open_shift_progress(shift) if shift else None
    raw_percent = progress.get('progress_percent') if progress else None
    display_percent = format_achievement_percent(raw_percent)
    decimal_percent = Decimal(raw_percent or 0).quantize(Decimal('0.1'))
    return progress, display_percent, decimal_percent


def achievement_active_prize():
    return AchievementPrize.objects.filter(is_active=True).exclude(image='').order_by('-updated_at', '-id').first()


def achievement_unlock_payload(unlock, percent):
    return {
        'unlocked': True,
        'unlock_id': unlock.id,
        'title': unlock.prize.title,
        'percent': percent,
        'shown': bool(unlock.shown_at),
        'image_url': reverse('achievement_prize_image', args=[unlock.id]),
        'download_url': reverse('achievement_prize_download', args=[unlock.id]),
    }


def achievement_unlock_is_allowed(access, unlock):
    if not access or unlock.user_id != access.employee_id:
        return False
    if unlock.employee_shift.employee_id != access.employee_id:
        return False
    if not unlock.equipment_id or unlock.equipment_id != unlock.employee_shift.equipment_id:
        return False
    progress, _display_percent, decimal_percent = achievement_progress_payload(unlock.employee_shift)
    return bool(progress and decimal_percent >= Decimal('100.0') and unlock.percent_at_unlock >= Decimal('100.0'))


@require_GET
def achievement_current_view(request):
    access = achievement_access_from_request(request)
    if not access:
        return JsonResponse({'unlocked': False, 'percent': 0, 'error': 'Нет доступа.'}, status=403)

    shift = achievement_current_shift(access)
    if not shift or not shift.equipment_id:
        return JsonResponse({'unlocked': False, 'percent': 0})

    _progress, percent, decimal_percent = achievement_progress_payload(shift)
    if decimal_percent < Decimal('100.0'):
        return JsonResponse({'unlocked': False, 'percent': percent})

    prize = achievement_active_prize()
    if not prize:
        return JsonResponse({'unlocked': False, 'percent': percent})

    unlock, created = AchievementUnlock.objects.get_or_create(
        user=access.employee,
        equipment=shift.equipment,
        employee_shift=shift,
        prize=prize,
        defaults={'percent_at_unlock': decimal_percent},
    )
    if not created and decimal_percent > unlock.percent_at_unlock:
        unlock.percent_at_unlock = decimal_percent
        unlock.save(update_fields=['percent_at_unlock'])
    return JsonResponse(achievement_unlock_payload(unlock, percent))


def achievement_prize_file_response(request, unlock_id, *, download=False):
    access = achievement_access_from_request(request)
    if not access:
        return HttpResponseForbidden('Нет доступа.')
    unlock = get_object_or_404(
        AchievementUnlock.objects.select_related('user', 'equipment', 'employee_shift', 'prize'),
        id=unlock_id,
    )
    if not achievement_unlock_is_allowed(access, unlock):
        return HttpResponseForbidden('Приз недоступен.')
    if not unlock.prize.image:
        return HttpResponse(status=404)
    filename = unlock.prize.image.name.rsplit('/', 1)[-1] or 'achievement-prize'
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    response = FileResponse(
        unlock.prize.image.open('rb'),
        as_attachment=download,
        filename=filename,
        content_type=content_type,
    )
    response['Cache-Control'] = 'private, no-store'
    return response


@require_GET
def achievement_prize_image_view(request, unlock_id):
    return achievement_prize_file_response(request, unlock_id, download=False)


@require_GET
def achievement_prize_download_view(request, unlock_id):
    return achievement_prize_file_response(request, unlock_id, download=True)


@require_POST
def achievement_shown_view(request, unlock_id):
    access = achievement_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа.'}, status=403)
    unlock = get_object_or_404(
        AchievementUnlock.objects.select_related('user', 'equipment', 'employee_shift', 'prize'),
        id=unlock_id,
    )
    if not achievement_unlock_is_allowed(access, unlock):
        return JsonResponse({'ok': False, 'error': 'Приз недоступен.'}, status=403)
    if not unlock.shown_at:
        unlock.shown_at = timezone.now()
        unlock.save(update_fields=['shown_at'])
    return JsonResponse({'ok': True, 'shown_at': unlock.shown_at.isoformat()})
