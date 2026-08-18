import hashlib
import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from settlement.models import SettlementResident
from shifts.models import WatchPeriod
from users.models import Employee, EmployeeAccess

from .arrival_roster_parser import (
    MAX_FILE_SIZE,
    PROFILE_CODE,
    PROFILE_VERSION,
    UnsafeArrivalWorkbook,
    parse_arrival_workbook,
    parser_profile_snapshot,
)
from .models import (
    ArrivalRosterEvent,
    ArrivalRosterIssue,
    ArrivalRosterMatch,
    ArrivalRosterMatchCandidate,
    ArrivalRosterMatchRow,
    ArrivalRosterNormalizedRow,
    ArrivalRosterParserProfile,
    ArrivalRosterSourceFile,
    ArrivalRosterSourceRow,
    ArrivalRosterVersion,
)


ALLOWED_CONTENT_TYPES = {
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/octet-stream',
    'application/zip',
    '',
}


def _validation_error(code, message):
    return ValidationError(message, code=code)


def _canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()


def _read_uploaded_xlsx(uploaded_file):
    original_name = Path(str(uploaded_file.name or '')).name
    if not original_name.casefold().endswith('.xlsx'):
        raise _validation_error('arrival_roster.xlsx_required', 'Разрешены только файлы .xlsx.')
    if original_name.casefold().endswith('.xlsm'):
        raise _validation_error('arrival_roster.xlsx_required', 'Книги с макросами не принимаются.')
    content_type = str(getattr(uploaded_file, 'content_type', '') or '').casefold()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise _validation_error('arrival_roster.invalid_content_type', 'Тип загруженного файла не поддерживается.')
    chunks = []
    byte_size = 0
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        byte_size += len(chunk)
        if byte_size > MAX_FILE_SIZE:
            raise _validation_error(
                'arrival_roster.file_too_large',
                'Размер файла превышает 10 МиБ.',
            )
        digest.update(chunk)
        chunks.append(chunk)
    if byte_size == 0:
        raise _validation_error('arrival_roster.empty_file', 'Загружен пустой файл.')
    return {
        'payload': b''.join(chunks),
        'sha256': digest.hexdigest(),
        'byte_size': byte_size,
        'original_name': original_name,
        'content_type': content_type or 'application/octet-stream',
    }


def _access_snapshot(actor_access_id):
    snapshot = (
        EmployeeAccess.objects
        .filter(pk=actor_access_id)
        .values('pk', 'employee_id', 'role_id')
        .first()
    )
    if snapshot is None:
        raise _validation_error(
            'arrival_roster.access_required',
            'Точный доступ табельщика не найден.',
        )
    return snapshot


def _lock_timekeeper_access(snapshot):
    Employee.objects.select_for_update(of=('self',)).get(pk=snapshot['employee_id'])
    try:
        access = (
            EmployeeAccess.objects
            .select_for_update(of=('self',))
            .select_related('employee', 'role')
            .get(pk=snapshot['pk'])
        )
    except EmployeeAccess.DoesNotExist as error:
        raise _validation_error(
            'arrival_roster.access_required',
            'Точный доступ табельщика не найден.',
        ) from error
    if access.employee_id != snapshot['employee_id'] or access.role_id != snapshot['role_id']:
        raise _validation_error(
            'arrival_roster.stale_access',
            'Доступ табельщика изменился. Войдите повторно.',
        )
    if (
        access.status != EmployeeAccess.Status.ACTIVATED
        or not access.is_active
        or not access.employee.is_active
        or access.employee.status != Employee.Status.ACTIVE
        or not access.role.is_active
        or access.role.code != 'timekeeper'
    ):
        raise _validation_error(
            'arrival_roster.access_denied',
            'Доступ табельщика неактивен или не имеет нужной роли.',
        )
    return access


def _profile():
    configuration, configuration_sha256 = parser_profile_snapshot()
    profile = (
        ArrivalRosterParserProfile.objects
        .filter(code=PROFILE_CODE, version=PROFILE_VERSION)
        .first()
    )
    if profile is None:
        profile = ArrivalRosterParserProfile(
            code=PROFILE_CODE,
            version=PROFILE_VERSION,
            configuration=configuration,
            configuration_sha256=configuration_sha256,
        )
        try:
            with transaction.atomic():
                profile.save()
        except IntegrityError:
            profile = ArrivalRosterParserProfile.objects.get(
                code=PROFILE_CODE,
                version=PROFILE_VERSION,
            )
    if (
        profile.configuration != configuration
        or profile.configuration_sha256 != configuration_sha256
    ):
        raise _validation_error(
            'arrival_roster.profile_changed',
            'Профиль проверки изменился без повышения версии.',
        )
    return profile


