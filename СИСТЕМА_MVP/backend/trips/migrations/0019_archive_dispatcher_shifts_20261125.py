from datetime import datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from django.db import migrations, models
from django.db.models import Exists, F, OuterRef, Q


# Freeze the requested production day and its time zone in the migration.
START = datetime(2026, 11, 25, 7, tzinfo=ZoneInfo('Asia/Vladivostok'))
NIGHT = START + timedelta(hours=12)
END = NIGHT + timedelta(hours=12)


def archive_shifts(apps, schema_editor):
    db = schema_editor.connection.alias
    shifts = apps.get_model('shifts', 'EmployeeShift').objects.using(db)
    accesses = apps.get_model('users', 'EmployeeAccess').objects.using(db)
    sessions = apps.get_model('users', 'ActiveApplicationSession').objects.using(db)
    observations = apps.get_model('trips', 'DispatcherDutyWindow').objects.using(db)

    # The existing journal and shift boundaries already carry their real timestamps.
    # Use the same narrow workplace fallback as the dispatcher header for older shifts.
    member_access = accesses.filter(employee_id=OuterRef('employee_id'), role__is_active=True,
                                    is_active=True, status='activated')
    duties = list(shifts.filter(opened_at__lt=END).filter(Q(closed_at__gt=START) | Q(closed_at__isnull=True))
                  .annotate(has_dispatch=Exists(member_access.filter(role__code='dispatcher')),
                            has_other=Exists(member_access.filter(role__code__in=[
                                'driver', 'excavator_operator', 'mining_master', 'oup'])))
                  .filter(Q(workplace_code='dispatcher') | Q(workplace_code='', equipment__isnull=True,
                                                            has_dispatch=True, has_other=False))
                  .values_list('pk', 'employee_id', 'opened_at', 'closed_at'))

    # A session row changes on each heartbeat. Equality with last_seen proves that
    # the foreground stamp belongs to the recorded access and path, even after a role switch.
    points = (sessions.filter(role_code='dispatcher', app_code='dispatcher', access__role__code='dispatcher',
                              access__employee_id__in={member for _, member, _, _ in duties},
                              foreground_seen_at__gte=START, foreground_seen_at__lt=END,
                              foreground_seen_at=F('last_seen_at'))
              .filter(Q(path__startswith='/dispatcher/') | Q(path__startswith='/reports/'))
              .values_list('pk', 'access__employee_id', 'foreground_seen_at'))
    for session_id, member, at in points:
        owners = [pk for pk, employee, opened, closed in duties
                  if member == employee and opened <= at and (closed is None or at < closed)]
        if len(owners) != 1:
            continue
        source = uuid5(NAMESPACE_URL, f'urn:copper:dispatcher:archive:20261125:{session_id}')
        observations.get_or_create(
            shift_id=owners[0], state='archive_visible', window_id=source, started_at=at,
            defaults={'session_digest': sha256(source.bytes).hexdigest(), 'last_seen_at': at, 'last_sequence': 0},
        )

    def available(first, last):
        return 'available' if any(opened < last and (closed is None or first < closed)
                                  for _, _, opened, closed in duties) else 'no_shift_record'

    # Only report whether each archive is available. Personnel, times and actions stay in the protected view.
    print(f' DISPATCHER_ARCHIVE 2026-11-25 day={available(START, NIGHT)} night={available(NIGHT, END)}', end=' ')


def remove_archive_points(apps, schema_editor):
    observations = apps.get_model('trips', 'DispatcherDutyWindow').objects.using(schema_editor.connection.alias)
    observations.filter(state='archive_visible', started_at__gte=START, started_at__lt=END).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('trips', '0018_dispatcher_duty_history'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dispatcherdutywindow',
            name='state',
            field=models.CharField(choices=[('visible', 'Пульт на экране'), ('hidden', 'Пульт свёрнут'), ('closed', 'Окно закрыто'), ('signed_out', 'Выход из приложения'), ('archive_visible', 'Архивная отметка открытого пульта')], max_length=16, verbose_name='Состояние'),
        ),
        migrations.RunPython(archive_shifts, remove_archive_points),
    ]
