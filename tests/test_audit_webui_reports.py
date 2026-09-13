import unittest
from unittest.mock import patch

from src.webui import annual_report_routes
import tests.test_webui_annual_report_routes as report_tests


class AnnualReportAuditTest(unittest.IsolatedAsyncioTestCase):
    setUp = report_tests.AnnualReportRoutesTest.setUp
    tearDown = report_tests.AnnualReportRoutesTest.tearDown
    create_message = report_tests.AnnualReportRoutesTest.create_message
    ts = report_tests.AnnualReportRoutesTest.ts
    dt = report_tests.AnnualReportRoutesTest.dt

    async def test_peak_time_uses_unrounded_interest(self):
        timestamp = self.ts(5, 3, 4)
        self.create_message("peak", timestamp=timestamp, interest_value=0.98765)
        self.create_message("rounded", timestamp=self.ts(4, 3), interest_value=0.98)
        data = await annual_report_routes.get_brain_power(2025)
        self.assertEqual(data.max_interest_value, 0.99)
        self.assertEqual(data.max_interest_time, self.dt(5, 3, 4).strftime("%Y-%m-%d %H:%M:%S"))

    async def test_night_sampling_does_not_materialize_daytime_messages(self):
        for i in range(80):
            self.create_message(f"day-{i}", timestamp=self.ts(11, 3, 14), user_id="bot-qq")
        self.create_message("night", timestamp=self.ts(2, 3, 3), user_id="bot-qq", content="night response")
        original = annual_report_routes.datetime
        with patch.object(annual_report_routes, "datetime", wraps=original) as datetime_mock:
            with patch(
                "src.config.config.global_config",
                report_tests.types.SimpleNamespace(bot=report_tests.types.SimpleNamespace(qq_account="bot-qq")),
            ):
                data = await annual_report_routes.get_expression_vibe(2025)
        self.assertIsNotNone(data.late_night_reply)
        self.assertLess(datetime_mock.fromtimestamp.call_count, 10)
