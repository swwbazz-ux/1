import json
from datetime import timedelta
from pathlib import Path

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, ExcavatorPlacement, HaulAssignment, HaulAssignmentAction
from core.models import OperationalStateEvent
from downtimes.models import DowntimeEvent, DowntimeReason
from references.equipment_states import upsert_default_equipment_states
from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentState,
    EquipmentType,
    RockType,
)
from shifts.models import EmployeeShift, EquipmentPlanGroup, PlanAssignmentStatus, PlanCalculationMode
from shifts.services import assign_shift_plan_snapshot, progress_cycle_visual_context
from trips.models import Trip, TripClientAction, TripStatus
from trips.views import build_dispatcher_dashboard_context, finalize_trip_unloaded
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role


class DispatcherSharedShiftStartTests(TestCase):
    def setUp(self):
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.current_dispatcher = Employee.objects.create(
            full_name='Дежурный диспетчер',
            phone='79000000500',
            is_active=True,
        )
        self.current_access = EmployeeAccess.objects.create(
            employee=self.current_dispatcher,
            role=self.dispatcher_role,
            access_code='500000',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = self.current_access.id
        session['device_kind'] = 'shared'
        session.save()

    def test_shared_desktop_shows_dispatcher_reauth_popup(self):
        response = self.client.get(reverse('dispatcher_control'))

        self.assertContains(response, 'data-shared-shift-login-open')
        self.assertContains(response, 'Вход Горного диспетчера')
        self.assertContains(response, 'код диспетчера')
        self.assertContains(response, 'name="reauth_phone"')
        self.assertContains(response, 'placeholder="999-000-00-00"')
        self.assertContains(response, 'name="reauth_access_code"')
        self.assertContains(response, 'placeholder="00-00-00"')
        self.assertContains(response, 'maxlength="8"')
        self.assertContains(response, 'pattern="[0-9]{2}-[0-9]{2}-[0-9]{2}"')
        self.assertContains(response, 'name="device_kind"')
        self.assertContains(response, 'value="shared"')

    def test_dispatcher_control_uses_reauth_popup_even_if_session_was_personal(self):
        session = self.client.session
        session['device_kind'] = 'personal'
        session.save()

        response = self.client.get(reverse('dispatcher_control'))

        self.assertContains(response, 'data-shared-shift-login-open')
        self.assertNotContains(response, 'data-confirm="Начать смену горного диспетчера?"')

    def test_dispatcher_truck_actions_use_local_dom_update_hook(self):
        response = self.client.get(reverse('dispatcher_control'))

        self.assertContains(response, 'function applyDesktopTruckAction')
        self.assertContains(response, 'function sortDesktopEquipmentList')
        self.assertContains(response, 'function compareDesktopEquipmentTiles')
        self.assertContains(response, 'function removeDuplicateDesktopTruckTiles')
        self.assertContains(response, 'function reconcileDesktopTruckUniqueness')
        self.assertContains(response, 'function refreshDesktopBoardIntegrity')
        self.assertContains(response, 'function refreshDesktopBoardAfterStructuralAction')
        self.assertContains(response, 'moveDesktopTruckToComplex')
        self.assertContains(response, 'releaseDesktopComplexTrucks')
        self.assertContains(response, 'activateDesktopComplexFromExcavatorTile')
        self.assertContains(response, 'moveDesktopComplexToExcavatorGarage')
        self.assertContains(response, 'confirmDesktopOptimisticBoardAction')
        self.assertContains(response, 'function applyDispatcherOperationalStateRefresh')
        self.assertContains(response, 'function refreshDispatcherDesktopBoardFromServer')
        self.assertContains(response, 'bindDispatcherDesktopInteractions')
        self.assertContains(response, 'window.initAppConfirmForms')
        self.assertContains(response, 'window.initDispatcherThemeControls')
        self.assertContains(response, 'window.initDispatcherRadialClocks')
        self.assertContains(response, 'events_truncated')
        self.assertContains(response, '"employee_changed"')
        self.assertContains(response, '"access_changed"')
        self.assertContains(response, 'markDispatcherLocalAssignmentApplied')
        self.assertContains(response, 'dispatcherIncomingRefreshQueueGraceMs')
        self.assertContains(response, 'dispatcherMobileSyncFlushDelayMs = 300')
        self.assertContains(response, 'isDispatcherSyncQueueBlockingRefresh')
        self.assertContains(response, 'type: "assign"')
        self.assertContains(response, 'type: "release"')
        self.assertContains(response, 'type: "release_complex"')

    def test_dispatcher_screen_has_own_pwa_and_sync_overlay(self):
        response = self.client.get(reverse('dispatcher_control'))

        self.assertContains(response, reverse('dispatcher_manifest'))
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, '/dispatcher-sw.js')
        self.assertContains(response, 'scope: "/dispatcher/"')
        self.assertContains(response, 'registration.update()')
        self.assertContains(response, 'SKIP_WAITING')
        self.assertContains(response, 'data-app-sync-overlay')
        self.assertContains(response, 'showAppSyncOverlay')
        self.assertContains(response, 'data-app-realtime-status')
        self.assertContains(response, 'data-app-realtime-update')
        self.assertContains(response, 'data-realtime-screen')
        self.assertContains(response, 'data-operational-state-version')
        self.assertContains(response, 'window.AppRealtimeConfig')
        self.assertContains(response, 'initialVersion: Number(document.body.dataset.operationalStateVersion || 0)')
        self.assertContains(response, 'js/realtime-client.js')
        self.assertContains(response, 'pollIntervalMs: 1000')
        self.assertContains(response, 'dispatcher-control')
        self.assertContains(response, '{name: "dispatcher-reports", role: "dispatcher", mode: "manual"')
        self.assertContains(response, 'system-admin')
        self.assertContains(response, '{name: "management-shift-analytics", role: "management", mode: "custom"')
        self.assertContains(response, '{name: "management-reports", role: "management", mode: "manual"')
        self.assertNotContains(response, 'mechanic-downtimes')
        self.assertContains(response, '{name: "excavator", role: "excavator_operator", mode: "custom"')
        self.assertContains(response, '{name: "driver", role: "driver", mode: "custom"')
        self.assertContains(response, 'is-realtime-stale')
        self.assertContains(response, 'Связь с сервером потеряна. Экран может отставать.')
        self.assertNotContains(response, '/mining-master-sw.js')

        realtime_client = Path(__file__).resolve().parents[1] / 'static' / 'js' / 'realtime-client.js'
        script = realtime_client.read_text(encoding='utf-8')
        self.assertIn('operational-state-connection', script)
        self.assertIn('window.addEventListener("focus"', script)
        self.assertIn('window.addEventListener("pageshow"', script)
        self.assertIn('window.addEventListener("online"', script)
        self.assertIn('window.addEventListener("offline"', script)
        self.assertIn('window.applyOperationalStateRefresh', script)
        self.assertIn('window.AppRealtime', script)
        self.assertIn('getDebugState', script)
        self.assertIn('initialVersion', script)
        self.assertIn('previousVersion', script)
        self.assertIn('buildOperationalStatePollUrl', script)
        self.assertIn('include_events", "0"', script)
        self.assertIn('operational-state-refresh-deferred', script)
        self.assertIn('pending_mobile_queue', script)
        self.assertIn('refreshMobileBoard: true', response.content.decode('utf-8'))
        self.assertIn('request.refreshMobileBoard && !freshQueue.length', response.content.decode('utf-8'))
        self.assertIn('dispatcherMobileSyncFlushDelayMs', response.content.decode('utf-8'))
        self.assertNotIn('delayMs : 5000', response.content.decode('utf-8'))
        self.assertIn('operational-state-update-available', script)
        self.assertIn('has-realtime-update', script)
        self.assertIn('refreshManualRealtimeScreen', script)

    def test_dispatcher_manifest_is_installable_pwa_manifest(self):
        response = self.client.get(reverse('dispatcher_manifest'))
        manifest = json.loads(response.content.decode('utf-8'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/manifest+json; charset=utf-8')
        self.assertEqual(manifest['name'], 'Горный диспетчер')
        self.assertEqual(manifest['start_url'], reverse('dispatcher_control'))
        self.assertEqual(manifest['scope'], '/dispatcher/')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['orientation'], 'landscape')
        self.assertIn('icons', manifest)
        self.assertTrue(any(icon.get('sizes') == '192x192' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('sizes') == '512x512' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('purpose') == 'maskable' for icon in manifest['icons']))

    def test_dispatcher_service_worker_serves_installable_shell(self):
        response = self.client.get(reverse('dispatcher_service_worker'))
        script = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/javascript; charset=utf-8')
        self.assertEqual(response['Service-Worker-Allowed'], '/dispatcher/')
        self.assertIn('dispatcher-desktop-shell-v32', script)
        self.assertIn(reverse('dispatcher_control'), script)
        self.assertIn(reverse('dispatcher_manifest'), script)
        self.assertIn('/static/js/realtime-client.js', script)
        self.assertIn('ignoreSearch: true', script)
        self.assertIn('request.headers.get("X-Requested-With") === "XMLHttpRequest"', script)
        self.assertIn('networkOnly(request)', script)
        self.assertIn('self.addEventListener("fetch"', script)
        self.assertIn('SKIP_WAITING', script)
        self.assertIn('GET_VERSION', script)

    def test_shared_desktop_blocks_direct_start_without_reauth(self):
        response = self.client.post(reverse('dispatcher_toggle_shift'), {'shift_action': 'start'})

        self.assertRedirects(response, reverse('dispatcher_control'))
        self.assertFalse(
            EmployeeShift.objects
            .filter(employee=self.current_dispatcher, closed_at__isnull=True)
            .exists()
        )

    def test_shared_desktop_can_start_shift_with_dispatcher_credentials(self):
        next_dispatcher = Employee.objects.create(
            full_name='Сменный диспетчер',
            phone='79000000544',
            is_active=True,
        )
        next_access = EmployeeAccess.objects.create(
            employee=next_dispatcher,
            role=self.dispatcher_role,
            access_code='544544',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )

        response = self.client.post(
            reverse('dispatcher_toggle_shift'),
            {
                'shift_action': 'start',
                'reauth_phone': '900-000-05-44',
                'reauth_access_code': '54-45-44',
                'device_kind': 'shared',
            },
        )

        self.assertRedirects(response, reverse('dispatcher_control'))
        self.assertFalse(
            EmployeeShift.objects
            .filter(employee=self.current_dispatcher, closed_at__isnull=True)
            .exists()
        )
        self.assertTrue(
            EmployeeShift.objects
            .filter(employee=next_dispatcher, closed_at__isnull=True)
            .exists()
        )
        self.assertEqual(self.client.session['employee_access_id'], next_access.id)
        self.assertEqual(self.client.session['device_kind'], 'shared')


class DispatcherGarageCurrentStateTests(TestCase):
    def setUp(self):
        self.dispatcher = Employee.objects.create(full_name='Диспетчер смены')
        self.shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            opened_at=timezone.now() - timedelta(hours=1),
        )
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.excavator = Equipment.objects.create(equipment_type=self.excavator_type, garage_number='1')
        self.idle_excavator = Equipment.objects.create(equipment_type=self.excavator_type, garage_number='2')
        self.free_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='10')
        self.active_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='11')
        self.downtime_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='12')
        self.assigned_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='13')
        self.rock = RockType.objects.create(name='Руда')
        self.dump_point = DumpPoint.objects.create(name='ККД')
        self.reason = DowntimeReason.objects.create(
            name='Аварийный простой',
            equipment_type=self.truck_type,
            is_critical=True,
        )

    def build_dashboard(self):
        active_trips = Trip.objects.filter(status__in=(TripStatus.ACTIVE, TripStatus.LOADED_WAITING_UNLOAD))
        return build_dispatcher_dashboard_context(
            dispatcher_shift=self.shift,
            active_trips=active_trips,
            pending_assignments=HaulAssignment.objects.filter(status=AssignmentStatus.PENDING),
            accepted_assignments=HaulAssignment.objects.filter(status=AssignmentStatus.ACCEPTED),
            recent_completed_trips=Trip.objects.none(),
            open_shifts=EmployeeShift.objects.filter(closed_at__isnull=True).exclude(id=self.shift.id),
            open_mechanic_downtimes=DowntimeEvent.objects.filter(ended_at__isnull=True),
            trucks=Equipment.objects.filter(equipment_type=self.truck_type).order_by('garage_number'),
            excavators=Equipment.objects.filter(equipment_type=self.excavator_type).order_by('garage_number'),
            recent_dispatcher_actions=[],
        )

    def create_plan_group(self, *, equipment, mode, value, name='Группа плана', is_active=True):
        group = EquipmentPlanGroup.objects.create(
            name=name,
            code=f'group-{equipment.id}-{EquipmentPlanGroup.objects.count() + 1}',
            calculation_mode=mode,
            plan_value=value,
            is_active=is_active,
        )
        group.equipment.add(equipment)
        return group

    def open_equipment_shift(self, equipment, *, employee_name='Сотрудник'):
        employee = Employee.objects.create(full_name=employee_name)
        shift = EmployeeShift.objects.create(
            employee=employee,
            shift_type='day',
            equipment=equipment,
            opened_at=self.shift.opened_at,
            opened_by=employee,
        )
        assign_shift_plan_snapshot(shift)
        return shift

    def create_completed_trip(self, *, truck, excavator, unloading_shift=None, loading_shift=None, volume='850.00'):
        return Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            loading_shift=loading_shift,
            unloading_shift=unloading_shift,
            volume_m3=volume,
            status=TripStatus.COMPLETED,
            created_at=self.shift.opened_at + timedelta(minutes=5),
            completed_at=self.shift.opened_at + timedelta(minutes=10),
        )

    def test_garages_show_current_equipment_state(self):
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.active_truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.ACTIVE,
            created_at=timezone.now(),
        )
        ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
        )
        DowntimeEvent.objects.create(
            equipment=self.downtime_truck,
            reason=self.reason,
            started_at=timezone.now() - timedelta(minutes=15),
        )
        HaulAssignment.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )

        dashboard = self.build_dashboard()
        trucks_by_name = {tile['name']: tile for tile in dashboard['truck_garage_tiles']}
        complex_by_id = {card['id']: card for card in dashboard['complex_cards']}
        excavators_by_number = {
            tile['display_name']: tile
            for tile in dashboard['excavator_garage_tiles']
            if not tile.get('is_placeholder')
        }

        self.assertEqual(trucks_by_name['10']['status'], 'gray')
        self.assertEqual(trucks_by_name['10']['equipment_state_code'], 'free')
        self.assertEqual(trucks_by_name['10']['label'], 'Свободен')
        self.assertEqual(trucks_by_name['10']['percent'], 0)
        self.assertEqual(trucks_by_name['11']['status'], 'green')
        self.assertEqual(trucks_by_name['11']['equipment_state_code'], 'loaded_waiting_unload')
        self.assertEqual(trucks_by_name['11']['label'], 'На разгрузку')
        self.assertEqual(trucks_by_name['12']['status'], 'red')
        self.assertEqual(trucks_by_name['12']['equipment_state_code'], 'breakdown')
        self.assertEqual(trucks_by_name['12']['label'], 'Поломка')
        assigned_tile = next(tile for tile in complex_by_id['K-1']['active_truck_tiles'] if tile['name'] == '13')
        self.assertEqual(assigned_tile['status'], 'blue')
        self.assertEqual(assigned_tile['equipment_state_code'], 'assigned')
        self.assertEqual(assigned_tile['label'], 'Назначена')
        self.assertEqual(excavators_by_number['2']['status'], 'gray')
        self.assertEqual(excavators_by_number['2']['equipment_state_code'], 'garage')
        self.assertEqual(excavators_by_number['2']['label'], 'Гараж')
        self.assertEqual(dashboard['equipment_state_ui']['free']['color_group'], 'gray')
        self.assertEqual(dashboard['equipment_state_ui']['garage']['color_group'], 'gray')

    def test_complex_cards_use_standard_equipment_state_colors(self):
        blue_excavator = Equipment.objects.create(equipment_type=self.excavator_type, garage_number='3')
        yellow_excavator = Equipment.objects.create(equipment_type=self.excavator_type, garage_number='4')
        orange_excavator = Equipment.objects.create(equipment_type=self.excavator_type, garage_number='5')
        blue_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='14')
        yellow_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='15')
        repair_reason = DowntimeReason.objects.create(
            name='Ремонт механической службы',
            equipment_type=self.excavator_type,
            is_critical=False,
        )
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.active_truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.ACTIVE,
            created_at=timezone.now(),
        )
        ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
        )
        HaulAssignment.objects.create(
            truck=blue_truck,
            excavator=blue_excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        HaulAssignment.objects.create(
            truck=yellow_truck,
            excavator=yellow_excavator,
            status=AssignmentStatus.PENDING,
        )
        ExcavatorPlacement.objects.create(
            excavator=orange_excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
        )
        DowntimeEvent.objects.create(
            equipment=orange_excavator,
            reason=repair_reason,
            started_at=timezone.now() - timedelta(minutes=15),
        )

        dashboard = self.build_dashboard()
        complex_by_id = {card['id']: card for card in dashboard['complex_cards']}

        self.assertEqual(complex_by_id['K-1']['status_key'], 'green')
        self.assertEqual(complex_by_id['K-1']['equipment_state_code'], 'working')
        self.assertEqual(complex_by_id['K-3']['status_key'], 'blue')
        self.assertEqual(complex_by_id['K-3']['equipment_state_code'], 'assigned')
        self.assertEqual(complex_by_id['K-4']['status_key'], 'yellow')
        self.assertEqual(complex_by_id['K-4']['equipment_state_code'], 'waiting')
        self.assertEqual(complex_by_id['K-5']['status_key'], 'orange')
        self.assertEqual(complex_by_id['K-5']['equipment_state_code'], 'repair')

    def test_downtime_summaries_use_reason_state_color(self):
        upsert_default_equipment_states()
        waiting_state = EquipmentState.objects.get(code='waiting')
        waiting_reason = DowntimeReason.objects.create(
            name='Тестовое ожидание сводки',
            equipment_type=self.truck_type,
            equipment_state=waiting_state,
            is_critical=False,
        )
        DowntimeEvent.objects.create(
            equipment=self.downtime_truck,
            reason=waiting_reason,
            started_at=timezone.now(),
        )
        DowntimeEvent.objects.create(
            equipment=self.assigned_truck,
            reason=self.reason,
            started_at=timezone.now() - timedelta(minutes=15),
        )

        dashboard = self.build_dashboard()
        loss_by_label = {row['label']: row for row in dashboard['loss_reasons']}

        self.assertEqual(dashboard['event_rows'][0]['status'], 'warning')
        self.assertEqual(loss_by_label['Тестовое ожидание сводки']['status'], 'warning')
        self.assertEqual(loss_by_label['Аварийный простой']['status'], 'danger')

    def test_dispatcher_dashboard_returns_truck_snapshot_plan_progress(self):
        group = self.create_plan_group(
            equipment=self.free_truck,
            mode=PlanCalculationMode.TRIPS,
            value='3.00',
            name='Самосвалы БелАЗ dispatcher',
        )
        truck_shift = self.open_equipment_shift(self.free_truck, employee_name='Водитель БелАЗ')
        self.create_completed_trip(truck=self.free_truck, excavator=self.excavator, unloading_shift=truck_shift)

        group.plan_value = '10.00'
        group.save(update_fields=['plan_value'])
        dashboard = self.build_dashboard()
        trucks_by_name = {tile['name']: tile for tile in dashboard['truck_garage_tiles']}
        tile = trucks_by_name['10']

        self.assertEqual(tile['plan_status'], PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(tile['plan_group_name'], 'Самосвалы БелАЗ dispatcher')
        self.assertEqual(tile['plan_calculation_mode'], PlanCalculationMode.TRIPS)
        self.assertEqual(tile['plan_value'], '3')
        self.assertEqual(tile['plan_fact_value'], '1')
        self.assertEqual(tile['plan_unit'], 'рейса')
        self.assertEqual(tile['percent'], 33)
        self.assertEqual(tile['plan']['percent'], 33)
        self.assertEqual(tile['plan']['fact_plan_label'], '1 / 3 рейса')
        self.assertEqual(tile['plan']['value'], truck_shift.plan_value)

    def test_dispatcher_dashboard_returns_excavator_snapshot_plan_progress(self):
        self.create_plan_group(
            equipment=self.excavator,
            mode=PlanCalculationMode.VOLUME,
            value='3000.00',
            name='Экскаваторы 4000 dispatcher',
        )
        excavator_shift = self.open_equipment_shift(self.excavator, employee_name='Машинист 4000')
        self.create_completed_trip(truck=self.free_truck, excavator=self.excavator, loading_shift=excavator_shift, volume='850.00')
        ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
        )

        dashboard = self.build_dashboard()
        complex_by_id = {card['id']: card for card in dashboard['complex_cards']}
        card = complex_by_id['K-1']

        self.assertEqual(card['plan_status'], PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(card['plan_group_name'], 'Экскаваторы 4000 dispatcher')
        self.assertEqual(card['plan_calculation_mode'], PlanCalculationMode.VOLUME)
        self.assertEqual(card['plan_value'], '3 000')
        self.assertEqual(card['plan_fact_value'], '850')
        self.assertEqual(card['plan_unit'], 'м³')
        self.assertEqual(card['percent'], 28)
        self.assertEqual(card['plan']['fact_plan_label'], '850 / 3 000 м³')

    def test_dispatcher_dashboard_does_not_turn_missing_plans_into_zero_percent(self):
        no_group_shift = self.open_equipment_shift(self.downtime_truck, employee_name='Водитель без группы')
        inactive_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='16')
        self.create_plan_group(
            equipment=inactive_truck,
            mode=PlanCalculationMode.TRIPS,
            value='4.00',
            name='Самосвалы NHL inactive',
            is_active=False,
        )
        no_active_shift = self.open_equipment_shift(inactive_truck, employee_name='Водитель без активного плана')

        dashboard = self.build_dashboard()
        trucks_by_name = {tile['name']: tile for tile in dashboard['truck_garage_tiles']}

        self.assertEqual(no_group_shift.plan_status, PlanAssignmentStatus.NO_PLAN_GROUP)
        self.assertEqual(trucks_by_name['12']['plan_status'], PlanAssignmentStatus.NO_PLAN_GROUP)
        self.assertIsNone(trucks_by_name['12']['plan']['percent'])
        self.assertEqual(trucks_by_name['12']['plan_fact_label'], 'Нет группы плана')
        self.assertEqual(no_active_shift.plan_status, PlanAssignmentStatus.NO_ACTIVE_PLAN)
        self.assertEqual(trucks_by_name['16']['plan_status'], PlanAssignmentStatus.NO_ACTIVE_PLAN)
        self.assertIsNone(trucks_by_name['16']['plan']['percent'])
        self.assertEqual(trucks_by_name['16']['plan_fact_label'], 'Нет активного плана')
        self.assertEqual(trucks_by_name['10']['plan_status'], 'plan_not_assigned')
        self.assertIsNone(trucks_by_name['10']['plan']['percent'])
        self.assertEqual(trucks_by_name['10']['plan_fact_label'], 'План не назначен')


class ExcavatorWorkServerIntegrationTests(TestCase):
    def create_registered_driver_shift(self, truck, *, full_name='Петров П.П.', access_code='200000'):
        driver_role, _ = Role.objects.get_or_create(
            code='driver',
            defaults={'name': 'Водитель'},
        )
        driver = Employee.objects.create(full_name=full_name)
        access = EmployeeAccess.objects.create(
            employee=driver,
            role=driver_role,
            access_code=access_code,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        dormitory, _ = Dormitory.objects.get_or_create(number='5')
        block, _ = DormitoryBlock.objects.get_or_create(dormitory=dormitory, name='Блок 1')
        section, _ = DormitorySection.objects.get_or_create(block=block, name='А')
        DriverPrimaryRegistration.objects.create(
            employee=driver,
            dormitory_section=section,
        )
        shift = EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=truck,
            opened_at=timezone.now(),
            opened_by=driver,
        )
        return driver, access, shift

    def create_driver_assignment(self, truck, *, driver=None, shift=None, status=AssignmentStatus.ACCEPTED):
        return EquipmentAssignment.objects.create(
            employee=driver or self.driver,
            equipment=truck,
            shift=shift,
            assigned_by=self.operator,
            status=status,
        )

    def setUp(self):
        upsert_default_equipment_states()
        self.role = Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
        self.operator = Employee.objects.create(full_name='Машинист ЭКГ')
        self.access = EmployeeAccess.objects.create(
            employee=self.operator,
            role=self.role,
            access_code='300000',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='21')
        self.other_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='99')
        self.excavator = Equipment.objects.create(equipment_type=self.excavator_type, garage_number='12')
        self.other_excavator = Equipment.objects.create(equipment_type=self.excavator_type, garage_number='13')
        self.rock = RockType.objects.create(name='Руда')
        self.dump_point = DumpPoint.objects.create(name='Дробилка')
        self.driver, self.driver_access, self.truck_shift = self.create_registered_driver_shift(
            self.truck,
            full_name='Петров П.П.',
            access_code='200021',
        )
        self.reason = DowntimeReason.objects.get(name='Ожидание самосвалов')
        self.reason.equipment_type = self.excavator_type
        self.reason.show_for_excavator_operator = True
        self.reason.save(update_fields=['equipment_type', 'show_for_excavator_operator'])
        EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            equipment=self.excavator,
            opened_at=timezone.now(),
        )
        HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        HaulAssignment.objects.create(
            truck=self.other_truck,
            excavator=self.other_excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()

    def test_excavator_work_uses_project_realtime_and_reference_data(self):
        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-realtime-screen')
        self.assertContains(response, '{name: "excavator", role: "excavator_operator", mode: "custom"')
        self.assertContains(response, 'customRefresh: true')
        self.assertContains(response, reverse('excavator_manifest'))
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, '/static/css/excavator-work-v55.css')
        self.assertContains(response, '/static/css/excavator-work-v55-final.css')
        self.assertContains(response, '/static/css/excavator-work-v55-shift.css')
        self.assertContains(response, '/excavator-sw.js')
        self.assertContains(response, 'scope: "/excavator/"')
        self.assertContains(response, 'excavator-mobile-shell-v106')
        self.assertContains(response, 'data-eo-shift-mileage disabled aria-disabled="true" tabindex="-1"')
        self.assertContains(response, 'class="eo-downtime-state-dot"')
        self.assertContains(response, 'eo-downtime-state-icon-play')
        self.assertContains(response, 'eo-downtime-state-icon-pause')
        self.assertContains(response, 'data-eo-active-duration')
        self.assertNotContains(response, 'data-eo-active-title')
        self.assertNotContains(response, 'data-eo-active-reason')
        self.assertContains(response, 'data-eo-apply-settings data-eo-hold-label="Применить настройки"')
        self.assertContains(response, 'data-eo-settings-applied="')
        self.assertContains(response, 'data-eo-settings-applied="false"')
        self.assertContains(response, 'appliedExcavatorSettingsSnapshot')
        self.assertContains(response, 'currentExcavatorSettingsSnapshot() !== appliedExcavatorSettingsSnapshot')
        self.assertContains(response, 'data.work_context_changed && data.active_downtime_reason')
        self.assertContains(response, '? "downtime"')
        self.assertContains(response, 'Простои')
        self.assertContains(response, '>Работа</span>')
        self.assertContains(response, '>Простой</span>')
        self.assertContains(response, 'class="mm-mobile-nav-icon"')
        self.assertContains(response, 'data-eo-tab="shift" data-eo-pwa-update-nav-target')
        self.assertContains(response, 'tab.setAttribute("aria-current", "page")')
        self.assertContains(response, 'if (tabName === "events")')
        self.assertContains(response, 'refreshExcavatorWorkFromServer({ preserveTab: true })')
        self.assertNotContains(response, 'class="eo-nav-clock"')
        self.assertNotContains(response, 'class="eo-nav-bell"')
        self.assertNotContains(response, 'data-eo-icon-idle')
        nav_css = (
            Path(__file__).resolve().parents[1]
            / 'static'
            / 'css'
            / 'excavator-work-v55-shift.css'
        ).read_text(encoding='utf-8')
        self.assertIn('.mm-mobile-nav-item.is-active.has-pwa-update', nav_css)
        self.assertIn('.mm-mobile-nav-item:not(.is-active).has-pwa-update', nav_css)
        self.assertNotContains(response, 'Отпустить сюда')
        self.assertContains(response, 'resolveExcavatorUpdateVersion')
        self.assertContains(response, 'renderUpdateModal')
        self.assertContains(response, 'fetchServerVersion')
        self.assertContains(response, 'normalizeExcavatorVersion')
        self.assertContains(response, 'data-eo-pwa-update-check')
        self.assertContains(response, 'data-eo-pwa-update-check-label')
        self.assertContains(response, 'data-eo-pwa-update-check-version')
        self.assertNotContains(response, 'Сверьте с фактом')
        self.assertNotContains(response, 'eo-shift-attention-label')
        self.assertContains(response, 'Обновить')
        self.assertContains(response, 'runManualUpdateCheck')
        self.assertContains(response, 'Проверка...')

    def test_excavator_work_disables_apply_button_for_saved_settings(self):
        ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            work_rock_type=self.rock,
            work_dump_point=self.dump_point,
            loading_horizon='125',
            loading_block='4',
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-eo-settings-applied="true"')
        self.assertContains(response, 'disabled aria-disabled="true" aria-label="Настройки уже применены"')
        self.assertContains(response, 'registration && registration.active')
        self.assertNotContains(response, 'newVersion || "new"')
        self.assertNotContains(response, 'registration.active || navigator.serviceWorker.controller')
        self.assertContains(response, reverse('excavator_work_settings'))
        self.assertContains(response, reverse('excavator_shift_action'))
        self.assertContains(response, reverse('excavator_truck_loaded_cancel'))
        self.assertContains(response, 'eo-truck-detail-cards-data')
        self.assertContains(response, 'data-eo-truck-detail')
        self.assertContains(response, 'data-eo-truck-detail-id')
        self.assertContains(response, 'openTruckDetailCard')
        self.assertContains(response, 'truckLongPressTimer')
        self.assertContains(response, 'truck-drag-preview')
        self.assertContains(response, 'data-eo-shift-url')
        self.assertContains(response, 'data-eo-truck-loaded-cancel-url')
        self.assertContains(response, 'data-eo-shift-button')
        self.assertContains(response, 'data-eo-shift-action="close"')
        self.assertContains(response, reverse('logout'))
        self.assertContains(response, 'data-eo-logout-button')
        self.assertContains(response, 'data-eo-logout-url')
        self.assertContains(response, 'data-eo-shift-label')
        self.assertContains(response, '--eo-shift-hold: 0%')
        self.assertContains(response, 'Показатели техники')
        self.assertContains(response, 'Итог смены')
        self.assertNotContains(response, 'eo-shift-face-panel')
        self.assertNotContains(response, 'eo-shift-dump-panel')
        self.assertNotContains(response, 'eo-shift-rock')
        self.assertNotContains(response, 'eo-shift-dump-list')
        self.assertContains(response, 'data-eo-screen="face"')
        self.assertContains(response, 'Забой')
        self.assertContains(response, 'Точки разгрузки')
        self.assertContains(response, 'Назначено')
        self.assertContains(response, 'В пути')
        self.assertContains(response, 'data-eo-shift-fuel')
        self.assertContains(response, '<em>л</em>')
        self.assertContains(response, '<em>км</em>')
        self.assertContains(response, '<em>м/ч</em>')
        self.assertContains(response, 'Факт')
        self.assertContains(response, '0 м³')
        self.assertContains(response, '<em>0 маш.</em>')
        self.assertNotContains(response, 'Факт</span><strong>0 рейс.</strong>')
        self.assertNotContains(response, 'data-eo-open-face-settings')
        self.assertNotContains(response, '+ Добавить')
        self.assertContains(response, 'Удерживайте 2 секунды, чтобы завершить смену')
        self.assertContains(response, 'Удерживайте 2 секунды, чтобы выйти')
        self.assertContains(response, 'data-eo-settings-url')
        self.assertContains(response, 'data-eo-rock-select')
        self.assertContains(response, 'data-eo-dump-points-input')
        self.assertContains(response, 'dump_point_ids')
        self.assertContains(response, 'data-eo-apply-settings')
        self.assertContains(response, 'Удерживайте 2 секунды, чтобы применить настройки')
        self.assertContains(response, 'window.applyOperationalStateRefresh')
        self.assertContains(response, 'refreshExcavatorWorkFromServer')
        self.assertContains(response, 'assignment_changed')
        self.assertContains(response, 'trip_changed')
        self.assertContains(response, 'downtime_changed')
        self.assertContains(response, 'shift_changed')
        self.assertContains(response, 'equipment_changed')
        self.assertContains(response, 'access_changed')
        self.assertContains(response, 'data-eo-pwa-update-modal')
        self.assertContains(response, 'data-eo-pwa-update-badge')
        self.assertContains(response, 'data-eo-refresh-work')
        self.assertContains(response, 'refreshExcavatorWorkFromServer({ preserveTab: true })')
        self.assertContains(response, 'class="eo-dashboard-head"')
        self.assertContains(response, 'class="eo-dashboard-main-zone"')
        self.assertContains(response, 'class="eo-dashboard-dump-zone"')
        self.assertContains(response, 'class="eo-dashboard-plan-widget"')
        self.assertContains(response, 'aria-label="Нет активного плана"')
        self.assertNotContains(response, '<small>Выполнение нормы</small>')
        self.assertContains(response, 'class="eo-topbar-cell eo-shift-kind-cell"')
        self.assertContains(response, 'class="eo-shift-status-text"')
        self.assertContains(response, 'Смена -1')
        self.assertNotContains(response, 'class="eo-sun-icon"')
        self.assertContains(response, 'Гор.125 / Бл.4 / Руда')
        self.assertNotContains(response, 'class="eo-dashboard-info"')
        self.assertNotContains(response, 'Назначенные самосвалы')
        self.assertContains(response, 'data-eo-dashboard-truck')
        self.assertContains(response, 'data-plan-percent=""')
        self.assertContains(response, 'data-plan-loop-percent=""')
        self.assertContains(response, 'data-plan-completed-loops="0"')
        self.assertContains(response, 'data-plan-status="no_active_plan"')
        self.assertContains(response, 'data-plan-status="no_plan_group"')
        self.assertContains(response, 'class="mm-mobile-bottom-nav"')
        self.assertNotContains(response, 'class="bottom-nav"')
        self.assertContains(response, 'class="eo-reason-grid"')
        self.assertNotContains(response, 'class="eo-event-actions"')
        self.assertIn('Перегон', [card['name'] for card in response.context['downtime_reason_cards']])
        self.assertContains(response, 'data-eo-dump-target')
        self.assertContains(response, 'class="eo-truck-grid eo-dashboard-truck-grid is-rows-3"')
        self.assertContains(response, 'addEventListener("pointerdown"')
        self.assertContains(response, 'document.elementFromPoint')
        self.assertContains(response, 'data-eo-dump-queue-modal')
        self.assertContains(response, 'data-eo-dump-queue-gesture-hint')
        self.assertContains(response, 'isReturnReady')
        self.assertContains(response, 'event.clientY <= dialogRect.top + 12')
        self.assertNotContains(response, 'data-eo-dump-return-zone')
        self.assertContains(response, 'truck_loaded_cancel')
        self.assertContains(response, 'Ожидание самосвалов')
        self.assertContains(response, 'Дробилка')
        self.assertContains(response, 'Руда')
        self.assertContains(response, '21')
        self.assertEqual([card['number'] for card in response.context['truck_cards']], ['21'])
        self.assertEqual(response.context['truck_cards'][0]['equipment_state_code'], 'assigned')
        self.assertEqual(response.context['truck_cards'][0]['status_key'], 'blue')
        self.assertEqual(response.context['truck_cards'][0]['status_label'], 'Назначена')
        self.assertIsNone(response.context['truck_cards'][0]['plan_percent'])
        self.assertEqual(response.context['truck_cards'][0]['plan_status'], 'no_plan_group')
        self.assertEqual(response.context['truck_cards'][0]['plan_status_key'], 'no_plan_group')
        self.assertContains(response, 'data-plan-status="no_plan_group"')
        self.assertTrue(response.context['truck_cards'][0]['can_drag'])
        self.assertTrue(response.context['truck_cards'][0]['can_load'])
        self.assertFalse(response.context['truck_cards'][0]['is_locked'])
        self.assertContains(response, 'Назначена')
        self.assertContains(response, 'data-eo-equipment-state="assigned"')
        self.assertContains(response, 'data-eo-can-load="1"')
        self.assertContains(response, 'card.dataset.eoEquipmentState = "loaded_waiting_unload";')
        self.assertNotContains(response, 'ожидает сервер')
        self.assertNotContains(response, 'Под погрузкой')
        self.assertContains(response, 'class="mm-mobile-shift-button is-danger"')

    def test_excavator_progress_cycle_visual_context_preserves_completed_boundaries(self):
        cases = {
            0: (0, 0, 'green'),
            45: (45, 0, 'green'),
            99: (99, 0, 'green'),
            100: (100, 0, 'green'),
            125: (25, 1, 'amber'),
            235: (35, 2, 'cyan'),
            346: (46, 3, 'orange'),
            999: (99, 9, 'orange'),
        }

        for percent, (loop_percent, completed_loops, phase) in cases.items():
            with self.subTest(percent=percent):
                visual = progress_cycle_visual_context(percent)
                self.assertEqual(visual['loop_progress'], loop_percent)
                self.assertEqual(visual['completed_loops'], completed_loops)
                self.assertEqual(visual['phase'], phase)
                self.assertEqual(visual['has_completed_loops'], completed_loops > 0)

    def test_excavator_work_renders_readonly_truck_detail_card_data(self):
        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        detail_cards = response.context['truck_detail_cards']
        detail = detail_cards[str(self.truck.id)]
        self.assertEqual(detail['type'], 'Самосвал')
        self.assertEqual(detail['label'], '21')
        self.assertEqual(detail['status_label'], 'Назначена')
        self.assertEqual(detail['category'], 'truck')
        self.assertTrue(detail['can_load'])
        self.assertTrue(detail['can_drag'])
        self.assertEqual(detail['equipment_state_code'], 'assigned')
        self.assertEqual(detail['color_group'], 'blue')
        self.assertEqual(detail['css_class'], 'status-blue')
        self.assertEqual(detail['load_block_reason_code'], '')
        self.assertEqual(detail['load_block_reason_label'], '')
        detail_labels = {row['label']: row['value'] for row in detail['details']}
        self.assertEqual(detail_labels['Гаражный N'], '21')
        self.assertEqual(detail_labels['Состояние'], 'Назначена')
        self.assertEqual(detail_labels['Доступность'], 'Доступен для погрузки')
        self.assertEqual(detail_labels['Назначение'], 'принято')
        self.assertIn('Рейсы смены', detail_labels)
        self.assertIn('План смены', detail_labels)
        self.assertIn('metrics', detail['shift_report'])
        self.assertContains(response, f'"{self.truck.id}"')
        self.assertContains(response, 'Смена самосвала')

    def test_excavator_work_renders_individual_truck_plan_percent(self):
        loading_shift = EmployeeShift.objects.get(employee=self.operator, equipment=self.excavator)
        opened_at = loading_shift.opened_at
        group = EquipmentPlanGroup.objects.create(
            name='Самосвалы БелАЗ карточка',
            code='belaz-excavator-card-test',
            calculation_mode=PlanCalculationMode.VOLUME,
            plan_value='100.00',
            is_active=True,
        )
        group.equipment.add(self.truck)
        assign_shift_plan_snapshot(self.truck_shift)
        self.truck_shift.refresh_from_db()
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            driver=self.driver,
            loading_shift=loading_shift,
            unloading_shift=self.truck_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            volume_m3='49.40',
            status=TripStatus.COMPLETED,
            completed_at=opened_at,
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        first_card = response.context['truck_cards'][0]
        self.assertEqual(first_card['number'], '21')
        self.assertEqual(first_card['plan_percent'], 49)
        self.assertEqual(first_card['plan_status'], PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(first_card['plan_status_key'], 'low')
        self.assertContains(response, 'data-plan-percent="49"')
        self.assertContains(response, 'data-plan-status="assigned"')
        self.assertContains(response, 'data-plan-progress-status="low"')
        self.assertContains(response, '--eo-truck-progress: 49%;')
        self.assertContains(response, 'data-plan-loop-percent="49"')
        self.assertContains(response, 'data-plan-completed-loops="0"')

    def test_truck_plan_percent_matches_dispatcher_driver_and_excavator(self):
        loading_shift = EmployeeShift.objects.get(employee=self.operator, equipment=self.excavator)
        group = EquipmentPlanGroup.objects.create(
            name='Самосвалы БелАЗ единый процент',
            code='belaz-unified-truck-percent-test',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='20.00',
            is_active=True,
        )
        group.equipment.add(self.truck)
        assign_shift_plan_snapshot(self.truck_shift)
        self.truck_shift.refresh_from_db()
        for index in range(12):
            Trip.objects.create(
                excavator=self.excavator,
                truck=self.truck,
                excavator_operator=self.operator,
                driver=self.driver,
                loading_shift=loading_shift,
                unloading_shift=self.truck_shift,
                rock_type=self.rock,
                dump_point=self.dump_point,
                actual_dump_point=self.dump_point,
                volume_m3='49.40',
                status=TripStatus.COMPLETED,
                completed_at=loading_shift.opened_at + timedelta(minutes=index),
            )

        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Диспетчер')
        dispatcher_access = EmployeeAccess.objects.create(
            employee=dispatcher,
            role=dispatcher_role,
            access_code='500000',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        dispatcher_client = Client()
        dispatcher_session = dispatcher_client.session
        dispatcher_session['employee_access_id'] = dispatcher_access.id
        dispatcher_session.save()
        dispatcher_response = dispatcher_client.get(reverse('dispatcher_control'))

        driver_client = Client()
        driver_session = driver_client.session
        driver_session['employee_access_id'] = self.driver_access.id
        driver_session.save()
        driver_response = driver_client.get(reverse('driver_shift'))
        excavator_response = self.client.get(reverse('excavator_work'))

        self.assertEqual(dispatcher_response.status_code, 200)
        self.assertContains(dispatcher_response, f'data-equipment-id="{self.truck.id}"')
        self.assertContains(dispatcher_response, f'data-equipment-name="{self.truck.garage_number}"')
        self.assertContains(dispatcher_response, 'data-plan-percent="60"')
        self.assertContains(dispatcher_response, 'data-plan-loop-percent="60"')
        self.assertContains(dispatcher_response, 'data-plan-completed-loops="0"')
        self.assertContains(dispatcher_response, '--tile-progress: 60%;')
        self.assertEqual(driver_response.status_code, 200)
        self.assertContains(driver_response, '--driver-progress: 60;')
        self.assertContains(driver_response, 'data-driver-loop-progress="60"')
        self.assertContains(driver_response, 'data-driver-completed-loops="0"')
        self.assertContains(driver_response, '<span class="driver-work-percent">60%</span>', html=False)
        self.assertEqual(excavator_response.status_code, 200)
        first_card = excavator_response.context['truck_cards'][0]
        self.assertEqual(first_card['number'], '21')
        self.assertEqual(first_card['plan_percent'], 60)
        self.assertContains(excavator_response, 'data-plan-percent="60"')
        self.assertContains(excavator_response, 'data-plan-loop-percent="60"')
        self.assertContains(excavator_response, 'data-plan-completed-loops="0"')
        self.assertContains(excavator_response, '--eo-truck-progress: 60%;')

    def test_truck_plan_overrun_cycle_matches_dispatcher_driver_and_excavator(self):
        loading_shift = EmployeeShift.objects.get(employee=self.operator, equipment=self.excavator)
        group = EquipmentPlanGroup.objects.create(
            name='Самосвалы БелАЗ единый цикл перевыполнения',
            code='belaz-unified-overrun-cycle-test',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='8.00',
            is_active=True,
        )
        group.equipment.add(self.truck)
        assign_shift_plan_snapshot(self.truck_shift)
        self.truck_shift.refresh_from_db()
        for index in range(10):
            Trip.objects.create(
                excavator=self.excavator,
                truck=self.truck,
                excavator_operator=self.operator,
                driver=self.driver,
                loading_shift=loading_shift,
                unloading_shift=self.truck_shift,
                rock_type=self.rock,
                dump_point=self.dump_point,
                actual_dump_point=self.dump_point,
                volume_m3='49.40',
                status=TripStatus.COMPLETED,
                completed_at=loading_shift.opened_at + timedelta(minutes=index),
            )

        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Диспетчер')
        dispatcher_access = EmployeeAccess.objects.create(
            employee=dispatcher,
            role=dispatcher_role,
            access_code='500001',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        dispatcher_client = Client()
        dispatcher_session = dispatcher_client.session
        dispatcher_session['employee_access_id'] = dispatcher_access.id
        dispatcher_session.save()
        dispatcher_response = dispatcher_client.get(reverse('dispatcher_control'))

        driver_client = Client()
        driver_session = driver_client.session
        driver_session['employee_access_id'] = self.driver_access.id
        driver_session.save()
        driver_response = driver_client.get(reverse('driver_shift'))
        excavator_response = self.client.get(reverse('excavator_work'))

        self.assertEqual(dispatcher_response.status_code, 200)
        self.assertContains(dispatcher_response, 'data-plan-percent="125"')
        self.assertContains(dispatcher_response, 'data-plan-loop-percent="25"')
        self.assertContains(dispatcher_response, 'data-plan-completed-loops="1"')
        self.assertContains(dispatcher_response, 'data-plan-progress-phase="amber"')
        self.assertContains(dispatcher_response, '--tile-progress: 25%;')
        self.assertContains(dispatcher_response, '×1', html=False)
        self.assertEqual(driver_response.status_code, 200)
        self.assertContains(driver_response, '--driver-progress: 125;')
        self.assertContains(driver_response, '--driver-progress-capped: 100;')
        self.assertContains(driver_response, '--driver-over-progress: 25;')
        self.assertContains(driver_response, 'data-driver-loop-progress="25"')
        self.assertContains(driver_response, 'data-driver-completed-loops="1"')
        self.assertContains(driver_response, 'data-plan-progress-phase="amber"')
        self.assertContains(driver_response, '<span class="driver-work-percent">25%</span>', html=False)
        self.assertContains(driver_response, '×1', html=False)
        self.assertEqual(excavator_response.status_code, 200)
        first_card = excavator_response.context['truck_cards'][0]
        self.assertEqual(first_card['plan_percent'], 125)
        self.assertEqual(first_card['plan_visual']['loop_progress'], 25)
        self.assertEqual(first_card['plan_visual']['completed_loops'], 1)
        self.assertContains(excavator_response, 'data-plan-percent="125"')
        self.assertContains(excavator_response, 'data-plan-loop-percent="25"')
        self.assertContains(excavator_response, 'data-plan-completed-loops="1"')
        self.assertContains(excavator_response, 'data-plan-progress-phase="amber"')
        self.assertContains(excavator_response, '--eo-truck-progress: 25%;')
        self.assertContains(excavator_response, '×1', html=False)

    def test_excavator_work_renders_face_settings_from_server_references(self):
        second_dump = DumpPoint.objects.create(name='Отвал')
        second_rock = RockType.objects.create(name='Негабарит')

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'<option value="{self.rock.id}"', html=False)
        self.assertContains(response, f'<option value="{second_rock.id}" selected', html=False)
        self.assertContains(response, f'data-eo-dump-select="{self.dump_point.id}"')
        self.assertContains(response, f'data-eo-dump-select="{second_dump.id}"')
        self.assertContains(response, f'data-eo-dump-target="{self.dump_point.id}"')
        self.assertNotContains(response, f'data-eo-dump-target="{second_dump.id}"')
        self.assertEqual([card['point'].id for card in response.context['dump_cards']], [self.dump_point.id])
        self.assertEqual(
            {card['point'].id for card in response.context['dump_choice_cards']},
            {self.dump_point.id, second_dump.id},
        )

    def test_excavator_work_settings_save_selected_reference_values(self):
        second_dump = DumpPoint.objects.create(name='Отвал')
        second_rock = RockType.objects.create(name='Негабарит')

        response = self.client.post(
            reverse('excavator_work_settings'),
            data=json.dumps({
                'client_action_id': 'settings-1',
                'rock_type_id': second_rock.id,
                'dump_point_ids': [second_dump.id, self.dump_point.id],
                'loading_horizon': '75a',
                'loading_block': '52-1',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertTrue(payload['ok'])
        self.assertTrue(payload['work_context_changed'])
        self.assertEqual(payload['active_downtime_reason'], 'Перегон экскаватора')
        self.assertEqual(payload['rock_type_id'], second_rock.id)
        self.assertEqual(payload['dump_point_ids'], [second_dump.id, self.dump_point.id])
        self.assertEqual(payload['loading_horizon'], '75')
        self.assertEqual(payload['loading_block'], '521')
        placement = ExcavatorPlacement.objects.get(excavator=self.excavator)
        self.assertEqual(placement.work_rock_type, second_rock)
        self.assertEqual(placement.work_dump_point, second_dump)
        self.assertEqual(placement.loading_horizon, '75')
        self.assertEqual(placement.loading_block, '521')
        self.assertTrue(
            OperationalStateEvent.objects.filter(
                event_type='equipment_changed',
                object_type='Equipment',
                object_id=str(self.excavator.id),
                payload__action='excavator_work_settings',
            ).exists()
        )

        screen = self.client.get(reverse('excavator_work'))
        self.assertEqual(screen.status_code, 200)
        self.assertEqual([card['point'].id for card in screen.context['dump_cards']], [second_dump.id, self.dump_point.id])
        self.assertEqual(screen.context['default_rock'], second_rock.id)
        self.assertEqual(screen.context['face_horizon'], '75')
        self.assertEqual(screen.context['face_block'], '521')
        self.assertContains(screen, f'data-eo-dump-target="{second_dump.id}"')
        self.assertContains(screen, f'data-eo-dump-target="{self.dump_point.id}"')
        self.assertContains(screen, f'value="{second_rock.id}" selected')

        transfer = DowntimeEvent.objects.get(equipment=self.excavator, ended_at__isnull=True)
        self.assertEqual(transfer.reason.name, 'Перегон экскаватора')
        self.assertEqual(transfer.employee, self.operator)

    def test_excavator_work_settings_same_context_does_not_restart_transfer(self):
        settings = {
            'rock_type_id': self.rock.id,
            'dump_point_ids': [self.dump_point.id],
            'loading_horizon': '75',
            'loading_block': '52',
        }
        first_response = self.client.post(
            reverse('excavator_work_settings'),
            data=json.dumps({'client_action_id': 'settings-first', **settings}),
            content_type='application/json',
        )
        transfer = DowntimeEvent.objects.get(equipment=self.excavator, ended_at__isnull=True)
        transfer.ended_at = timezone.now()
        transfer.save(update_fields=['ended_at'])

        second_response = self.client.post(
            reverse('excavator_work_settings'),
            data=json.dumps({'client_action_id': 'settings-same', **settings}),
            content_type='application/json',
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        second_payload = json.loads(second_response.content.decode('utf-8'))
        self.assertFalse(second_payload['work_context_changed'])
        self.assertEqual(second_payload['active_downtime_reason'], '')
        self.assertFalse(DowntimeEvent.objects.filter(equipment=self.excavator, ended_at__isnull=True).exists())

    def test_excavator_work_settings_rejects_inactive_reference_values(self):
        inactive_dump = DumpPoint.objects.create(name='Закрытая точка', is_active=False)

        response = self.client.post(
            reverse('excavator_work_settings'),
            data=json.dumps({
                'client_action_id': 'settings-bad',
                'rock_type_id': self.rock.id,
                'dump_point_ids': [inactive_dump.id],
                'loading_horizon': '75',
                'loading_block': '52',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(OperationalStateEvent.objects.filter(payload__action='excavator_work_settings').count(), 0)

    def test_excavator_shift_action_closes_open_shift_with_meter_values(self):
        shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)

        response = self.client.post(
            reverse('excavator_shift_action'),
            data=json.dumps({
                'action': 'close',
                'client_action_id': 'shift-close-1',
                'fuel': '87.5',
                'mileage': '1234',
                'engine_hours': '1208.25',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['action'], 'shift_closed')
        self.assertFalse(payload['shift_open'])
        shift.refresh_from_db()
        self.assertIsNotNone(shift.closed_at)
        self.assertEqual(str(shift.end_fuel), '87.50')
        self.assertEqual(str(shift.end_mileage), '1234.00')
        self.assertEqual(str(shift.end_engine_hours), '1208.25')
        self.assertTrue(
            OperationalStateEvent.objects.filter(
                event_type='shift_changed',
                object_type='EmployeeShift',
                object_id=str(shift.id),
            ).exists()
        )

    def test_excavator_shift_close_marks_unfinished_loaded_trips_as_carryover(self):
        shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            loading_shift=shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            volume_m3='40.00',
            status=TripStatus.LOADED_WAITING_UNLOAD,
        )

        response = self.client.post(
            reverse('excavator_shift_action'),
            data=json.dumps({
                'action': 'close',
                'client_action_id': 'shift-close-carryover',
                'fuel': '87.5',
                'mileage': '1234',
                'engine_hours': '1208.25',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        trip.refresh_from_db()
        self.assertTrue(trip.is_carryover)

        unloading_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            equipment=self.truck,
            opened_at=timezone.now(),
            opened_by=self.driver,
        )
        finalize_trip_unloaded(trip, driver=self.driver, unloading_shift=unloading_shift)
        trip.refresh_from_db()
        self.assertTrue(trip.is_carryover)

    def test_excavator_shift_summary_uses_exact_open_shift_and_dump_count_uses_current_face(self):
        current_shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        previous_shift = EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            equipment=self.excavator,
            opened_at=current_shift.opened_at - timedelta(hours=8),
            closed_at=current_shift.opened_at - timedelta(minutes=1),
            opened_by=self.operator,
            closed_by=self.operator,
        )
        ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            work_rock_type=self.rock,
            work_dump_point=self.dump_point,
            loading_horizon='75',
            loading_block='52',
        )
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.other_truck,
            excavator_operator=self.operator,
            loading_shift=previous_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            volume_m3='90.00',
            loading_horizon='75',
            loading_block='52',
            status=TripStatus.COMPLETED,
            completed_at=timezone.now(),
        )
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            loading_shift=current_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            volume_m3='40.00',
            loading_horizon='75',
            loading_block='52',
            status=TripStatus.COMPLETED,
            completed_at=timezone.now(),
        )
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.other_truck,
            excavator_operator=self.operator,
            loading_shift=current_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            volume_m3='60.00',
            loading_horizon='90',
            loading_block='60',
            status=TripStatus.COMPLETED,
            completed_at=timezone.now(),
        )
        pending_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='31')
        Trip.objects.create(
            excavator=self.excavator,
            truck=pending_truck,
            excavator_operator=self.operator,
            loading_shift=current_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            volume_m3='30.00',
            loading_horizon='75',
            loading_block='52',
            status=TripStatus.LOADED_WAITING_UNLOAD,
        )
        fallback_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='32')
        Trip.objects.create(
            excavator=self.excavator,
            truck=fallback_truck,
            excavator_operator=self.operator,
            loading_shift=current_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            actual_dump_point=None,
            volume_m3='20.00',
            loading_horizon='75',
            loading_block='52',
            status=TripStatus.COMPLETED,
            completed_at=timezone.now(),
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['shift_fact_value'], '150 м³')
        self.assertEqual(response.context['shift_fact_meta'], '4 маш.')
        dump_card = next(card for card in response.context['dump_cards'] if card['point'] == self.dump_point)
        self.assertEqual(dump_card['completed_count'], 2)

    def test_excavator_shift_action_opens_shift_when_none_is_open(self):
        EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).update(closed_at=timezone.now())
        group = EquipmentPlanGroup.objects.create(
            name='Экскаваторы 4000 endpoint',
            code='excavators-4000-open-test',
            calculation_mode=PlanCalculationMode.VOLUME,
            plan_value='4200.00',
            is_active=True,
        )
        group.equipment.add(self.excavator)

        response = self.client.post(
            reverse('excavator_shift_action'),
            data=json.dumps({
                'action': 'open',
                'client_action_id': 'shift-open-1',
                'excavator_id': self.excavator.id,
                'fuel': '90',
                'mileage': '0',
                'engine_hours': '1210',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['action'], 'shift_opened')
        self.assertTrue(payload['shift_open'])
        shift = EmployeeShift.objects.get(id=payload['shift_id'])
        self.assertEqual(shift.employee, self.operator)
        self.assertEqual(shift.equipment, self.excavator)
        self.assertEqual(str(shift.start_fuel), '90.00')
        self.assertEqual(str(shift.start_mileage), '0.00')
        self.assertEqual(str(shift.start_engine_hours), '1210.00')
        self.assertEqual(shift.plan_status, PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(shift.plan_group_name, 'Экскаваторы 4000 endpoint')
        self.assertEqual(shift.plan_calculation_mode, PlanCalculationMode.VOLUME)
        self.assertEqual(str(shift.plan_value), '4200.00')
        self.assertEqual(payload['plan_status'], 'assigned')
        self.assertEqual(payload['calculation_mode'], 'volume_m3')
        self.assertEqual(payload['plan_value'], '4200.00')

    def test_excavator_shift_open_inherits_previous_equipment_meter_values_when_blank(self):
        previous_shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        previous_shift.end_fuel = '87.50'
        previous_shift.end_mileage = '1234.00'
        previous_shift.end_engine_hours = '1208.25'
        previous_shift.closed_at = timezone.now()
        previous_shift.closed_by = self.operator
        previous_shift.save(update_fields=[
            'end_fuel',
            'end_mileage',
            'end_engine_hours',
            'closed_at',
            'closed_by',
        ])

        screen = self.client.get(reverse('excavator_work'))

        self.assertEqual(screen.status_code, 200)
        self.assertEqual(screen.context['shift_fuel_display'], '87.5')
        self.assertEqual(screen.context['shift_mileage_display'], '1234')
        self.assertEqual(screen.context['shift_engine_hours_display'], '1208.25')

        response = self.client.post(
            reverse('excavator_shift_action'),
            data=json.dumps({
                'action': 'open',
                'client_action_id': 'shift-open-inherit-meters',
                'excavator_id': self.excavator.id,
                'fuel': '',
                'mileage': '',
                'engine_hours': '',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        shift = EmployeeShift.objects.get(id=response.json()['shift_id'])
        self.assertEqual(str(shift.start_fuel), '87.50')
        self.assertEqual(str(shift.start_mileage), '1234.00')
        self.assertEqual(str(shift.start_engine_hours), '1208.25')

    def test_excavator_downtime_reasons_come_from_role_reference_without_limit(self):
        waiting_state = EquipmentState.objects.get(code='waiting')
        created_reasons = []
        for index in range(14):
            created_reasons.append(DowntimeReason.objects.create(
                name=f'Серверная причина простоя {index:02d}',
                short_label=f'П-{index:02d}',
                equipment_type=self.excavator_type if index % 2 else None,
                equipment_state=waiting_state,
                show_for_excavator_operator=True,
                sort_order=500 + index,
            ))
        hidden_reason = DowntimeReason.objects.create(
            name='Скрытая причина для водителя',
            short_label='Скрытая',
            equipment_state=waiting_state,
            show_for_truck_driver=True,
            show_for_excavator_operator=False,
            sort_order=900,
        )
        wrong_type = EquipmentType.objects.create(name='Бульдозер')
        wrong_type_reason = DowntimeReason.objects.create(
            name='Простой другой техники',
            short_label='Не экскаватор',
            equipment_type=wrong_type,
            equipment_state=waiting_state,
            show_for_excavator_operator=True,
            sort_order=901,
        )

        response = self.client.get(reverse('excavator_work'))

        cards = response.context['downtime_reason_cards']
        rendered_ids = {card['reason'].id for card in cards}
        for reason in created_reasons:
            self.assertIn(reason.id, rendered_ids)
            self.assertContains(response, f'data-eo-downtime-reason-id="{reason.id}"')
            self.assertContains(response, 'eo-hold-action')
            self.assertContains(response, f'>{reason.button_label}</button>')
        self.assertNotIn(hidden_reason.id, rendered_ids)
        self.assertNotIn(wrong_type_reason.id, rendered_ids)
        self.assertGreaterEqual(len(cards), len(created_reasons))

    def test_excavator_downtime_reason_uses_button_label_and_equipment_state(self):
        maintenance_state = EquipmentState.objects.get(code='maintenance')
        reason = DowntimeReason.objects.create(
            name='Полное название регламентного обслуживания экскаватора',
            short_label='ТО смены',
            equipment_type=self.excavator_type,
            equipment_state=maintenance_state,
            show_for_excavator_operator=True,
            sort_order=5,
        )

        response = self.client.get(reverse('excavator_work'))

        cards = response.context['downtime_reason_cards']
        card = next(item for item in cards if item['reason'].id == reason.id)
        self.assertEqual(card['name'], 'ТО смены')
        self.assertEqual(card['full_name'], 'Полное название регламентного обслуживания экскаватора')
        self.assertEqual(card['equipment_state_code'], 'maintenance')
        self.assertEqual(card['status_key'], 'orange')
        self.assertContains(response, f'data-eo-downtime-reason-id="{reason.id}"')
        self.assertContains(response, 'data-eo-reason="Полное название регламентного обслуживания экскаватора"')
        self.assertContains(response, 'data-eo-equipment-state="maintenance"')
        self.assertContains(response, 'Удерживайте 2 секунды, чтобы начать простой: ТО смены')
        self.assertContains(response, '>ТО смены</button>')

    def test_excavator_downtime_reason_uses_effective_server_semantics_without_local_red_default(self):
        waiting_reason = DowntimeReason.objects.create(
            name='Плановое ожидание маркшейдера',
            short_label='Ожидание',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
            sort_order=6,
        )
        breakdown_reason = DowntimeReason.objects.create(
            name='Поломка гидравлики',
            short_label='Поломка',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
            sort_order=7,
        )

        response = self.client.get(reverse('excavator_work'))

        cards = response.context['downtime_reason_cards']
        cards_by_id = {item['reason'].id: item for item in cards}
        self.assertEqual(cards_by_id[waiting_reason.id]['equipment_state_code'], waiting_reason.effective_equipment_state_code)
        self.assertEqual(cards_by_id[waiting_reason.id]['status_key'], waiting_reason.effective_color_group)
        self.assertEqual(cards_by_id[waiting_reason.id]['equipment_state_code'], 'waiting')
        self.assertEqual(cards_by_id[waiting_reason.id]['status_key'], 'yellow')
        self.assertEqual(cards_by_id[breakdown_reason.id]['equipment_state_code'], 'breakdown')
        self.assertEqual(cards_by_id[breakdown_reason.id]['status_key'], 'red')

    def test_excavator_active_downtime_uses_reason_effective_color_group(self):
        reason = DowntimeReason.objects.create(
            name='Перегон на новый забой',
            short_label='Перегон',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
            sort_order=8,
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.operator,
            reason=reason,
            started_at=timezone.now() - timedelta(minutes=3),
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.context['active_downtime_state']['code'], reason.effective_equipment_state_code)
        self.assertEqual(response.context['active_downtime_state']['color_group'], reason.effective_color_group)
        self.assertEqual(response.context['active_downtime_state']['code'], 'waiting')
        self.assertEqual(response.context['active_downtime_state']['color_group'], 'yellow')
        self.assertContains(response, 'class="eo-downtime-card status-yellow is-active"')
        self.assertContains(response, 'data-eo-downtime-state="waiting"')
        self.assertContains(response, 'class="eo-hold-action status-yellow is-selected"')

    def test_excavator_work_shift_button_shows_start_style_without_open_shift(self):
        EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).update(closed_at=timezone.now())

        response = self.client.get(reverse('excavator_work'))

        self.assertContains(response, 'class="mm-mobile-shift-button"')
        self.assertNotContains(response, 'class="mm-mobile-shift-button is-danger"')
        self.assertContains(response, 'Начать смену')


    def test_excavator_manifest_is_installable_pwa_manifest(self):
        response = self.client.get(reverse('excavator_manifest'))
        manifest = json.loads(response.content.decode('utf-8'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/manifest+json; charset=utf-8')
        self.assertEqual(manifest['start_url'], reverse('excavator_work'))
        self.assertEqual(manifest['scope'], '/excavator/')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['orientation'], 'portrait')
        self.assertIn('icons', manifest)
        self.assertTrue(any(icon.get('sizes') == '192x192' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('sizes') == '512x512' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('purpose') == 'maskable' for icon in manifest['icons']))

    def test_excavator_service_worker_caches_mobile_shell(self):
        response = self.client.get(reverse('excavator_service_worker'))
        script = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/javascript; charset=utf-8')
        self.assertEqual(response['Service-Worker-Allowed'], '/excavator/')
        self.assertIn('excavator-mobile-shell-v106', script)
        self.assertIn(reverse('excavator_work'), script)
        self.assertIn(reverse('excavator_manifest'), script)
        self.assertIn('/static/js/realtime-client.js', script)
        self.assertIn('/static/css/app.css', script)
        self.assertIn('/static/css/excavator-work-v55.css', script)
        self.assertIn('/static/css/excavator-work-v55-final.css', script)
        self.assertIn('/static/css/excavator-work-v55-shift.css', script)
        self.assertIn('ignoreSearch: true', script)
        self.assertIn('request.headers.get("X-Requested-With") === "XMLHttpRequest"', script)
        self.assertIn('networkOnly(request)', script)
        self.assertIn('SKIP_WAITING', script)
        self.assertIn('GET_VERSION', script)
        self.assertIn('event.ports && event.ports[0]', script)

    def test_excavator_work_hides_pending_assignment_until_driver_accepts(self):
        pending_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='77')
        self.create_registered_driver_shift(
            pending_truck,
            full_name='Водитель 77',
            access_code='200077',
        )
        HaulAssignment.objects.create(
            truck=pending_truck,
            excavator=self.excavator,
            status=AssignmentStatus.PENDING,
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([card['number'] for card in response.context['truck_cards']], ['21'])
        cards_by_number = {card['number']: card for card in response.context['truck_cards']}
        self.assertEqual(cards_by_number['21']['equipment_state_code'], 'assigned')
        self.assertEqual(cards_by_number['21']['status_key'], 'blue')
        self.assertEqual(cards_by_number['21']['status_label'], 'Назначена')
        self.assertTrue(cards_by_number['21']['can_drag'])
        self.assertTrue(cards_by_number['21']['can_load'])

    def test_excavator_work_marks_complex_truck_without_driver_assignment(self):
        no_shift_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='78')
        HaulAssignment.objects.create(
            truck=no_shift_truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        cards_by_number = {card['number']: card for card in response.context['truck_cards']}
        self.assertEqual(cards_by_number['78']['equipment_state_code'], 'no_driver')
        self.assertEqual(cards_by_number['78']['status_key'], 'yellow')
        self.assertEqual(cards_by_number['78']['status_label'], 'Нет водителя')
        self.assertFalse(cards_by_number['78']['can_load'])
        self.assertFalse(cards_by_number['78']['can_drag'])
        self.assertFalse(cards_by_number['78']['is_inactive'])
        self.assertTrue(cards_by_number['78']['is_load_blocked'])
        self.assertEqual(cards_by_number['78']['load_block_reason_code'], 'no_driver')
        self.assertEqual(cards_by_number['78']['load_block_reason_label'], 'Водитель не назначен')
        self.assertContains(response, 'data-eo-equipment-state="no_driver"')
        self.assertContains(response, 'data-eo-can-load="0"')
        self.assertContains(response, 'data-eo-load-block-reason-code="no_driver"')

    def test_excavator_work_marks_driver_assignment_without_open_shift_as_waiting_for_shift(self):
        waiting_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='79')
        driver, _, shift = self.create_registered_driver_shift(
            waiting_truck,
            full_name='Водитель 79',
            access_code='200079',
        )
        shift.closed_at = timezone.now()
        shift.save(update_fields=['closed_at'])
        self.create_driver_assignment(waiting_truck, driver=driver, shift=shift)
        HaulAssignment.objects.create(
            truck=waiting_truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        cards_by_number = {card['number']: card for card in response.context['truck_cards']}
        self.assertEqual(cards_by_number['79']['equipment_state_code'], 'waiting_for_shift')
        self.assertEqual(cards_by_number['79']['status_key'], 'blue')
        self.assertEqual(cards_by_number['79']['status_label'], 'Ожидает смену')
        self.assertFalse(cards_by_number['79']['can_load'])
        self.assertFalse(cards_by_number['79']['can_drag'])
        self.assertFalse(cards_by_number['79']['is_inactive'])
        self.assertTrue(cards_by_number['79']['is_load_blocked'])
        self.assertFalse(cards_by_number['79']['driver_shift_started'])
        self.assertEqual(cards_by_number['79']['load_block_reason_code'], 'driver_shift_not_started')
        self.assertEqual(cards_by_number['79']['load_block_reason_label'], 'Смена водителя не начата')
        self.assertContains(response, 'data-eo-equipment-state="waiting_for_shift"')
        self.assertContains(response, 'data-eo-can-load="0"')
        self.assertContains(response, 'data-eo-load-block-reason-code="driver_shift_not_started"')

    def test_excavator_work_marks_active_trip_truck_as_waiting_unload_state(self):
        active_trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.ACTIVE,
            loading_horizon='12',
            loading_block='1',
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        first_card = response.context['truck_cards'][0]
        self.assertEqual(first_card['number'], str(self.truck.garage_number))
        self.assertEqual(first_card['equipment_state_code'], 'loaded_waiting_unload')
        self.assertEqual(first_card['status_key'], 'green')
        self.assertEqual(first_card['status_label'], 'На разгрузку')
        self.assertEqual(first_card['target_label'], str(active_trip.dump_point))
        self.assertTrue(first_card['is_locked'])
        self.assertFalse(first_card['can_drag'])
        self.assertContains(response, 'class="eo-truck-card eo-dashboard-truck-card status-green is-inactive"')
        self.assertContains(response, 'draggable="false"')
        self.assertContains(response, 'data-eo-equipment-state="loaded_waiting_unload"')
        self.assertContains(response, 'data-eo-truck-inactive="1"')

    def test_excavator_work_locks_truck_with_open_trip_on_other_excavator(self):
        active_trip = Trip.objects.create(
            excavator=self.other_excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loading_horizon='12',
            loading_block='1',
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_trips_count'], 0)
        first_card = response.context['truck_cards'][0]
        self.assertEqual(first_card['number'], str(self.truck.garage_number))
        self.assertEqual(first_card['equipment_state_code'], 'loaded_waiting_unload')
        self.assertEqual(first_card['status_key'], 'green')
        self.assertEqual(first_card['target_label'], str(active_trip.dump_point))
        self.assertTrue(first_card['is_locked'])
        self.assertFalse(first_card['can_drag'])
        self.assertContains(response, 'data-eo-truck-inactive="1"')

    def test_excavator_work_marks_loaded_truck_as_waiting_unload_state(self):
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loading_horizon='12',
            loading_block='1',
        )

        response = self.client.get(reverse('excavator_work'))

        first_card = response.context['truck_cards'][0]
        self.assertEqual(first_card['equipment_state_code'], 'loaded_waiting_unload')
        self.assertEqual(first_card['status_key'], 'green')
        self.assertEqual(first_card['status_label'], 'На разгрузку')
        self.assertTrue(first_card['is_locked'])
        self.assertFalse(first_card['can_drag'])
        self.assertContains(response, 'data-eo-equipment-state="loaded_waiting_unload"')

    def test_excavator_work_rejects_trip_for_pending_assignment(self):
        pending_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='88')
        self.create_registered_driver_shift(
            pending_truck,
            full_name='Водитель 88',
            access_code='200088',
        )
        pending_assignment = HaulAssignment.objects.create(
            truck=pending_truck,
            excavator=self.excavator,
            status=AssignmentStatus.PENDING,
        )

        response = self.client.post(
            reverse('excavator_work'),
            data={
                'assignment': pending_assignment.id,
                'rock_type': self.rock.id,
                'dump_point': self.dump_point.id,
                'loading_horizon': '125',
                'loading_block': '4',
            },
        )

        self.assertEqual(response.status_code, 200)
        pending_assignment.refresh_from_db()
        self.assertEqual(pending_assignment.status, AssignmentStatus.PENDING)
        self.assertIsNone(pending_assignment.accepted_at)
        self.assertFalse(Trip.objects.filter(truck=pending_truck).exists())

    def post_truck_loaded(self, *, client_action_id='load-1', truck=None, dump_point=None, rock=None):
        return self.client.post(
            reverse('excavator_truck_loaded'),
            data=json.dumps({
                'client_action_id': client_action_id,
                'truck_id': (truck or self.truck).id,
                'excavator_id': self.excavator.id,
                'dump_point_id': (dump_point or self.dump_point).id,
                'rock_type': (rock or self.rock).id,
                'loading_horizon': '125',
                'loading_block': '4',
            }),
            content_type='application/json',
        )

    def test_truck_loaded_creates_loaded_waiting_unload_trip(self):
        response = self.post_truck_loaded()

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode('utf-8'))
        trip = Trip.objects.get(id=payload['trip_id'])
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.truck, self.truck)
        self.assertEqual(trip.excavator, self.excavator)
        self.assertEqual(trip.dump_point, self.dump_point)
        self.assertEqual(trip.loading_horizon, '125')
        self.assertEqual(trip.loading_block, '4')
        self.assertIsNone(trip.volume_m3)
        self.assertIsNone(trip.tonnage)
        self.assertTrue(
            TripClientAction.objects.filter(
                action_type='truck_loaded',
                client_action_id='load-1',
                trip=trip,
                actor=self.operator,
            ).exists()
        )

    def test_truck_loaded_closes_transfer_when_another_truck_remains_available(self):
        excavator_shift = EmployeeShift.objects.get(
            employee=self.operator,
            equipment=self.excavator,
            closed_at__isnull=True,
        )
        excavator_shift.opened_at = timezone.now() - timedelta(minutes=10)
        excavator_shift.save(update_fields=['opened_at'])
        second_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            garage_number='22',
        )
        second_driver, _, second_shift = self.create_registered_driver_shift(
            second_truck,
            full_name='Сидоров С.С.',
            access_code='200022',
        )
        self.create_driver_assignment(second_truck, driver=second_driver, shift=second_shift)
        HaulAssignment.objects.create(
            truck=second_truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        transfer_reason = DowntimeReason.objects.get(name='Перегон экскаватора')
        transfer = DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.operator,
            reason=transfer_reason,
            comment='Автоматически по производственному событию',
            started_at=timezone.now() - timedelta(minutes=5),
        )

        response = self.post_truck_loaded(client_action_id='close-transfer')

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertFalse(payload['downtime_status']['active'])
        self.assertGreaterEqual(payload['downtime_status']['shift_total_seconds'], (5 * 60) - 1)
        transfer.refresh_from_db()
        self.assertIsNotNone(transfer.ended_at)
        self.assertFalse(
            DowntimeEvent.objects.filter(
                equipment=self.excavator,
                reason__name='Ожидание самосвалов',
                ended_at__isnull=True,
            ).exists()
        )

    def test_truck_loaded_starts_waiting_when_last_available_truck_is_sent(self):
        response = self.post_truck_loaded(client_action_id='last-loadable-truck')

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertTrue(payload['downtime_status']['active'])
        self.assertEqual(payload['downtime_status']['reason'], 'Ожидание самосвалов')
        self.assertEqual(payload['downtime_status']['status_key'], 'yellow')
        waiting = DowntimeEvent.objects.get(
            equipment=self.excavator,
            reason__name='Ожидание самосвалов',
            ended_at__isnull=True,
        )
        self.assertEqual(waiting.employee, self.operator)
        self.assertEqual(waiting.comment, 'Автоматически по производственному событию')

    def test_excavator_work_restores_waiting_when_no_truck_is_loadable(self):
        self.post_truck_loaded(client_action_id='waiting-before-refresh')
        DowntimeEvent.objects.filter(
            equipment=self.excavator,
            reason__name='Ожидание самосвалов',
            ended_at__isnull=True,
        ).delete()

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        waiting = DowntimeEvent.objects.get(
            equipment=self.excavator,
            reason__name='Ожидание самосвалов',
            ended_at__isnull=True,
        )
        self.assertEqual(waiting.employee, self.operator)
        self.assertEqual(waiting.comment, 'Автоматически по производственному событию')

    def test_trip_unloaded_closes_automatic_waiting_for_trucks(self):
        load_response = self.post_truck_loaded(client_action_id='waiting-unload')
        trip = Trip.objects.get(id=json.loads(load_response.content.decode('utf-8'))['trip_id'])
        waiting = DowntimeEvent.objects.get(
            equipment=self.excavator,
            reason__name='Ожидание самосвалов',
            ended_at__isnull=True,
        )

        finalize_trip_unloaded(trip, driver=self.driver, unloading_shift=self.truck_shift)

        waiting.refresh_from_db()
        self.assertIsNotNone(waiting.ended_at)

    def test_truck_loaded_reuses_same_client_action_id(self):
        first_response = self.post_truck_loaded(client_action_id='same-action')
        second_response = self.post_truck_loaded(client_action_id='same-action')

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        first_payload = json.loads(first_response.content.decode('utf-8'))
        second_payload = json.loads(second_response.content.decode('utf-8'))
        self.assertEqual(first_payload['trip_id'], second_payload['trip_id'])
        self.assertTrue(second_payload['deduplicated'])
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(TripClientAction.objects.count(), 1)

    def test_truck_loaded_rejects_truck_already_waiting_unload(self):
        self.post_truck_loaded(client_action_id='first-load')

        response = self.post_truck_loaded(client_action_id='second-load')

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Trip.objects.count(), 1)

    def test_truck_loaded_rejects_assigned_truck_without_open_driver_shift(self):
        self.truck_shift.closed_at = timezone.now()
        self.truck_shift.save(update_fields=['closed_at'])

        response = self.post_truck_loaded(client_action_id='no-driver-shift')

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['load_block_reason_code'], 'no_driver')
        self.assertEqual(payload['load_block_reason_label'], 'Водитель не назначен')
        self.assertEqual(payload['error'], 'Водитель не назначен')
        self.assertEqual(Trip.objects.count(), 0)

    def test_truck_loaded_rejects_assigned_driver_without_open_shift(self):
        self.create_driver_assignment(self.truck, driver=self.driver, shift=self.truck_shift)
        self.truck_shift.closed_at = timezone.now()
        self.truck_shift.save(update_fields=['closed_at'])

        response = self.post_truck_loaded(client_action_id='driver-shift-not-started')

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['load_block_reason_code'], 'driver_shift_not_started')
        self.assertEqual(payload['load_block_reason_label'], 'Смена водителя не начата')
        self.assertEqual(payload['error'], 'Смена водителя не начата')
        self.assertEqual(Trip.objects.count(), 0)

    def test_truck_loaded_rejects_assigned_truck_with_active_downtime(self):
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=self.reason,
            started_at=timezone.now() - timedelta(minutes=5),
        )

        response = self.post_truck_loaded(client_action_id='truck-downtime')

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertFalse(payload['ok'])
        self.assertIn('простое', payload['error'])
        self.assertEqual(Trip.objects.count(), 0)

    def test_truck_loaded_publishes_operational_state_event(self):
        response = self.post_truck_loaded(client_action_id='event-load')

        self.assertEqual(response.status_code, 200)
        trip = Trip.objects.get()
        event = OperationalStateEvent.objects.filter(
            event_type='trip_changed',
            object_type='Trip',
            object_id=str(trip.id),
            reason='Trip:truck_loaded',
            payload__action='truck_loaded',
            payload__status=TripStatus.LOADED_WAITING_UNLOAD,
        ).first()
        self.assertIsNotNone(event)

    def test_dispatcher_assigned_truck_load_unload_cycle_returns_available_to_excavator(self):
        kkd = DumpPoint.objects.create(name='ККД')
        assignment = HaulAssignment.objects.get(truck=self.truck, excavator=self.excavator)
        start_response = self.client.get(reverse('excavator_work'))
        cards_by_number = {card['number']: card for card in start_response.context['truck_cards']}
        self.assertEqual(cards_by_number['21']['equipment_state_code'], 'assigned')
        self.assertTrue(cards_by_number['21']['can_drag'])

        load_response = self.post_truck_loaded(
            client_action_id='cycle-load',
            dump_point=kkd,
        )

        self.assertEqual(load_response.status_code, 200)
        load_payload = json.loads(load_response.content.decode('utf-8'))
        trip = Trip.objects.get(id=load_payload['trip_id'])
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.assigned_dump_point, kkd)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)

        driver_client = self.client_class()
        driver_session = driver_client.session
        driver_session['employee_access_id'] = self.driver_access.id
        driver_session.save()
        driver_response = driver_client.get(reverse('driver_work'))

        self.assertEqual(driver_response.status_code, 200)
        self.assertEqual(driver_response.context['active_trip'], trip)
        self.assertEqual(driver_response.context['driver_status'], 'ЗАГРУЖЕН')
        self.assertEqual(str(driver_response.context['driver_dial_label']), 'ККД')

        complete_response = driver_client.post(
            reverse('driver_complete_trip', args=[trip.id]),
            data={'client_action_id': 'cycle-unload'},
        )

        self.assertEqual(complete_response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, self.driver)
        self.assertEqual(trip.unloading_shift, self.truck_shift)

        finish_response = self.client.get(reverse('excavator_work'))
        finish_cards_by_number = {card['number']: card for card in finish_response.context['truck_cards']}
        self.assertEqual(finish_cards_by_number['21']['equipment_state_code'], 'assigned')
        self.assertTrue(finish_cards_by_number['21']['can_drag'])

    def test_truck_loaded_cancel_returns_truck_to_assigned_state(self):
        load_response = self.post_truck_loaded(client_action_id='cancel-load')
        trip = Trip.objects.get(id=json.loads(load_response.content.decode('utf-8'))['trip_id'])

        response = self.post_truck_loaded_cancel(trip)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode('utf-8'))
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertEqual(payload['equipment_state'], 'assigned')
        self.assertEqual(payload['status'], TripStatus.CANCELLED)
        self.assertTrue(
            TripClientAction.objects.filter(
                action_type='truck_loaded_cancel',
                client_action_id='cancel-1',
                trip=trip,
                actor=self.operator,
            ).exists()
        )
        self.assertFalse(
            DowntimeEvent.objects.filter(
                equipment=self.excavator,
                reason__name='Ожидание самосвалов',
                ended_at__isnull=True,
            ).exists()
        )

    def test_truck_loaded_cancel_reuses_same_client_action_id(self):
        load_response = self.post_truck_loaded(client_action_id='cancel-same-load')
        trip = Trip.objects.get(id=json.loads(load_response.content.decode('utf-8'))['trip_id'])

        first_response = self.post_truck_loaded_cancel(trip, client_action_id='same-cancel')
        second_response = self.post_truck_loaded_cancel(trip, client_action_id='same-cancel')

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        second_payload = json.loads(second_response.content.decode('utf-8'))
        self.assertTrue(second_payload['deduplicated'])
        self.assertEqual(TripClientAction.objects.filter(action_type='truck_loaded_cancel').count(), 1)

    def test_truck_loaded_cancel_publishes_operational_state_event(self):
        load_response = self.post_truck_loaded(client_action_id='cancel-event-load')
        trip = Trip.objects.get(id=json.loads(load_response.content.decode('utf-8'))['trip_id'])

        response = self.post_truck_loaded_cancel(trip, client_action_id='cancel-event')

        self.assertEqual(response.status_code, 200)
        event = OperationalStateEvent.objects.filter(
            event_type='trip_changed',
            object_type='Trip',
            object_id=str(trip.id),
            reason='Trip:truck_loaded_cancel',
            payload__action='truck_loaded_cancel',
            payload__status=TripStatus.CANCELLED,
        ).first()
        self.assertIsNotNone(event)

    def test_excavator_downtime_action_creates_and_closes_downtime_event(self):
        start_response = self.client.post(
            reverse('excavator_downtime_action'),
            data=json.dumps({
                'action': 'start',
                'reason_id': self.reason.id,
                'client_action_id': 'test-start',
            }),
            content_type='application/json',
        )

        self.assertEqual(start_response.status_code, 200)
        start_payload = json.loads(start_response.content.decode('utf-8'))
        event = DowntimeEvent.objects.get(equipment=self.excavator)
        self.assertEqual(event.reason, self.reason)
        self.assertEqual(event.employee, self.operator)
        self.assertIsNone(event.ended_at)
        self.assertEqual(start_payload['action'], 'downtime_started')
        self.assertTrue(start_payload['active'])
        self.assertEqual(start_payload['equipment_state_code'], 'waiting')
        self.assertEqual(start_payload['status_key'], 'yellow')
        self.assertIn('started_at', start_payload)
        self.assertIn('elapsed_seconds', start_payload)

        close_response = self.client.post(
            reverse('excavator_downtime_action'),
            data=json.dumps({'action': 'close', 'client_action_id': 'test-close'}),
            content_type='application/json',
        )
        event.refresh_from_db()

        self.assertEqual(close_response.status_code, 200)
        close_payload = json.loads(close_response.content.decode('utf-8'))
        self.assertIsNotNone(event.ended_at)
        self.assertEqual(close_payload['action'], 'downtime_closed')
        self.assertFalse(close_payload['active'])
        self.assertIn('elapsed_seconds', close_payload)
        self.assertIn('shift_total_seconds', close_payload)
        self.assertIn('shift_total_label', close_payload)

    def test_excavator_downtime_action_rejects_reason_outside_role_reference(self):
        wrong_type = EquipmentType.objects.create(name='Погрузчик')
        wrong_reason = DowntimeReason.objects.create(
            name='Причина другого рабочего места',
            short_label='Другая',
            equipment_type=wrong_type,
            show_for_excavator_operator=True,
            sort_order=700,
        )

        response = self.client.post(
            reverse('excavator_downtime_action'),
            data=json.dumps({
                'action': 'start',
                'reason_id': wrong_reason.id,
                'client_action_id': 'wrong-reason',
            }),
            content_type='application/json',
        )

    def post_truck_loaded_cancel(self, trip, *, client_action_id='cancel-1'):
        return self.client.post(
            reverse('excavator_truck_loaded_cancel'),
            data=json.dumps({
                'client_action_id': client_action_id,
                'truck_id': trip.truck_id,
                'trip_id': trip.id,
                'dump_point_id': trip.assigned_dump_point_id or trip.actual_dump_point_id or trip.dump_point_id,
            }),
            content_type='application/json',
        )

        payload = json.loads(response.content.decode('utf-8'))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload['ok'])
        self.assertIn('недоступна', payload['error'])
        self.assertFalse(DowntimeEvent.objects.filter(equipment=self.excavator, reason=wrong_reason).exists())

    def test_excavator_downtime_action_publishes_state_payload_for_realtime(self):
        response = self.client.post(
            reverse('excavator_downtime_action'),
            data=json.dumps({
                'action': 'start',
                'reason_id': self.reason.id,
                'client_action_id': 'test-realtime-start',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        event = DowntimeEvent.objects.get(equipment=self.excavator)
        realtime_event = OperationalStateEvent.objects.filter(
            event_type='downtime_changed',
            object_type='DowntimeEvent',
            object_id=str(event.id),
            payload__action='downtime_started',
            payload__equipment_id=self.excavator.id,
            payload__equipment_state_code='waiting',
        ).first()
        self.assertIsNotNone(realtime_event)

    def test_excavator_events_screen_shows_elapsed_downtime_timer(self):
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.operator,
            reason=self.reason,
            started_at=timezone.now() - timedelta(minutes=12),
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertContains(response, 'data-eo-active-duration')
        self.assertContains(response, 'data-eo-active-started-at=')
        self.assertContains(response, 'data-eo-downtime-state="waiting"')
        self.assertContains(response, 'startDowntimeTimer')
        self.assertContains(response, '>00:')
        self.assertNotContains(response, 'eo-downtime-clock')

    def test_excavator_events_screen_shows_current_shift_downtime_total_when_inactive(self):
        shift_start = timezone.now() - timedelta(hours=2)
        excavator_shift = EmployeeShift.objects.get(employee=self.operator, equipment=self.excavator, closed_at__isnull=True)
        excavator_shift.opened_at = shift_start
        excavator_shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Сменный итог простоя',
            equipment_type=self.excavator.equipment_type,
            show_for_excavator_operator=True,
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.operator,
            reason=reason,
            started_at=shift_start + timedelta(minutes=10),
            ended_at=shift_start + timedelta(minutes=25),
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.context['shift_downtime_total_seconds'], 15 * 60)
        self.assertContains(response, 'data-eo-shift-downtime-seconds="900"')
        self.assertContains(response, '>00:15:00</b>')
        self.assertNotContains(response, '>активен<')

    def test_excavator_close_downtime_button_disabled_without_active_downtime(self):
        response = self.client.get(reverse('excavator_work'))

        self.assertContains(response, 'data-eo-close-event')
        self.assertContains(response, 'disabled aria-disabled="true"')
        self.assertContains(response, 'eo-primary-action')
        self.assertContains(response, 'is-disabled')
        self.assertContains(response, 'data-eo-hold-label="Завершить простой"')

    def test_excavator_close_downtime_button_enabled_with_active_downtime(self):
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.operator,
            reason=self.reason,
            started_at=timezone.now() - timedelta(minutes=7),
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertContains(response, 'data-eo-close-event')
        self.assertNotContains(response, 'data-eo-close-event disabled aria-disabled="true"')
        self.assertNotContains(response, 'eo-primary-action is-disabled')
        self.assertContains(response, 'Удерживайте 2 секунды, чтобы завершить простой')


class DispatcherAssignmentRealtimeTests(TestCase):
    def setUp(self):
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер тест',
            phone='79000000600',
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='600000',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            opened_at=timezone.now(),
            opened_by=self.dispatcher,
        )
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck_model = EquipmentModel.objects.create(equipment_type=truck_type, name='Самосвал тест')
        excavator_model = EquipmentModel.objects.create(equipment_type=excavator_type, name='Экскаватор тест')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='201',
            is_active=True,
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number='Э-201',
            is_active=True,
        )
        ExcavatorPlacement.objects.create(excavator=self.excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
        HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.ACCEPTED,
        )
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()

    def test_release_complex_emits_assignment_changed_event_without_moving_excavator(self):
        response = self.client.post(
            reverse('dispatcher_assign_truck'),
            data=json.dumps({
                'action': 'release_complex',
                'excavator_id': self.excavator.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(response.json()['scheduled'], 1)
        self.assertTrue(
            HaulAssignment.objects
            .filter(
                excavator=self.excavator,
                action=HaulAssignmentAction.RELEASE,
                status=AssignmentStatus.PENDING,
                ended_at__isnull=True,
            )
            .exists()
        )
        self.assertTrue(
            ExcavatorPlacement.objects
            .filter(excavator=self.excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
            .exists()
        )
        event = OperationalStateEvent.objects.filter(
            event_type='assignment_changed',
            reason='HaulAssignment:release_pending',
            payload__action='release_pending',
        ).latest('version')
        self.assertEqual(event.payload['excavator_ids'], [self.excavator.id])
        self.assertEqual(event.payload['truck_ids'], [self.truck.id])

    def test_dispatcher_control_renders_duplicate_active_truck_assignment_once(self):
        HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.PENDING,
        )

        response = self.client.get(reverse('dispatcher_control'))
        content = response.content.decode('utf-8')
        truck_marker = (
            f'data-dispatcher-drag="truck" '
            f'data-equipment-card-id="{self.truck.id}" '
            f'data-equipment-id="{self.truck.id}"'
        )

        self.assertEqual(content.count(truck_marker), 1)

    def test_dispatcher_assign_truck_reuses_same_pending_action(self):
        pending = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.dispatcher,
            status=AssignmentStatus.PENDING,
        )

        response = self.client.post(
            reverse('dispatcher_assign_truck'),
            data=json.dumps({
                'action': 'assign',
                'truck_id': self.truck.id,
                'excavator_id': self.excavator.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        active_assignments = (
            HaulAssignment.objects
            .filter(truck=self.truck, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
        )
        self.assertFalse(response.json()['created'])
        self.assertEqual(active_assignments.count(), 2)
        self.assertEqual(response.json()['assignment_id'], pending.id)
        self.assertTrue(active_assignments.filter(status=AssignmentStatus.ACCEPTED).exists())
        self.assertTrue(active_assignments.filter(id=pending.id, status=AssignmentStatus.PENDING).exists())