def _trusted_create_source_file(*, file_data, actor_access):
    payload = bytes(file_data['payload'])
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    actual_byte_size = len(payload)
    if (
        file_data.get('sha256') != actual_sha256
        or file_data.get('byte_size') != actual_byte_size
    ):
        raise _validation_error(
            'rotations.arrival_roster.source_file_integrity_error',
            'Контрольная сумма или размер исходного файла не совпадают с его содержимым.',
        )

    existing = ArrivalRosterSourceFile.objects.filter(sha256=actual_sha256).first()
    if existing is not None:
        if existing.byte_size != actual_byte_size:
            raise _validation_error(
                'arrival_roster.hash_conflict',
                'Обнаружен конфликт контрольной суммы файла.',
            )
        return existing, None

    source = ArrivalRosterSourceFile(
        sha256=actual_sha256,
        original_name=file_data['original_name'],
        byte_size=actual_byte_size,
        content_type=file_data['content_type'],
        uploaded_by_access=actor_access,
    )
    storage_name = f'{actual_sha256[:2]}/{actual_sha256}.xlsx'
    source.file = ContentFile(payload, name=storage_name)
    source.full_clean()
    source.file.save(storage_name, ContentFile(payload), save=False)
    created_storage_name = source.file.name
    try:
        with transaction.atomic():
            ArrivalRosterSourceFile._base_manager.bulk_create([source])
    except IntegrityError:
        source.file.storage.delete(created_storage_name)
        source = ArrivalRosterSourceFile.objects.get(sha256=actual_sha256)
        return source, None
    except Exception:
        source.file.storage.delete(created_storage_name)
        raise
    return source, created_storage_name


def _trusted_bulk_create_source_rows(*, version, parsed_rows):
    source_rows = []
    seen_keys = set()
    for item in parsed_rows:
        row_sha256 = _canonical_sha256({
            'values': item.raw_values,
            'styles': item.raw_styles,
        })
        if item.row_sha256 != row_sha256:
            raise _validation_error(
                'rotations.arrival_roster.source_row_integrity_error',
                'Контрольная сумма исходной строки не совпадает с её содержимым.',
            )
        row_key = (item.sheet_name, item.row_number)
        if row_key in seen_keys:
            raise _validation_error(
                'rotations.arrival_roster.duplicate_source_row',
                'В результате разбора обнаружена повторяющаяся исходная строка.',
            )
        seen_keys.add(row_key)
        source_row = ArrivalRosterSourceRow(
            version=version,
            sheet_name=item.sheet_name,
            row_number=item.row_number,
            row_kind=item.row_kind,
            raw_values=item.raw_values,
            raw_styles=item.raw_styles,
            row_sha256=row_sha256,
        )
        source_row.full_clean()
        source_rows.append(source_row)

    with transaction.atomic():
        return ArrivalRosterSourceRow._base_manager.bulk_create(source_rows)


def _resident_record(resident):
    if resident.employee_id:
        employee = resident.employee
        position = (
            employee.personnel_position.name
            if employee.personnel_position_id
            else employee.position
        )
        full_name = employee.full_name
        phone = employee.phone
    else:
        position = resident.position_title
        full_name = resident.full_name
        phone = resident.phone
    return {
        'resident': resident,
        'name_key': ' '.join(str(full_name or '').replace('\xa0', ' ').split()).casefold().replace('ё', 'е'),
        'position_key': ' '.join(str(position or '').replace('\xa0', ' ').split()).casefold().replace('ё', 'е'),
        'phones': _phone_tokens(phone),
        'active': resident.status == SettlementResident.Status.ACTIVE,
    }


def _phone_tokens(value):
    digits = ''.join(character for character in str(value or '') if character.isdigit())
    if len(digits) == 10:
        digits = f'7{digits}'
    elif len(digits) == 11 and digits.startswith('8'):
        digits = f'7{digits[1:]}'
    return {digits} if len(digits) == 11 and digits.startswith('7') else set()


