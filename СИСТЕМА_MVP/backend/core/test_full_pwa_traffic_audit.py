from django.test import SimpleTestCase

from tools.full_pwa_traffic_audit import READY_ROLES, build_summary


class FullPwaTrafficProjectionRegressionTests(SimpleTestCase):
    def test_projection_uses_contractual_five_and_fifteen_second_intervals(self):
        intervals = {
            role.role: role.poll_interval_seconds
            for role in READY_ROLES
        }
        self.assertEqual(
            intervals,
            {
                'admin': 15,
                'oup': None,
                'deputy_mining_manager': None,
                'dispatcher': 5,
                'mining_master': 5,
                'excavator_operator': 5,
                'driver': 5,
                'manager': 15,
            },
        )
        role_results = [
            {
                'role': role.role,
                'simultaneous_sessions': role.simultaneous_sessions,
                'realtime': role.realtime,
                'poll_interval_seconds': role.poll_interval_seconds,
                'rows': [],
                'realtime_body_mean': 10,
                'realtime_gzip_mean': 4,
                'changed_static_urls_between_navigations': [],
            }
            for role in READY_ROLES
        ]

        summary = build_summary(role_results)

        self.assertEqual(summary['realtime_requests_per_12h_shift'], 550_080)
        self.assertEqual(summary['realtime_body_bytes_per_12h_shift'], 5_500_800)
        self.assertEqual(
            summary['realtime_gzip_estimate_bytes_per_12h_shift'],
            2_200_320,
        )
        self.assertTrue(
            summary['realtime_projection_assumes_visible_active_windows'],
        )
        projected_by_role = {
            item['role']: item['projected_realtime_requests_per_12h_shift']
            for item in summary['roles']
        }
        self.assertEqual(projected_by_role['driver'], 457_920)
        self.assertEqual(projected_by_role['excavator_operator'], 69_120)
        self.assertEqual(projected_by_role['dispatcher'], 8_640)
        self.assertEqual(projected_by_role['mining_master'], 8_640)
        self.assertEqual(projected_by_role['admin'], 2_880)
        self.assertEqual(projected_by_role['manager'], 2_880)
        self.assertEqual(projected_by_role['oup'], 0)
        self.assertEqual(projected_by_role['deputy_mining_manager'], 0)
