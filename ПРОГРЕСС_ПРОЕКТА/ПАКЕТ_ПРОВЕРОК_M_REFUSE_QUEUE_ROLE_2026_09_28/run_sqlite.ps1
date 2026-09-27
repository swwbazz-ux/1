param(
  [Parameter(Mandatory=$true)][string]$SourceRoot,
  [string]$PythonExe = 'python'
)
$ErrorActionPreference = 'Stop'
$managePy = Get-ChildItem -LiteralPath $SourceRoot -Recurse -Filter manage.py | Select-Object -First 1
if (-not $managePy) { throw "manage.py not found under $SourceRoot" }
$backend = $managePy.Directory.FullName
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = "$packageRoot;$backend"
$env:PASSPORT_BACKEND = $backend
New-Item -ItemType Directory -Force (Join-Path $backend 'media/employee_photos') | Out-Null
Push-Location $backend
try {
  & $PythonExe manage.py test `
    passport_probes.django_probes.PassportDowntimePostgreSQLProbe.test_disputed_start_is_idempotent_and_preserves_action_time `
    passport_probes.django_probes.PassportManagerMoveProbe.test_manager_open_shift_can_move_excavator `
    passport_probes.django_probes.PassportManagerMoveProbe.test_manager_closed_shift_cannot_move_excavator `
    passport_probes.django_probes.PassportManagerMoveProbe.test_mining_master_cannot_use_dispatcher_move_endpoint `
    passport_probes.django_probes.PassportManagerAssignmentProbe.test_manager_open_shift_can_release_truck `
    passport_probes.django_probes.PassportManagerAssignmentProbe.test_manager_closed_shift_cannot_release_truck `
    passport_probes.django_probes.PassportManagerAssignmentProbe.test_mining_master_cannot_use_dispatcher_assignment_endpoint `
    passport_probes.django_probes.PassportManagerSettingsProbe.test_manager_open_shift_can_change_face `
    passport_probes.django_probes.PassportManagerSettingsProbe.test_manager_closed_shift_cannot_change_face `
    passport_probes.django_probes.PassportManagerSettingsProbe.test_mining_master_cannot_use_dispatcher_settings_endpoint `
    passport_probes.django_probes.PassportManagerDowntimeProbe.test_manager_open_shift_can_close_downtime `
    passport_probes.django_probes.PassportManagerDowntimeProbe.test_manager_closed_shift_cannot_close_downtime `
    passport_probes.django_probes.PassportManagerDowntimeProbe.test_mining_master_cannot_use_dispatcher_downtime_endpoint `
    passport_probes.django_probes.PassportRoleGuardMatrixProbe.test_common_dispatcher_guard_role_matrix `
    passport_probes.django_probes.PassportDispatcherStaleCommandProbe.test_dispatcher_stale_command_is_409_and_version_is_unchanged `
    passport_probes.django_probes.PassportMiningMasterStaleCommandProbe.test_master_stale_command_is_409_and_version_is_unchanged `
    trips.test_dispatcher_topology_commands.DispatcherMoveExcavatorCommandTests.test_move_to_garage_is_atomic_and_idempotent `
    trips.tests.DispatcherAssignmentRealtimeTests.test_release_complex_emits_assignment_changed_event_without_moving_excavator `
    trips.test_dispatcher_equipment_commands.DispatcherEquipmentCommandBehaviorTests.test_post_saves_settings_and_returns_protected_v2_contract `
    trips.tests.DispatcherDowntimeControlTests.test_dispatcher_closes_downtime_from_matching_excavator_and_truck_cards `
    --verbosity 2
  if ($LASTEXITCODE -ne 0) { throw "Django probes failed: $LASTEXITCODE" }
  node --test $packageRoot/queue_probe.test.js
  if ($LASTEXITCODE -ne 0) { throw "Node probes failed: $LASTEXITCODE" }
} finally { Pop-Location }
