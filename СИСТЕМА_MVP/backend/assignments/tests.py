import json
from datetime import timedelta

from django.test import TestCase

from django.urls import reverse
from django.utils import timezone

from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentState, EquipmentType, RockType
from shifts.models import EmployeeShift, EquipmentPlanGroup, PlanCalculationMode
from shifts.services import assign_shift_plan_snapshot
from trips.models import Trip, TripStatus
from users.models import Employee, EmployeeAccess, Role

from .models import AssignmentStatus, ExcavatorPlacement, HaulAssignment, HaulAssignmentAction
from .services import apply_pending_haul_assignment, reconcile_due_haul_assignments, schedule_haul_assignment, schedule_haul_release
from .views import build_excavator_tile, build_truck_tile


class MiningMasterAssignmentsViewTests(TestCase):
    def setUp(self):
        self.master_role = Role.objects.create(code='mining_master', name='Горный мастер')
        self.master = Employee.objects.create(
            full_name='Горный мастер Тест',
            phone='79000000400',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.master,
            role=self.master_role,
            access_code='400000',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck_model = EquipmentModel.objects.create(equipment_type=truck_type, name='БЕЛАЗ тест')
        excavator_model = EquipmentModel.objects.create(equipment_type=excavator_type, name='Экскаватор тест')
        self.waiting_state = EquipmentState.objects.create(
            code='waiting',
            name='Ожидает',
            short_label='Ожидает',
            color_group='yellow',
            semantic_group='availability',
        )
        self.maintenance_state = EquipmentState.objects.create(
            code='maintenance',
            name='Техническое обслуживание',
            short_label='ТО',
            color_group='orange',
            semantic_group='technical',
        )
        self.assigned_truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='101',
            is_active=True,
        )
        self.free_truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='102',
            is_active=True,
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number='Э-1',
            is_active=True,
        )
        self.other_excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number='Э-2',
            is_active=True,
        )
        ExcavatorPlacement.objects.create(excavator=self.excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
        ExcavatorPlacement.objects.create(excavator=self.other_excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
        HaulAssignment.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.PENDING,
        )
        self.shift = EmployeeShift.objects.create(
            employee=self.master,
            shift_type='day',
            opened_at=timezone.now(),
            opened_by=self.master,
        )
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()

    def test_legacy_tiles_accept_server_orange_status(self):
        truck_tile = build_truck_tile(self.free_truck, 'orange', 'ТО')
        excavator_tile = build_excavator_tile(self.excavator, 'orange', 'Ремонт')

        self.assertEqual(truck_tile['status_key'], 'orange')
        self.assertEqual(excavator_tile['status_key'], 'orange')
        self.assertEqual(truck_tile['icon'], 'img/equipment/truck-yellow.png')
        self.assertEqual(excavator_tile['icon'], 'img/equipment/excavator-yellow.png')

    def test_mining_master_screen_renders_dispatcher_pult_copy(self):
        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'dispatcher-control-screen')
        self.assertContains(response, 'dispatcher-board')
        self.assertContains(response, 'ГОРНЫЙ МАСТЕР')
        self.assertContains(response, reverse('mining_master_move_excavator'))
        self.assertContains(response, reverse('mining_master_assign_truck'))
        self.assertContains(response, 'data-dispatcher-drag="truck"')
        self.assertContains(response, 'data-dispatcher-drop="complex"')
        self.assertContains(response, 'data-dispatcher-drop="truck-garage"')
        self.assertNotContains(response, reverse('dispatcher_move_excavator'))
        self.assertNotContains(response, reverse('dispatcher_assign_truck'))
        self.assertNotContains(response, 'mining-master-clean-slate')
        self.assertNotContains(response, 'data-mm-tab')
        self.assertNotContains(response, 'data-mm-panel')
        self.assertEqual(len(response.context['dispatcher_dashboard']['complex_zones']), 9)

    def test_mining_master_truck_plan_overrun_uses_progress_cycle_contract(self):
        excavator_group = EquipmentPlanGroup.objects.create(
            name='Экскаваторы Горный мастер цикл',
            code='mm-excavator-cycle-test',
            calculation_mode=PlanCalculationMode.VOLUME,
            plan_value='40.00',
            is_active=True,
        )
        excavator_group.equipment.add(self.excavator)
        operator = Employee.objects.create(full_name='Машинист цикла')
        excavator_shift = EmployeeShift.objects.create(
            employee=operator,
            shift_type='day',
            equipment=self.excavator,
            opened_at=self.shift.opened_at,
            opened_by=operator,
        )
        assign_shift_plan_snapshot(excavator_shift)
        group = EquipmentPlanGroup.objects.create(
            name='Самосвалы Горный мастер цикл',
            code='mm-truck-cycle-test',
            calculation_mode=PlanCalculationMode.TRIPS,
            plan_value='4.00',
            is_active=True,
        )
        group.equipment.add(self.assigned_truck)
        driver = Employee.objects.create(full_name='Водитель цикла')
        truck_shift = EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=self.assigned_truck,
            opened_at=self.shift.opened_at,
            opened_by=driver,
        )
        assign_shift_plan_snapshot(truck_shift)
        rock_type = RockType.objects.create(name='Руда цикла')
        dump_point = DumpPoint.objects.create(name='Отвал цикла')
        for index in range(5):
            Trip.objects.create(
                excavator=self.excavator,
                truck=self.assigned_truck,
                loading_shift=excavator_shift,
                unloading_shift=truck_shift,
                rock_type=rock_type,
                dump_point=dump_point,
                volume_m3='10.00',
                status=TripStatus.COMPLETED,
                created_at=self.shift.opened_at + timedelta(minutes=index),
                completed_at=self.shift.opened_at + timedelta(minutes=index + 1),
            )

        response = self.client.get(reverse('mining_master_assignments'))
        complex_card = next(
            card
            for card in response.context['dispatcher_dashboard']['complex_zones']
            if card['id'] == 'K-1'
        )
        truck_tile = next(
            tile
            for tile in complex_card['active_truck_tiles']
            if tile['card_id'] == str(self.assigned_truck.id)
        )

        self.assertEqual(complex_card['percent'], 125)
        self.assertEqual(complex_card['plan_visual']['loop_progress'], 25)
        self.assertEqual(complex_card['plan_visual']['completed_loops'], 1)
        self.assertEqual(complex_card['plan_visual']['phase'], 'amber')
        self.assertEqual(truck_tile['percent'], 125)
        self.assertEqual(truck_tile['plan_visual']['loop_progress'], 25)
        self.assertEqual(truck_tile['plan_visual']['completed_loops'], 1)
        self.assertEqual(truck_tile['plan_visual']['phase'], 'amber')
        self.assertContains(response, 'data-plan-percent="125"')
        self.assertContains(response, 'data-plan-loop-percent="25"')
        self.assertContains(response, 'data-plan-completed-loops="1"')
        self.assertContains(response, 'data-plan-progress-phase="amber"')
        self.assertContains(response, 'is-plan-overrun')
        self.assertContains(response, 'dispatcher-plan-loop-badge')
        self.assertContains(response, 'mm-mobile-complex-plan-ring')
        self.assertContains(response, 'mm-mobile-plan-ring-layer')
        self.assertContains(response, 'function syncMobilePlanVisual(source, target)')
        self.assertContains(response, 'syncMobilePlanVisual(source, mini);')
        html = response.content.decode('utf-8')
        mobile_complex_marker = f'data-mm-mobile-open-complex="{complex_card["card_id"]}"'
        mobile_complex_index = html.index(mobile_complex_marker)
        mobile_complex_tag = html[html.rfind('<article', 0, mobile_complex_index):html.index('>', mobile_complex_index) + 1]
        self.assertIn('has-plan-progress', mobile_complex_tag)
        self.assertIn('is-plan-overrun', mobile_complex_tag)
        self.assertIn('data-plan-percent="125"', mobile_complex_tag)
        self.assertIn('data-plan-loop-percent="25"', mobile_complex_tag)
        mobile_truck_marker = f'data-mm-mobile-home-truck-id="{self.assigned_truck.id}"'
        mobile_truck_index = html.index(mobile_truck_marker)
        mobile_truck_tag = html[html.rfind('<span', 0, mobile_truck_index):html.index('>', mobile_truck_index) + 1]
        self.assertIn('has-plan-progress', mobile_truck_tag)
        self.assertIn('is-plan-overrun', mobile_truck_tag)
        self.assertIn('data-plan-percent="125"', mobile_truck_tag)
        self.assertIn('data-plan-loop-percent="25"', mobile_truck_tag)
        self.assertIn('data-plan-completed-loops="1"', mobile_truck_tag)
        self.assertContains(response, '×1', html=False)

    def test_mining_master_desktop_realtime_refresh_uses_rendered_board(self):
        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'function isElementRendered(node)')
        self.assertContains(response, 'return isElementRendered(document.querySelector(".dispatcher-board"));')
        self.assertContains(response, 'function isMiningMasterMobilePage()')
        self.assertContains(response, 'function captureDispatcherDesktopState(currentBoard)')
        self.assertContains(response, 'function restoreDispatcherDesktopState(freshBoard, state)')
        self.assertContains(response, 'function sortDesktopEquipmentList')
        self.assertContains(response, 'function compareDesktopEquipmentTiles')
        self.assertContains(response, 'function refreshDesktopBoardAfterStructuralAction')
        self.assertContains(response, 'window.initAppConfirmForms')
        self.assertContains(response, 'window.initDispatcherThemeControls')
        self.assertContains(response, 'window.initDispatcherRadialClocks')
        self.assertContains(response, 'detailLayer.dataset.gdActiveCardId')
        self.assertContains(response, 'dispatcherIncomingRefreshQueueGraceMs')
        self.assertContains(response, 'isDispatcherSyncQueueBlockingRefresh')
        self.assertNotContains(response, 'return Boolean(document.querySelector(".dispatcher-board")) && !document.querySelector(".mm-mobile-shell");')

    def test_mining_master_mobile_shell_renders_open_shift_status(self):
        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'mm-mobile-shell')
        self.assertContains(response, 'is-shift-open')
        self.assertContains(response, 'data-mm-mobile-readonly="false"')
        self.assertContains(response, 'mm-mobile-plan-ring')
        self.assertContains(response, 'Горный мастер')
        self.assertContains(response, 'Горный М.Т.')
        self.assertNotContains(response, 'mm-mobile-clock')
        self.assertNotContains(response, 'mm-mobile-icon-button')

    def test_mining_master_mobile_complex_status_chip_uses_downtime_reason_label(self):
        reason = DowntimeReason.objects.create(
            name='Заправка экскаватора',
            short_label='Заправка',
            equipment_type=self.excavator.equipment_type,
            is_critical=False,
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            reason=reason,
            started_at=timezone.now(),
        )

        response = self.client.get(reverse('mining_master_assignments'))
        mobile_zones = [
            zone
            for zone in response.context['dispatcher_dashboard']['mobile_complex_zones']
            if not zone.get('is_empty')
        ]
        complex_card = next(zone for zone in mobile_zones if zone['id'] == 'K-1')

        self.assertEqual(complex_card['status_key'], 'yellow')
        self.assertEqual(complex_card['equipment_state_code'], 'waiting')
        self.assertEqual(complex_card['status_label'], 'Заправка')
        self.assertContains(response, 'Заправка')

    def test_mining_master_desktop_complex_status_chip_uses_downtime_reason_label(self):
        reason = DowntimeReason.objects.create(
            name='БВР тест Горного мастера',
            short_label='БВР',
            equipment_type=self.excavator.equipment_type,
            equipment_state=self.waiting_state,
            is_critical=False,
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            reason=reason,
            started_at=timezone.now(),
        )

        response = self.client.get(reverse('mining_master_assignments'))
        complex_card = next(
            zone
            for zone in response.context['dispatcher_dashboard']['complex_zones']
            if zone.get('id') == 'K-1'
        )

        self.assertEqual(complex_card['status_key'], 'yellow')
        self.assertEqual(complex_card['equipment_state_code'], 'waiting')
        self.assertEqual(complex_card['status_label'], 'БВР')
        self.assertContains(response, '<span class="complex-state-chip">БВР</span>', html=True)

    def test_mining_master_desktop_complex_status_chip_preserves_server_color_from_reason_state(self):
        reason = DowntimeReason.objects.create(
            name='ТО тест Горного мастера',
            short_label='ТО',
            equipment_type=self.excavator.equipment_type,
            equipment_state=self.maintenance_state,
            is_critical=False,
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            reason=reason,
            started_at=timezone.now(),
        )

        response = self.client.get(reverse('mining_master_assignments'))
        complex_card = next(
            zone
            for zone in response.context['dispatcher_dashboard']['complex_zones']
            if zone.get('id') == 'K-1'
        )

        self.assertEqual(complex_card['status_key'], 'orange')
        self.assertEqual(complex_card['equipment_state_code'], 'maintenance')
        self.assertEqual(complex_card['status_label'], 'ТО')
        self.assertContains(response, '<span class="complex-state-chip">ТО</span>', html=True)

    def test_mining_master_mobile_shell_includes_pwa_install_metadata(self):
        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('mining_master_manifest'))
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, 'name="theme-color"')
        self.assertContains(response, 'apple-mobile-web-app-capable')
        self.assertContains(response, 'apple-touch-icon')
        self.assertContains(response, '/mining-master-sw.js')
        self.assertContains(response, 'data-mm-pwa-update-modal')
        self.assertContains(response, 'data-mm-pwa-current-shell-version')
        self.assertContains(response, 'data-mm-pwa-update-nav-target')
        self.assertContains(response, 'data-mm-pwa-update-badge')
        self.assertContains(response, 'setMiningMasterUpdateButtonsAttention')
        self.assertContains(response, 'Обновить приложение Горного мастера')
        self.assertContains(response, 'miningMasterUpdateCheckIntervalMs')
        self.assertContains(response, 'checkMiningMasterPwaUpdateSilently')
        self.assertContains(response, 'Установлена последняя версия приложения')
        self.assertContains(response, 'mining-master-mobile-shell-v107')
        self.assertContains(response, '"trip_changed"')
        self.assertContains(
            response,
            'querySelectorAll("[data-mm-mobile-panel=\'trucks\'] .mm-mobile-truck-card[data-equipment-card-id]").forEach(bindEquipmentCardTrigger)'
        )

    def test_login_screen_includes_mining_master_pwa_install_metadata(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('mining_master_manifest'))
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, 'name="theme-color"')
        self.assertContains(response, 'apple-mobile-web-app-capable')
        self.assertContains(response, 'apple-touch-icon')
        self.assertContains(response, '/mining-master-sw.js')

    def test_mining_master_manifest_is_installable_pwa_manifest(self):
        response = self.client.get(reverse('mining_master_manifest'))
        manifest = json.loads(response.content.decode('utf-8'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/manifest+json; charset=utf-8')
        self.assertEqual(manifest['name'], 'Горный мастер')
        self.assertEqual(manifest['start_url'], reverse('mining_master_assignments'))
        self.assertEqual(manifest['scope'], '/')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['orientation'], 'portrait')
        self.assertIn('icons', manifest)
        self.assertTrue(any(icon.get('sizes') == '192x192' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('sizes') == '512x512' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('purpose') == 'maskable' for icon in manifest['icons']))

    def test_mining_master_service_worker_caches_pwa_assets(self):
        response = self.client.get(reverse('mining_master_service_worker'))
        script = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertIn('`${CACHE_PREFIX}v107`', script)
        self.assertIn('const CACHE_PREFIX = "mining-master-mobile-shell-";', script)
        self.assertIn('key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME', script)
        self.assertIn('EXCLUDED_NAVIGATION_PREFIXES = ["/deputy-mining-manager/"]', script)
        self.assertIn('EXCLUDED_NAVIGATION_PREFIXES.some(prefix => url.pathname.startsWith(prefix))', script)
        self.assertIn(reverse('mining_master_manifest'), script)
        self.assertIn('/static/js/realtime-client.js', script)
        self.assertIn('ignoreSearch: true', script)
        self.assertIn('request.headers.get("X-Requested-With") === "XMLHttpRequest"', script)
        self.assertIn('networkOnly(request)', script)
        self.assertIn('/static/img/pwa/mining-master-192.png', script)
        self.assertIn('/static/img/pwa/mining-master-maskable-512.png', script)
        self.assertIn('SKIP_WAITING', script)
        self.assertIn('GET_VERSION', script)

    def test_mining_master_mobile_shell_renders_closed_shift_status(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])

        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'mm-mobile-shell')
        self.assertContains(response, 'is-shift-closed')
        self.assertContains(response, 'data-mm-mobile-readonly="true"')
        self.assertContains(response, 'mm-mobile-plan-ring')
        self.assertNotContains(response, 'is-shift-open')

    def test_mining_master_mobile_shell_is_readonly_when_other_master_shift_blocks_work(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        other_master = Employee.objects.create(
            full_name='Другой Горный Мастер',
            phone='79000000401',
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=other_master,
            role=self.master_role,
            access_code='400001',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        EmployeeShift.objects.create(
            employee=other_master,
            shift_type='day',
            opened_at=timezone.now(),
            opened_by=other_master,
        )

        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'mm-mobile-shell')
        self.assertContains(response, 'is-shift-blocked')
        self.assertContains(response, 'is-readonly')
        self.assertContains(response, 'data-mm-mobile-readonly="true"')

    def test_truck_tiles_keep_unique_visible_numbers(self):
        response = self.client.get(reverse('mining_master_assignments'))

        dashboard = response.context['dispatcher_dashboard']
        garage_ids = {
            int(tile['card_id'])
            for tile in dashboard['truck_garage_tiles']
            if tile.get('card_id')
        }
        complex_ids = {
            int(truck['card_id'])
            for complex_item in dashboard['complex_zones']
            for truck in complex_item.get('active_truck_tiles', [])
            if truck.get('card_id')
        }
        garage_numbers = {str(tile['name']) for tile in dashboard['truck_garage_tiles']}
        complex_numbers = {
            str(truck['name'])
            for complex_item in dashboard['complex_zones']
            for truck in complex_item.get('active_truck_tiles', [])
        }

        self.assertFalse(garage_ids & complex_ids)
        self.assertIn('101', complex_numbers)
        self.assertNotIn('101', garage_numbers)
        self.assertIn('102', garage_numbers)

    def test_active_trip_sets_truck_status_to_loaded_waiting_unload(self):
        rock_type = RockType.objects.create(name='Щебень')
        dump_point = DumpPoint.objects.create(name='Накопитель 1')
        Trip.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            rock_type=rock_type,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
        )

        response = self.client.get(reverse('mining_master_assignments'))

        dashboard = response.context['dispatcher_dashboard']
        complex_zone = next(
            zone for zone in dashboard['complex_zones']
            if str(zone.get('equipment_card_id')) == str(self.excavator.id)
        )
        active_tiles = complex_zone.get('active_truck_tiles', [])
        truck_tile = next(
            tile for tile in active_tiles
            if str(tile.get('card_id')) == str(self.assigned_truck.id)
        )

        self.assertEqual(truck_tile.get('status'), 'green')
        self.assertEqual(truck_tile.get('label'), 'На разгрузку')
        self.assertEqual(truck_tile.get('equipment_state_code'), 'loaded_waiting_unload')

    def test_accepted_truck_without_trip_is_assigned_blue_in_complex(self):
        HaulAssignment.objects.filter(truck=self.assigned_truck).update(
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        response = self.client.get(reverse('mining_master_assignments'))

        dashboard = response.context['dispatcher_dashboard']
        complex_zone = next(
            zone for zone in dashboard['complex_zones']
            if str(zone.get('equipment_card_id')) == str(self.excavator.id)
        )
        truck_tile = next(
            tile for tile in complex_zone.get('active_truck_tiles', [])
            if str(tile.get('card_id')) == str(self.assigned_truck.id)
        )

        self.assertEqual(truck_tile.get('status'), 'blue')
        self.assertEqual(truck_tile.get('equipment_state_code'), 'assigned')
        self.assertEqual(truck_tile.get('label'), 'Назначена')

    def test_mobile_truck_garage_shows_server_truck_states(self):
        truck_type = self.free_truck.equipment_type
        truck_model = self.free_truck.model
        trip_truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='104',
            is_active=True,
        )
        broken_truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='103',
            is_active=True,
        )
        reason = DowntimeReason.objects.create(name='Авария самосвала', is_critical=True)
        DowntimeEvent.objects.create(
            equipment=broken_truck,
            employee=self.master,
            reason=reason,
            started_at=timezone.now(),
        )
        rock_type = RockType.objects.create(name='Щебень рейс')
        dump_point = DumpPoint.objects.create(name='Накопитель рейс')
        Trip.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            rock_type=rock_type,
            dump_point=dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
        )
        Trip.objects.create(
            truck=trip_truck,
            excavator=self.excavator,
            rock_type=rock_type,
            dump_point=dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
        )

        response = self.client.get(reverse('mining_master_assignments'))

        dashboard = response.context['dispatcher_dashboard']
        mobile_tiles = {
            str(tile['name']): tile
            for tile in dashboard['mobile_truck_garage_tiles']
        }

        self.assertNotIn('101', mobile_tiles)
        self.assertEqual(mobile_tiles['102']['status'], 'gray')
        self.assertEqual(mobile_tiles['102']['equipment_state_code'], 'free')
        self.assertEqual(mobile_tiles['102']['label'], 'Свободен')
        self.assertEqual(mobile_tiles['103']['status'], 'red')
        self.assertEqual(mobile_tiles['103']['equipment_state_code'], 'breakdown')
        self.assertEqual(mobile_tiles['103']['label'], 'Поломка')
        self.assertEqual(mobile_tiles['104']['status'], 'green')
        self.assertEqual(mobile_tiles['104']['equipment_state_code'], 'loaded_waiting_unload')
        self.assertEqual(mobile_tiles['104']['label'], 'На разгрузку')

    def test_mobile_complexes_are_sorted_by_status_priority_then_number(self):
        excavator_type = self.excavator.equipment_type
        model = self.excavator.model
        excavator_10 = Equipment.objects.create(
            equipment_type=excavator_type,
            model=model,
            garage_number='Э-10',
            is_active=True,
        )
        excavator_3 = Equipment.objects.create(
            equipment_type=excavator_type,
            model=model,
            garage_number='Э-3',
            is_active=True,
        )
        excavator_4 = Equipment.objects.create(
            equipment_type=excavator_type,
            model=model,
            garage_number='Э-4',
            is_active=True,
        )
        ExcavatorPlacement.objects.create(excavator=excavator_10, zone=ExcavatorPlacement.Zone.ACTIVE)
        ExcavatorPlacement.objects.create(excavator=excavator_3, zone=ExcavatorPlacement.Zone.ACTIVE)
        ExcavatorPlacement.objects.create(excavator=excavator_4, zone=ExcavatorPlacement.Zone.ACTIVE)
        critical_downtime_reason = DowntimeReason.objects.create(
            name='Аварийная остановка',
            equipment_type=excavator_type,
            is_critical=True,
        )
        warning_downtime_reason = DowntimeReason.objects.create(
            name='Перегон',
            equipment_type=excavator_type,
            is_critical=False,
        )
        DowntimeEvent.objects.create(
            equipment=excavator_10,
            employee=self.master,
            reason=critical_downtime_reason,
            started_at=timezone.now(),
        )
        DowntimeEvent.objects.create(
            equipment=excavator_3,
            employee=self.master,
            reason=warning_downtime_reason,
            started_at=timezone.now(),
        )
        pending_truck_type = self.free_truck.equipment_type
        pending_truck_model = self.free_truck.model
        for number, excavator in (('103', excavator_3), ('104', excavator_4)):
            truck = Equipment.objects.create(
                equipment_type=pending_truck_type,
                model=pending_truck_model,
                garage_number=number,
                is_active=True,
            )
            HaulAssignment.objects.create(
                truck=truck,
                excavator=excavator,
                assigned_by=self.master,
                status=AssignmentStatus.PENDING,
            )

        response = self.client.get(reverse('mining_master_assignments'))

        mobile_zones = [
            zone
            for zone in response.context['dispatcher_dashboard']['mobile_complex_zones']
            if not zone.get('is_empty')
        ]
        self.assertEqual(
            [(zone['id'], zone['status_key']) for zone in mobile_zones],
            [
                ('K-10', 'red'),
                ('K-1', 'yellow'),
                ('K-3', 'yellow'),
                ('K-4', 'yellow'),
                ('K-2', 'blue'),
            ],
        )
        self.assertEqual(
            next(zone for zone in mobile_zones if zone['id'] == 'K-3')['equipment_state_code'],
            'waiting',
        )

    def test_closed_mining_master_shift_uses_dispatcher_header_defaults(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])

        response = self.client.get(reverse('mining_master_assignments'))

        self.assertContains(response, 'ГОРНЫЙ МАСТЕР')
        self.assertNotContains(response, 'Предыдущая смена закрыта')
        self.assertNotContains(response, 'ожидание запуска')

    def test_mining_master_can_create_assignment_from_screen(self):
        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'assign',
                'excavator': self.excavator.id,
                'truck': self.free_truck.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        assignment = HaulAssignment.objects.get(truck=self.free_truck, excavator=self.excavator)
        self.assertEqual(assignment.assigned_by, self.master)
        self.assertEqual(assignment.status, AssignmentStatus.PENDING)

    def test_mining_master_cannot_assign_without_open_shift(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'assign',
                'excavator': self.excavator.id,
                'truck': self.free_truck.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(HaulAssignment.objects.filter(truck=self.free_truck, excavator=self.excavator).exists())

    def test_mining_master_can_release_truck_to_garage(self):
        HaulAssignment.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'release',
                'truck': self.assigned_truck.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        assignments = HaulAssignment.objects.filter(truck=self.assigned_truck)
        self.assertTrue(assignments.filter(status=AssignmentStatus.ACCEPTED, ended_at__isnull=True).exists())
        pending_release = assignments.get(
            action=HaulAssignmentAction.RELEASE,
            status=AssignmentStatus.PENDING,
            ended_at__isnull=True,
        )
        self.assertGreater(pending_release.effective_at, timezone.now())

    def test_reassignment_keeps_old_excavator_until_driver_accepts(self):
        HaulAssignment.objects.filter(truck=self.assigned_truck).delete()
        accepted = HaulAssignment.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        pending, created = schedule_haul_assignment(
            truck=self.assigned_truck,
            excavator=self.other_excavator,
            assigned_by=self.master,
        )

        self.assertTrue(created)
        accepted.refresh_from_db()
        self.assertEqual(accepted.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(accepted.ended_at)
        self.assertEqual(pending.status, AssignmentStatus.PENDING)

        apply_pending_haul_assignment(pending.id)

        accepted.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(accepted.status, AssignmentStatus.CANCELLED)
        self.assertIsNotNone(accepted.ended_at)
        self.assertEqual(pending.status, AssignmentStatus.ACCEPTED)

    def test_pending_assignment_applies_automatically_after_five_minutes(self):
        HaulAssignment.objects.filter(truck=self.free_truck).delete()
        start = timezone.now()
        pending, _ = schedule_haul_assignment(
            truck=self.free_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            now=start,
        )

        applied = reconcile_due_haul_assignments(now=start + timedelta(minutes=5, seconds=1))

        pending.refresh_from_db()
        self.assertEqual(applied, 1)
        self.assertEqual(pending.status, AssignmentStatus.ACCEPTED)

    def test_pending_release_keeps_assignment_until_timeout_then_removes_it(self):
        HaulAssignment.objects.filter(truck=self.free_truck).delete()
        accepted = HaulAssignment.objects.create(
            truck=self.free_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        start = timezone.now()
        pending, _ = schedule_haul_release(
            truck=self.free_truck,
            assigned_by=self.master,
            now=start,
        )

        accepted.refresh_from_db()
        self.assertIsNone(accepted.ended_at)
        self.assertEqual(pending.action, HaulAssignmentAction.RELEASE)

        reconcile_due_haul_assignments(now=start + timedelta(minutes=5, seconds=1))

        accepted.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(accepted.status, AssignmentStatus.CANCELLED)
        self.assertEqual(pending.status, AssignmentStatus.CANCELLED)

    def test_mining_master_can_release_excavator_complex_to_garage(self):
        other_assignment = HaulAssignment.objects.create(
            truck=self.free_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'release_excavator',
                'excavator': self.excavator.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertTrue(
            HaulAssignment.objects
            .filter(excavator=self.excavator, action=HaulAssignmentAction.RELEASE, status=AssignmentStatus.PENDING, ended_at__isnull=True)
            .exists()
        )
        other_assignment.refresh_from_db()
        self.assertIsNone(other_assignment.ended_at)

    def test_mining_master_can_release_all_complexes_to_garage(self):
        HaulAssignment.objects.create(
            truck=self.free_truck,
            excavator=self.other_excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_assignments'),
            {'action': 'release_all'},
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertTrue(
            HaulAssignment.objects
            .filter(action=HaulAssignmentAction.RELEASE, status=AssignmentStatus.PENDING, ended_at__isnull=True)
            .exists()
        )
        self.assertFalse(
            ExcavatorPlacement.objects
            .filter(zone=ExcavatorPlacement.Zone.ACTIVE)
            .exists()
        )

    def test_mining_master_can_move_excavator_to_inactive_shift(self):
        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'deactivate_excavator',
                'excavator': self.excavator.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        placement = ExcavatorPlacement.objects.get(excavator=self.excavator)
        self.assertEqual(placement.zone, ExcavatorPlacement.Zone.INACTIVE)
        self.assertTrue(
            HaulAssignment.objects
            .filter(excavator=self.excavator, action=HaulAssignmentAction.RELEASE, status=AssignmentStatus.PENDING, ended_at__isnull=True)
            .exists()
        )

    def test_inactive_excavator_is_tile_not_complex_zone(self):
        ExcavatorPlacement.objects.filter(excavator=self.other_excavator).update(zone=ExcavatorPlacement.Zone.INACTIVE)

        response = self.client.get(reverse('mining_master_assignments'))

        dashboard = response.context['dispatcher_dashboard']
        complex_ids = {
            int(item['equipment_card_id'])
            for item in dashboard['complex_zones']
            if item.get('equipment_card_id')
        }
        inactive_ids = {
            int(item['card_id'])
            for item in dashboard['excavator_garage_tiles']
            if item.get('card_id')
        }

        self.assertIn(self.excavator.id, complex_ids)
        self.assertNotIn(self.other_excavator.id, complex_ids)
        self.assertIn(self.other_excavator.id, inactive_ids)

    def test_mining_master_can_reassign_truck_with_active_trip(self):
        rock_type = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='Отвал тест')
        Trip.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            rock_type=rock_type,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
        )

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'assign',
                'excavator': self.other_excavator.id,
                'truck': self.assigned_truck.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(
            HaulAssignment.objects
            .filter(truck=self.assigned_truck, excavator=self.excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
        self.assertTrue(
            HaulAssignment.objects
            .filter(
                truck=self.assigned_truck,
                excavator=self.other_excavator,
                ended_at__isnull=True,
                status=AssignmentStatus.PENDING,
            )
            .exists()
        )
        self.assertTrue(
            Trip.objects.filter(
                truck=self.assigned_truck,
                excavator=self.excavator,
                status=TripStatus.ACTIVE,
            ).exists()
        )

    def test_mining_master_json_can_assign_truck(self):
        response = self.client.post(
            reverse('mining_master_assign_truck'),
            data=json.dumps({
                'action': 'assign',
                'truck_id': self.free_truck.id,
                'excavator_id': self.excavator.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertTrue(
            HaulAssignment.objects
            .filter(
                truck=self.free_truck,
                excavator=self.excavator,
                status=AssignmentStatus.PENDING,
                ended_at__isnull=True,
            )
            .exists()
        )

    def test_mining_master_json_can_release_truck_to_garage(self):
        response = self.client.post(
            reverse('mining_master_assign_truck'),
            data=json.dumps({
                'action': 'release',
                'truck_id': self.assigned_truck.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertTrue(response.json()['created'])
        self.assertTrue(
            HaulAssignment.objects
            .filter(
                truck=self.assigned_truck,
                action=HaulAssignmentAction.RELEASE,
                status=AssignmentStatus.PENDING,
                ended_at__isnull=True,
            )
            .exists()
        )

    def test_mining_master_json_rejects_stale_truck_move_conflict(self):
        response = self.client.post(
            reverse('mining_master_assign_truck'),
            data=json.dumps({
                'action': 'assign',
                'truck_id': self.assigned_truck.id,
                'excavator_id': self.other_excavator.id,
                'expected_source_excavator_id': self.other_excavator.id,
                'client_action_id': 'client-action-1',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()['ok'])
        self.assertTrue(response.json()['conflict'])
        self.assertEqual(response.json()['client_action_id'], 'client-action-1')
        self.assertTrue(
            HaulAssignment.objects
            .filter(truck=self.assigned_truck, excavator=self.excavator, ended_at__isnull=True)
            .exists()
        )

    def test_mining_master_json_release_complex_keeps_excavator_active(self):
        HaulAssignment.objects.create(
            truck=self.free_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_assign_truck'),
            data=json.dumps({
                'action': 'release_complex',
                'excavator_id': self.excavator.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
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

    def test_mining_master_json_move_excavator_active_creates_placement(self):
        ExcavatorPlacement.objects.filter(excavator=self.other_excavator).delete()

        response = self.client.post(
            reverse('mining_master_move_excavator'),
            data=json.dumps({
                'excavator_id': self.other_excavator.id,
                'zone': ExcavatorPlacement.Zone.ACTIVE,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertTrue(
            ExcavatorPlacement.objects
            .filter(excavator=self.other_excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
            .exists()
        )

    def test_mining_master_json_move_excavator_inactive_disbands_complex(self):
        HaulAssignment.objects.create(
            truck=self.free_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_move_excavator'),
            data=json.dumps({
                'excavator_id': self.excavator.id,
                'zone': ExcavatorPlacement.Zone.INACTIVE,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertFalse(
            HaulAssignment.objects
            .filter(excavator=self.excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
        self.assertTrue(
            ExcavatorPlacement.objects
            .filter(excavator=self.excavator, zone=ExcavatorPlacement.Zone.INACTIVE)
            .exists()
        )

    def test_mining_master_can_start_and_end_shift(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        session = self.client.session
        session['device_kind'] = 'personal'
        session.save()

        response = self.client.post(reverse('mining_master_assignments'), {'action': 'start_shift'})
        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertTrue(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())
        self.assertEqual(self.client.session['device_kind'], 'personal')

        response = self.client.post(reverse('mining_master_assignments'), {'action': 'end_shift'})
        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())

    def test_mining_master_cannot_restart_shift_after_employee_is_dismissed(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        Employee.objects.filter(pk=self.master.pk).update(
            status=Employee.Status.DISMISSED,
            is_active=False,
        )
        session = self.client.session
        session['device_kind'] = 'personal'
        session.save()

        response = self.client.post(
            reverse('mining_master_assignments'),
            {'action': 'start_shift'},
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(
            EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists()
        )

    def test_shared_desktop_requires_credentials_to_start_shift(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        session = self.client.session
        session['device_kind'] = 'shared'
        session.save()

        response = self.client.get(reverse('mining_master_assignments'))

        self.assertContains(response, 'data-shared-shift-login-open')
        self.assertContains(response, 'name="reauth_phone"')
        self.assertContains(response, 'shared-shift-country-code')
        self.assertContains(response, '999-000-00-00')
        self.assertContains(response, 'placeholder="999-000-00-00"')
        self.assertContains(response, 'maxlength="13"')
        self.assertContains(response, 'pattern="[0-9]{3}-[0-9]{3}-[0-9]{2}-[0-9]{2}"')
        self.assertContains(response, 'name="reauth_access_code"')
        self.assertContains(response, 'placeholder="00-00-00"')
        self.assertContains(response, 'maxlength="8"')
        self.assertContains(response, 'pattern="[0-9]{2}-[0-9]{2}-[0-9]{2}"')
        self.assertContains(response, 'name="device_kind"')
        self.assertContains(response, 'value="personal"')
        self.assertContains(response, 'value="shared"')
        self.assertContains(response, 'на этом устройстве')

        response = self.client.post(reverse('mining_master_assignments'), {'action': 'start_shift'})

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())

    def test_mobile_start_shift_marks_shared_session_as_personal(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        session = self.client.session
        session['device_kind'] = 'shared'
        session.save()

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'start_shift',
                'device_kind': 'personal',
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertTrue(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())
        self.assertEqual(self.client.session['device_kind'], 'personal')

    def test_shared_desktop_can_start_shift_with_other_mining_master_credentials(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        next_master = Employee.objects.create(
            full_name='Сменный горный мастер',
            phone='79000000044',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        next_access = EmployeeAccess.objects.create(
            employee=next_master,
            role=self.master_role,
            access_code='444444',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['device_kind'] = 'shared'
        session.save()

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'start_shift',
                'reauth_phone': '900-000-00-44',
                'reauth_access_code': '44-44-44',
                'device_kind': 'shared',
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())
        self.assertTrue(EmployeeShift.objects.filter(employee=next_master, closed_at__isnull=True).exists())
        self.assertEqual(self.client.session['employee_access_id'], next_access.id)
        self.assertEqual(self.client.session['device_kind'], 'shared')

    def test_dispatcher_access_cannot_open_mining_master_screen(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Диспетчер тест', is_active=True)
        dispatcher_access = EmployeeAccess.objects.create(
            employee=dispatcher,
            role=dispatcher_role,
            access_code='400001',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = dispatcher_access.id
        session.save()

        response = self.client.post(reverse('mining_master_assignments'), {'action': 'start_shift'})
        self.assertRedirects(response, reverse('role_home'), fetch_redirect_response=False)
        response = self.client.get(reverse('mining_master_assignments'))

        self.assertFalse(EmployeeShift.objects.filter(employee=dispatcher, closed_at__isnull=True).exists())
        self.assertRedirects(response, reverse('role_home'), fetch_redirect_response=False)
