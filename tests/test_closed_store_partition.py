import unittest
from unittest.mock import patch

from app import info_store


class ClosedStorePartitionTests(unittest.TestCase):
    def test_partition_uses_normalized_store_codes(self):
        rows = [
            {"کد فروشگاه": "۱۲۳", "کارشناس انفورماتیک": "فاقد کارشناس"},
            {"کد فروشگاه": "456.0", "کارشناس انفورماتیک": "فاقد کارشناس"},
            {"کد فروشگاه": "789", "کارشناس انفورماتیک": "کارشناس فعال"},
        ]

        active, closed = info_store.partition_directory_rows(rows, {"123", "456"})

        self.assertEqual([row["کد فروشگاه"] for row in closed], ["۱۲۳", "456.0"])
        self.assertEqual([row["کد فروشگاه"] for row in active], ["789"])

    def test_closed_unassigned_store_never_enters_unassigned_expert_group(self):
        rows = [
            {"کد فروشگاه": "۱۲۳", "کارشناس انفورماتیک": "فاقد کارشناس",
             "فروشگاه": "بسته"},
            {"کد فروشگاه": "456", "کارشناس انفورماتیک": "",
             "فروشگاه": "فعال بدون کارشناس"},
            {"کد فروشگاه": "789", "کارشناس انفورماتیک": "کارشناس الف",
             "فروشگاه": "فعال تخصیص‌یافته"},
        ]

        with patch.object(info_store, "get_directory_rows", return_value=(rows, [])), \
             patch.object(info_store, "closed_codes", return_value=frozenset({"123"})):
            groups = info_store.get_expert_groups()

        grouped_codes = {
            store["code"]
            for group in groups
            for store in group["stores"]
        }
        unassigned = next(
            group for group in groups if group["expert"] == info_store.EXPERT_UNSET
        )

        self.assertEqual(grouped_codes, {"456", "789"})
        self.assertEqual(unassigned["count"], 1)
        self.assertEqual([store["code"] for store in unassigned["stores"]], ["456"])


if __name__ == "__main__":
    unittest.main()
