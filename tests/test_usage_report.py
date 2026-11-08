import unittest

from app.bale_bot import format_usage_report


class UsageReportTests(unittest.TestCase):
    def test_group_report_includes_all_active_users(self):
        users = [
            {"full_name": f"کاربر {index}", "total": index}
            for index in range(1, 19)
        ]

        report = format_usage_report(
            active_users=users,
            inactive_users=[],
            admins=[],
            top_admin_id=None,
        )

        for index in range(1, 19):
            self.assertIn(f"کاربر {index} — {index} بار", report)


if __name__ == "__main__":
    unittest.main()