def _match_plan(normalized, residents):
    same_name = [record for record in residents if record['name_key'] == normalized.normalized_name_key]
    row_phones = set(normalized.normalized_phones)
    phone_matches = [record for record in residents if row_phones.intersection(record['phones'])]
    exact = [record for record in phone_matches if record in same_name and record['active']]
    conflicting_phone = [record for record in phone_matches if record not in same_name]
    archived_name = [record for record in same_name if not record['active']]
    if len(exact) == 1 and not conflicting_phone:
        return 'exact', 'phone_and_name', 'exact', exact[0], []
    if len(exact) > 1 or conflicting_phone:
        candidates = {record['resident'].pk: record for record in [*exact, *conflicting_phone]}
        return 'conflict', 'identifier_conflict', 'conflict', None, list(candidates.values())
    if archived_name:
        return 'conflict', 'archived_name', 'conflict', None, archived_name
    same_position = [
        record for record in same_name
        if normalized.normalized_position_key
        and record['position_key'] == normalized.normalized_position_key
        and record['active']
    ]
    if len(same_position) == 1:
        return 'probable', 'name_and_position', 'probable', None, same_position
    if len(same_name) > 1 or len(same_position) > 1:
        return 'ambiguous', 'multiple_name_candidates', 'ambiguous', None, [
            record for record in same_name if record['active']
        ]
    if len(same_name) == 1 and same_name[0]['active']:
        return 'probable', 'name_only', 'probable', None, same_name
    return 'unmatched', 'not_found', 'unmatched', None, []


