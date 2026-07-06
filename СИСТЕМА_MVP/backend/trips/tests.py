import json
from datetime import timedelta
from pathlib import Path

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from core.models import OperationalStateEvent
from downtimes.models import DowntimeEvent, DowntimeReason
from references.equipment_states import upsert_default_equipment_states
from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentState, EquipmentType, RockType
from shifts.models import EmployeeShift
from trips.models import Trip, TripClientAction, TripStatus
from trips.views import build_dispatcher_dashboard_context
from users.models import Employee, EmployeeAccess, Role


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
        self.assertIn('dispatcher-desktop-shell-v22', script)
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
            open_shifts=[],
            open_mechanic_downtimes=DowntimeEvent.objects.filter(ended_at__isnull=True),
            trucks=Equipment.objects.filter(equipment_type=self.truck_type).order_by('garage_number'),
            excavators=Equipment.objects.filter(equipment_type=self.excavator_type).order_by('garage_number'),
            recent_dispatcher_actions=[],
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

        self.assertEqual(trucks_by_name['10']['status'], 'yellow')
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


class ExcavatorWorkServerIntegrationTests(TestCase):
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
        self.assertContains(response, '/static/css/excavator-work-v41.css')
        self.assertContains(response, '/excavator-sw.js')
        self.assertContains(response, 'scope: "/excavator/"')
        self.assertContains(response, 'excavator-mobile-shell-v41')
        self.assertContains(response, 'resolveExcavatorUpdateVersion')
        self.assertContains(response, 'renderUpdateModal')
        self.assertContains(response, 'fetchServerVersion')
        self.assertContains(response, 'normalizeExcavatorVersion')
        self.assertContains(response, 'data-eo-pwa-update-check')
        self.assertContains(response, 'data-eo-pwa-update-check-label')
        self.assertContains(response, 'data-eo-pwa-update-check-version')
        self.assertContains(response, 'runManualUpdateCheck')
        self.assertContains(response, 'Проверка...')
        self.assertContains(response, 'registration && registration.active')
        self.assertNotContains(response, 'newVersion || "new"')
        self.assertNotContains(response, 'registration.active || navigator.serviceWorker.controller')
        self.assertContains(response, reverse('excavator_work_settings'))
        self.assertContains(response, reverse('excavator_shift_action'))
        self.assertContains(response, 'data-eo-shift-url')
        self.assertContains(response, 'data-eo-shift-button')
        self.assertContains(response, 'data-eo-shift-action="close"')
        self.assertContains(response, reverse('logout'))
        self.assertContains(response, 'data-eo-logout-button')
        self.assertContains(response, 'data-eo-logout-url')
        self.assertContains(response, 'data-eo-shift-label')
        self.assertContains(response, '--eo-shift-hold: 0%')
        self.assertContains(response, 'Показатели техники')
        self.assertContains(response, 'Точки разгрузки')
        self.assertContains(response, 'eo-shift-rock')
        self.assertContains(response, 'Зафиксировано')
        self.assertContains(response, 'Итог смены')
        self.assertContains(response, 'Назначено')
        self.assertContains(response, 'В пути')
        self.assertContains(response, 'data-eo-shift-fuel')
        self.assertContains(response, '<em>л</em>')
        self.assertContains(response, '<em>км</em>')
        self.assertContains(response, '<em>м/ч</em>')
        self.assertContains(response, 'Отгружено')
        self.assertContains(response, '0 маш.')
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
        self.assertContains(response, 'data-eo-dashboard-truck')
        self.assertContains(response, 'class="mm-mobile-bottom-nav"')
        self.assertNotContains(response, 'class="bottom-nav"')
        self.assertContains(response, 'class="eo-reason-grid"')
        self.assertNotContains(response, 'class="eo-event-actions"')
        self.assertIn('Перегон', [card['name'] for card in response.context['downtime_reason_cards']])
        self.assertContains(response, 'data-eo-dump-target')
        self.assertContains(response, 'class="eo-truck-grid eo-dashboard-truck-grid is-single"')
        self.assertContains(response, 'addEventListener("pointerdown"')
        self.assertContains(response, 'document.elementFromPoint')
        self.assertContains(response, 'Ожидание самосвалов')
        self.assertContains(response, 'Дробилка')
        self.assertContains(response, 'Руда')
        self.assertContains(response, '21')
        self.assertEqual([card['number'] for card in response.context['truck_cards']], ['21'])
        self.assertEqual(response.context['truck_cards'][0]['equipment_state_code'], 'assigned')
        self.assertEqual(response.context['truck_cards'][0]['status_key'], 'blue')
        self.assertEqual(response.context['truck_cards'][0]['status_label'], 'Назначена')
        self.assertTrue(response.context['truck_cards'][0]['can_drag'])
        self.assertFalse(response.context['truck_cards'][0]['is_locked'])
        self.assertContains(response, 'Назначена')
        self.assertContains(response, 'data-eo-equipment-state="assigned"')
        self.assertContains(response, 'card.dataset.eoEquipmentState = "loaded_waiting_unload";')
        self.assertNotContains(response, 'ожидает сервер')
        self.assertNotContains(response, 'Под погрузкой')
        self.assertContains(response, 'class="mm-mobile-shift-button is-danger"')

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
        self.assertEqual(payload['rock_type_id'], second_rock.id)
        self.assertEqual(payload['dump_point_ids'], [second_dump.id, self.dump_point.id])
        self.assertEqual(payload['loading_horizon'], '75')
        self.assertEqual(payload['loading_block'], '521')
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

    def test_excavator_shift_action_opens_shift_when_none_is_open(self):
        EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).update(closed_at=timezone.now())

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
        self.assertIn('excavator-mobile-shell-v41', script)
        self.assertIn(reverse('excavator_work'), script)
        self.assertIn(reverse('excavator_manifest'), script)
        self.assertIn('/static/js/realtime-client.js', script)
        self.assertIn('/static/css/app.css', script)
        self.assertIn('/static/css/excavator-work-v41.css', script)
        self.assertIn('ignoreSearch: true', script)
        self.assertIn('request.headers.get("X-Requested-With") === "XMLHttpRequest"', script)
        self.assertIn('networkOnly(request)', script)
        self.assertIn('SKIP_WAITING', script)
        self.assertIn('GET_VERSION', script)
        self.assertIn('event.ports && event.ports[0]', script)

    def test_excavator_work_displays_pending_assignment_from_dispatcher(self):
        pending_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='77')
        HaulAssignment.objects.create(
            truck=pending_truck,
            excavator=self.excavator,
            status=AssignmentStatus.PENDING,
        )

        response = self.client.get(reverse('excavator_work'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '77')
        self.assertEqual([card['number'] for card in response.context['truck_cards']], ['21', '77'])
        cards_by_number = {card['number']: card for card in response.context['truck_cards']}
        self.assertEqual(cards_by_number['21']['equipment_state_code'], 'assigned')
        self.assertEqual(cards_by_number['21']['status_key'], 'blue')
        self.assertEqual(cards_by_number['21']['status_label'], 'Назначена')
        self.assertTrue(cards_by_number['21']['can_drag'])
        self.assertEqual(cards_by_number['77']['equipment_state_code'], 'waiting')
        self.assertEqual(cards_by_number['77']['status_key'], 'yellow')
        self.assertEqual(cards_by_number['77']['status_label'], 'Ожидает')
        self.assertFalse(cards_by_number['77']['can_drag'])

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

    def test_excavator_work_starts_trip_and_accepts_pending_assignment(self):
        pending_truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='88')
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

        self.assertEqual(response.status_code, 302)
        pending_assignment.refresh_from_db()
        self.assertEqual(pending_assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNotNone(pending_assignment.accepted_at)

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
        self.assertEqual(response.json()['closed'], 1)
        self.assertFalse(
            HaulAssignment.objects
            .filter(excavator=self.excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
        self.assertTrue(
            ExcavatorPlacement.objects
            .filter(excavator=self.excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
            .exists()
        )
        event = OperationalStateEvent.objects.filter(
            event_type='assignment_changed',
            reason='HaulAssignment:bulk_close',
            payload__action='release_complex',
        ).latest('version')
        self.assertEqual(event.payload['closed_count'], 1)
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

    def test_dispatcher_assign_truck_closes_all_previous_active_assignments(self):
        HaulAssignment.objects.create(
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
        self.assertEqual(active_assignments.count(), 1)
        self.assertEqual(active_assignments.get().id, response.json()['assignment_id'])
        self.assertFalse(
            HaulAssignment.objects
            .filter(truck=self.truck)
            .exclude(id=response.json()['assignment_id'])
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
