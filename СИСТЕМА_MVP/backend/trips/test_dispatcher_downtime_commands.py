import inspect
from unittest.mock import patch

from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import resolve, reverse

from . import views as trips_views


class DispatcherDowntimeCommandBoundaryTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_url_keeps_public_views_facade(self):
        match = resolve(reverse('dispatcher_close_downtime', kwargs={'event_id': 17}))

        self.assertIs(match.func, trips_views.dispatcher_close_downtime_view)

    def test_public_view_is_thin_downtime_command_facade(self):
        source = inspect.getsource(trips_views.dispatcher_close_downtime_view)

        self.assertIn('_execute_dispatcher_close_downtime(', source)
        self.assertIn('lock_mutation_access=lock_dispatcher_mutation_access', source)
        self.assertIn('response_builder=dispatcher_downtime_close_response', source)
        self.assertIn('event_payload=downtime_event_payload', source)
        self.assertNotIn('DowntimeEvent.objects', source)
        self.assertNotIn('lock_production_state', source)
        self.assertNotIn('bump_operational_state', source)

    def test_public_view_keeps_post_guard_before_command_execution(self):
        request = self.factory.get('/dispatcher/control/downtime/17/close/')
        with patch.object(trips_views, '_execute_dispatcher_close_downtime') as execute:
            response = trips_views.dispatcher_close_downtime_view(request, event_id=17)

        self.assertEqual(response.status_code, 405)
        execute.assert_not_called()


class DispatcherDowntimeFacadeDelegationTests(TestCase):
    def test_facade_injects_views_patch_and_response_seams(self):
        request = RequestFactory().post('/dispatcher/control/downtime/17/close/')
        expected = JsonResponse({'ok': True})
        with patch.object(
            trips_views,
            '_execute_dispatcher_close_downtime',
            return_value=expected,
        ) as execute:
            response = trips_views.dispatcher_close_downtime_view(request, event_id=17)

        self.assertIs(response, expected)
        execute.assert_called_once_with(
            request,
            17,
            lock_mutation_access=trips_views.lock_dispatcher_mutation_access,
            response_builder=trips_views.dispatcher_downtime_close_response,
            event_payload=trips_views.downtime_event_payload,
        )