def _persist_parse_result(*, version, parsed):
    source_rows = _trusted_bulk_create_source_rows(
        version=version,
        parsed_rows=parsed.source_rows,
    )
    source_by_key = {}
    for source_row in source_rows:
        source_by_key[(source_row.sheet_name, source_row.row_number)] = source_row

    normalized_models = []
    for item in parsed.normalized_rows:
        normalized = ArrivalRosterNormalizedRow(
            source_row=source_by_key[(item.sheet_name, item.row_number)],
            raw_full_name=item.raw_full_name,
            normalized_full_name=item.normalized_full_name,
            normalized_name_key=item.normalized_name_key,
            name_comment=item.name_comment,
            source_position=item.source_position,
            normalized_position_key=item.normalized_position_key,
            raw_shift_hint=item.raw_shift_hint,
            raw_date=item.raw_date,
            arrival_date_candidate=item.arrival_date_candidate,
            date_comment=item.date_comment,
            route_text=item.route_text,
            raw_phone=item.raw_phone,
            normalized_phones=item.normalized_phones,
            comments=item.comments,
            participation_hint=item.participation_hint,
            color_hint=item.color_hint,
        )
        normalized.save()
        normalized_models.append(normalized)

    residents = [
        _resident_record(resident)
        for resident in (
            SettlementResident.objects
            .select_related('employee__personnel_position')
            .order_by('pk')
        )
    ]
    exact_match_by_resident = {}
    exact_rows_by_resident = {}
    proven_residents_by_name = {}
    for normalized in normalized_models:
        status, method, quality, exact_record, candidates = _match_plan(normalized, residents)
        proven_record = proven_residents_by_name.get(normalized.normalized_name_key)
        if (
            exact_record is None
            and status == ArrivalRosterMatch.Status.PROBABLE
            and not normalized.normalized_phones
            and proven_record is not None
            and normalized.normalized_position_key
            and normalized.normalized_position_key == proven_record['position_key']
        ):
            status = ArrivalRosterMatch.Status.EXACT
            method = 'proven_cross_sheet_identity'
            quality = 'exact'
            exact_record = proven_record
            candidates = []
        if exact_record is not None and exact_record['resident'].pk in exact_match_by_resident:
            match = exact_match_by_resident[exact_record['resident'].pk]
            previous_rows = exact_rows_by_resident[exact_record['resident'].pk]
            if (
                normalized.source_row.sheet_name == 'Список сотрудников'
                and any(row.source_row.sheet_name == 'Список сотрудников' for row in previous_rows)
            ):
                ArrivalRosterIssue(
                    version=version,
                    source_row=normalized.source_row,
                    normalized_row=normalized,
                    match=match,
                    severity=ArrivalRosterIssue.Severity.ERROR,
                    code='duplicate_primary_resident',
                    message='Один жилец повторяется в основном списке.',
                ).save()
            prior_shifts = {row.raw_shift_hint for row in previous_rows if row.raw_shift_hint}
            prior_dates = {
                row.arrival_date_candidate
                for row in previous_rows
                if row.arrival_date_candidate
            }
            if normalized.raw_shift_hint and prior_shifts and normalized.raw_shift_hint not in prior_shifts:
                ArrivalRosterIssue(
                    version=version,
                    source_row=normalized.source_row,
                    normalized_row=normalized,
                    match=match,
                    severity=ArrivalRosterIssue.Severity.ERROR,
                    code='conflicting_shift_hints',
                    message='В разных листах указаны разные смены-подсказки.',
                ).save()
            if (
                normalized.arrival_date_candidate
                and prior_dates
                and normalized.arrival_date_candidate not in prior_dates
            ):
                ArrivalRosterIssue(
                    version=version,
                    source_row=normalized.source_row,
                    normalized_row=normalized,
                    match=match,
                    severity=ArrivalRosterIssue.Severity.ERROR,
                    code='conflicting_arrival_dates',
                    message='В разных листах указаны разные даты прибытия.',
                ).save()
            previous_rows.append(normalized)
        else:
            match = ArrivalRosterMatch(
                version=version,
                status=status,
                method=method,
                quality=quality,
                matched_resident=exact_record['resident'] if exact_record else None,
                evidence={
                    'name_key_sha256': hashlib.sha256(
                        normalized.normalized_name_key.encode('utf-8')
                    ).hexdigest(),
                    'has_phone': bool(normalized.normalized_phones),
                    'source_sheet': normalized.source_row.sheet_name,
                    'source_row': normalized.source_row.row_number,
                },
            )
            match.save()
            if exact_record is not None:
                exact_match_by_resident[exact_record['resident'].pk] = match
                exact_rows_by_resident[exact_record['resident'].pk] = [normalized]
                proven_residents_by_name[normalized.normalized_name_key] = exact_record
            for candidate in candidates:
                ArrivalRosterMatchCandidate(
                    match=match,
                    resident=candidate['resident'],
                    evidence={
                        'name_agrees': candidate['name_key'] == normalized.normalized_name_key,
                        'position_agrees': bool(
                            normalized.normalized_position_key
                            and candidate['position_key'] == normalized.normalized_position_key
                        ),
                        'phone_agrees': bool(
                            set(normalized.normalized_phones).intersection(candidate['phones'])
                        ),
                        'resident_active': candidate['active'],
                    },
                ).save()
            if status != ArrivalRosterMatch.Status.EXACT:
                messages = {
                    'probable': 'Найден вероятный жилец; требуется подтверждение табельщиком.',
                    'ambiguous': 'Найдено несколько возможных жильцов.',
                    'unmatched': 'Жилец не найден; создавать новую карточку автоматически запрещено.',
                    'conflict': 'Идентификаторы строки противоречат данным жильцов.',
                }
                ArrivalRosterIssue(
                    version=version,
                    source_row=normalized.source_row,
                    normalized_row=normalized,
                    match=match,
                    severity=ArrivalRosterIssue.Severity.ERROR,
                    code=f'match_{status}',
                    message=messages[status],
                    details={'candidate_count': len(candidates)},
                ).save()
        ArrivalRosterMatchRow(match=match, normalized_row=normalized).save()

    for parsed_issue in parsed.issues:
        source_row = source_by_key.get((parsed_issue.sheet_name, parsed_issue.row_number))
        normalized_row = None
        match = None
        if source_row is not None:
            try:
                normalized_row = source_row.normalized
            except ArrivalRosterNormalizedRow.DoesNotExist:
                normalized_row = None
            if normalized_row is not None:
                try:
                    match = normalized_row.match_link.match
                except ArrivalRosterMatchRow.DoesNotExist:
                    match = None
        ArrivalRosterIssue(
            version=version,
            source_row=source_row,
            normalized_row=normalized_row,
            match=match,
            severity=parsed_issue.severity,
            code=parsed_issue.code,
            message=parsed_issue.message,
            details=parsed_issue.details or {},
        ).save()


@transaction.atomic
def upload_arrival_roster(*, uploaded_file, watch_period_id, actor_access_id):
    file_data = _read_uploaded_xlsx(uploaded_file)
    parsed = parse_arrival_workbook(file_data['payload'])
    snapshot = _access_snapshot(actor_access_id)
    actor_access = _lock_timekeeper_access(snapshot)
    try:
        period = (
            WatchPeriod.objects
            .select_for_update(of=('self',))
            .get(pk=watch_period_id, is_active=True)
        )
    except WatchPeriod.DoesNotExist as error:
        raise _validation_error(
            'arrival_roster.watch_period_required',
            'Выбранный период вахты недоступен.',
        ) from error

    profile = _profile()
    source_file = None
    created_storage_name = None
    try:
        source_file, created_storage_name = _trusted_create_source_file(
            file_data=file_data,
            actor_access=actor_access,
        )
        existing = (
            ArrivalRosterVersion.objects
            .filter(
                watch_period=period,
                source_file=source_file,
                parser_profile=profile,
            )
            .first()
        )
        if existing is not None:
            ArrivalRosterEvent(
                version=existing,
                actor_access=actor_access,
                action=ArrivalRosterEvent.Action.REUSED,
                details={'sha256': source_file.sha256},
            ).save()
            return existing, False

        last_version = (
            ArrivalRosterVersion.objects
            .select_for_update(of=('self',))
            .filter(watch_period=period)
            .order_by('-version_number', '-pk')
            .first()
        )
        version = ArrivalRosterVersion(
            watch_period=period,
            version_number=(last_version.version_number + 1) if last_version else 1,
            status=ArrivalRosterVersion.Status.REVIEW_REQUIRED,
            source_file=source_file,
            parser_profile=profile,
            uploaded_by_access=actor_access,
        )
        version.save()
        _persist_parse_result(version=version, parsed=parsed)

        blocking_count = version.issues.filter(
            severity=ArrivalRosterIssue.Severity.ERROR,
        ).count()
        warning_count = version.issues.filter(
            severity=ArrivalRosterIssue.Severity.WARNING,
        ).count()
        source_rows = list(
            version.source_rows.order_by('sheet_name', 'row_number').values(
                'sheet_name', 'row_number', 'row_kind', 'row_sha256',
            )
        )
        matches = list(
            version.matches.order_by('pk').values(
                'status', 'method', 'quality', 'matched_resident_id',
            )
        )
        immutable_snapshot = {
            'watch_period_id': period.pk,
            'version_number': version.version_number,
            'source_sha256': source_file.sha256,
            'parser_profile': {
                'code': profile.code,
                'version': profile.version,
                'sha256': profile.configuration_sha256,
            },
            'workbook': parsed.workbook_summary,
            'source_rows': source_rows,
            'matches': matches,
            'blocking_issue_count': blocking_count,
            'warning_count': warning_count,
        }
        version.status = (
            ArrivalRosterVersion.Status.REVIEW_REQUIRED
            if blocking_count
            else ArrivalRosterVersion.Status.DRAFT
        )
        version.source_row_count = len(source_rows)
        version.normalized_row_count = len(parsed.normalized_rows)
        version.blocking_issue_count = blocking_count
        version.warning_count = warning_count
        version.snapshot = immutable_snapshot
        version.snapshot_sha256 = _canonical_sha256(immutable_snapshot)
        version.save(
            update_fields=[
                'status', 'source_row_count', 'normalized_row_count',
                'blocking_issue_count', 'warning_count', 'snapshot',
                'snapshot_sha256', 'updated_at',
            ]
        )
        ArrivalRosterEvent(
            version=version,
            actor_access=actor_access,
            action=ArrivalRosterEvent.Action.UPLOADED,
            details={
                'sha256': source_file.sha256,
                'byte_size': source_file.byte_size,
            },
        ).save()
        ArrivalRosterEvent(
            version=version,
            actor_access=actor_access,
            action=ArrivalRosterEvent.Action.PARSED,
            details={
                'source_rows': version.source_row_count,
                'normalized_rows': version.normalized_row_count,
                'blocking_issues': version.blocking_issue_count,
                'warnings': version.warning_count,
            },
        ).save()
        return version, True
    except Exception:
        if created_storage_name and source_file is not None:
            source_file.file.storage.delete(created_storage_name)
        raise


__all__ = ['UnsafeArrivalWorkbook', 'upload_arrival_roster']
