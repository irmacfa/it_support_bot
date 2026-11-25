"""Regression tests for bugs found during the full project audit."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import bale_bot, config, info_store, registration, schedule_store, separated_experts
from app.main import app, _safe_filename, _info_target_for_backup, _restore_info_file


class SafeFilenameTests(unittest.TestCase):
    def test_dot_paths_cannot_escape_the_base_directory(self):
        for name in ("..", ".", "...", "../data", "..\\data"):
            self.assertNotIn(_safe_filename(name), {".", "..", ""})

    def test_duplicate_upload_route_removed(self):
        routes = [r for r in app.routes if getattr(r, "path", None) == "/info/upload"]
        self.assertEqual(len(routes), 1)


class SecurityMiddlewareTests(unittest.TestCase):
    def test_https_origin_is_accepted_behind_a_tls_proxy(self):
        # TestClient میبیند http ولی مرورگر پشت پروکسی Origin=https میفرستد.
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/login",
            data={"password": config.ADMIN_PASSWORD},
            headers={"origin": "https://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

    def test_dot_token_cannot_delete_files_outside_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "upload_staging"
            staging.mkdir()
            sentinel = root / "DO_NOT_DELETE.txt"
            sentinel.write_text("keep", encoding="utf-8")

            client = TestClient(app, raise_server_exceptions=False)
            headers = {"origin": "http://testserver"}
            client.post("/login", data={"password": config.ADMIN_PASSWORD}, headers=headers)
            with patch.object(config, "DATA_DIR", root), \
                 patch.object(config, "UPLOAD_STAGING_DIR", staging):
                for path in ("/upload/cancel", "/stores/restore"):
                    field = "token" if path == "/upload/cancel" else "backup"
                    resp = client.post(path, data={field: ".."}, headers=headers)
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(sentinel.exists(), f"{path} بیرون از پوشه موقت را پاک کرد")


class ScheduleUpdateTests(unittest.TestCase):
    def test_update_day_persists_without_unbound_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            path.write_text(json.dumps({"1404/01/01": {"date": "1404/01/01", "desk1": "الف"}}),
                            encoding="utf-8")
            with patch.object(schedule_store, "JSON_PATH", path):
                updated = schedule_store.update_day("1404/01/01", desk1="ب")
                self.assertEqual(updated["desk1"], "ب")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["1404/01/01"]["desk1"], "ب")


class RegistrationTests(unittest.TestCase):
    def test_register_reloads_approvals_written_by_the_panel(self):
        original = registration._registered
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "registered.json"
                path.write_text(json.dumps({"1": {"user_id": "1", "approved": True}}), encoding="utf-8")
                registration._registered = {}
                with patch.object(registration, "REGISTRY_PATH", path):
                    registration.register("2", "کاربر جدید", {})
                    self.assertTrue(registration.is_approved("1"))
        finally:
            registration._registered = original


class SeparatedExpertsTests(unittest.TestCase):
    def test_empty_list_is_not_reseeded(self):
        original = separated_experts._records
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "separated.json"
                path.write_text("[]", encoding="utf-8")
                separated_experts._records = []
                with patch.object(separated_experts, "STORE_PATH", path):
                    self.assertEqual(separated_experts.list_all(), [])
        finally:
            separated_experts._records = original


class InfoRestoreTests(unittest.TestCase):
    def test_restore_target_follows_backup_suffix(self):
        with patch.object(config, "INFO_EXCEL_PATH", Path("/data/info_stores.xlsx")):
            self.assertEqual(_info_target_for_backup(Path("/backup/x.xlsx")),
                             Path("/data/info_stores.xlsx"))
            self.assertEqual(_info_target_for_backup(Path("/backup/x.csv")),
                             Path("/data/info_stores.csv"))
            self.assertEqual(_info_target_for_backup(Path("/backup/x.txt")),
                             Path("/data/info_stores.txt"))

    def test_restoring_csv_backup_does_not_corrupt_the_csv_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_xlsx = root / "info_stores.xlsx"
            old_xlsx.write_bytes(b"old workbook")
            backup = root / "backup.csv"
            backup.write_text("کد فروشگاه\n123\n", encoding="utf-8")
            with patch.object(config, "INFO_EXCEL_PATH", root / "info_stores.xlsx"), \
                 patch.object(config, "INFO_SNAPSHOT_PATH", root / "info_snapshot.json"), \
                 patch.object(info_store, "reload_info", return_value=(True, "ok")):
                ok, _msg = _restore_info_file(backup)
                self.assertTrue(ok)
                self.assertFalse(old_xlsx.exists())
                self.assertEqual((root / "info_stores.csv").read_text(encoding="utf-8"),
                                 "کد فروشگاه\n123\n")


class InfoSourceTests(unittest.TestCase):
    def test_csv_info_file_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "info_stores.csv"
            csv_path.write_text("کد فروشگاه\n123\n", encoding="utf-8")
            with patch.object(config, "INFO_EXCEL_PATH", Path(tmp) / "info_stores.xlsx"):
                self.assertEqual(info_store.source_files(), [csv_path])


class RequestCancellationTests(unittest.TestCase):
    """دکمه لغو در پیام یادآوری: فقط درخواست کننده اجازه لغو دارد."""

    def setUp(self):
        self._charge_backup = dict(bale_bot._charge_requests)
        self._reset_backup = dict(bale_bot._reset_requests)
        bale_bot._charge_requests.clear()
        bale_bot._reset_requests.clear()

    def tearDown(self):
        bale_bot._charge_requests.clear()
        bale_bot._charge_requests.update(self._charge_backup)
        bale_bot._reset_requests.clear()
        bale_bot._reset_requests.update(self._reset_backup)

    @staticmethod
    def _charge_record(requester_id=42):
        return {
            "group_chat_id": -100,
            "answer_message_id": 5,
            "code": "123",
            "requester_name": "علی",
            "requester_user_id": requester_id,
            "resolved": False,
            "admin_message_ids": {"99": [10, 11]},
            "group_reminder_ids": [77],
            "last_reminded_at": 0,
            "created_at": 0,
        }

    @staticmethod
    def _callback(kind, request_id, user_id, chat_id=-100):
        return {
            "id": "cb1",
            "data": f"{kind}:cancel:{request_id}",
            "from": {"id": user_id, "first_name": "تست"},
            "message": {"chat": {"id": chat_id}, "message_id": 77},
        }

    def test_charge_request_remembers_requester_id(self):
        with patch.object(bale_bot, "_save_charge_requests"), \
             patch.object(bale_bot.charge_admins, "get_ids", return_value=["99"]), \
             patch.object(bale_bot, "send_message", return_value=10):
            bale_bot._notify_charge_admins(-100, 5, "123", "علی", [], 42)
        record = next(iter(bale_bot._charge_requests.values()))
        self.assertEqual(record["requester_user_id"], 42)

    def test_cancel_keyboard_is_only_created_for_known_requester(self):
        self.assertIsNone(bale_bot._requester_cancel_keyboard("charge", "abc", {}))
        keyboard = bale_bot._requester_cancel_keyboard("charge", "abc", {"requester_user_id": 42})
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "charge:cancel:abc")

    def test_group_reminder_shows_request_and_cancel_button(self):
        record = self._charge_record()
        bale_bot._charge_requests["req1"] = record
        with patch.object(bale_bot, "_save_charge_requests"), \
             patch.object(bale_bot.charge_admins, "get_ids", return_value=[]), \
             patch.object(bale_bot, "send_message", return_value=77) as send:
            bale_bot._remind_pending_charge_request("req1", record)
        args, kwargs = send.call_args
        self.assertIn("123", args[1])
        self.assertIn("علی", args[1])
        self.assertEqual(kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         "charge:cancel:req1")

    def test_admin_and_wrong_chat_cannot_cancel(self):
        record = self._charge_record()
        bale_bot._charge_requests["req1"] = record
        for user_id, chat_id in ((99, -100), (42, -999)):
            with self.subTest(user_id=user_id, chat_id=chat_id), \
                 patch.object(bale_bot, "presence"), \
                 patch.object(bale_bot, "answer_callback_query") as answer, \
                 patch.object(bale_bot, "_cancel_request_by_requester") as cancel:
                bale_bot.handle_callback_query(self._callback("charge", "req1", user_id, chat_id))
                answer.assert_called_once()
                cancel.assert_not_called()
        self.assertFalse(record["resolved"])

    def test_requester_cancel_marks_resolved_and_cleans_up(self):
        record = self._charge_record()
        bale_bot._charge_requests["req1"] = record
        with patch.object(bale_bot, "presence"), \
             patch.object(bale_bot, "answer_callback_query") as answer, \
             patch.object(bale_bot, "send_message") as send, \
             patch.object(bale_bot, "delete_message") as delete, \
             patch.object(bale_bot, "clear_message_buttons") as clear, \
             patch.object(bale_bot, "_auto_delete_message") as auto_delete, \
             patch.object(bale_bot, "_save_charge_requests") as save:
            bale_bot.handle_callback_query(self._callback("charge", "req1", 42))
        self.assertTrue(record["resolved"])
        self.assertEqual(record["resolved_action"], "cancelled")
        answer.assert_called_once()
        clear.assert_called_once_with("99", 10)
        self.assertEqual(delete.call_count, 2)
        save.assert_called_once()
        self.assertTrue(any("لغو" in str(call) for call in send.call_args_list))
        # کارت درخواست، تأیید لغو در گروه، کارت مسئول و اعلان لغو — همگی بعد از چند ثانیه پاک میشوند.
        self.assertEqual(auto_delete.call_count, 4)
        for call in auto_delete.call_args_list:
            self.assertEqual(call.args[2], bale_bot.CANCEL_CLEANUP_DELAY_SECONDS)

    def test_initial_charge_message_has_cancel_button(self):
        chat_id, user_id = -100, 42
        pending_key = (chat_id, user_id)
        old_pending = bale_bot._pending.get(pending_key)
        bale_bot._pending[pending_key] = {
            "trigger_id": 1, "prompt_id": 2, "extra_ids": [],
            "created_at": time.time(), "display_name": "علی",
        }
        message = {"chat": {"id": chat_id}, "from": {"id": user_id, "first_name": "علی"},
                   "message_id": 3, "text": "123"}
        try:
            with patch.object(bale_bot, "_normalize_code_input", side_effect=lambda t: t), \
                 patch.object(bale_bot.data_store, "lookup", return_value=[{"code": "123"}]), \
                 patch.object(bale_bot, "format_group_reply", return_value="نتیجه"), \
                 patch.object(bale_bot, "send_message", return_value=5) as send, \
                 patch.object(bale_bot, "delete_message"), \
                 patch.object(bale_bot, "usage_stats"), \
                 patch.object(bale_bot.charge_admins, "get_ids", return_value=["99"]), \
                 patch.object(bale_bot, "_save_charge_requests"):
                bale_bot.handle_group_message(message)
            markup = send.call_args_list[0].kwargs.get("reply_markup")
            self.assertIsNotNone(markup)
            request_id = markup["inline_keyboard"][0][0]["callback_data"].split(":")[2]
            self.assertIn(request_id, bale_bot._charge_requests)
            self.assertEqual(bale_bot._charge_requests[request_id]["requester_user_id"], user_id)
        finally:
            if old_pending is None:
                bale_bot._pending.pop(pending_key, None)
            else:
                bale_bot._pending[pending_key] = old_pending

    def test_initial_reset_message_has_cancel_button(self):
        chat_id, user_id = -100, 42
        pending_key = (chat_id, user_id)
        old_pending = bale_bot._pending_reset.get(pending_key)
        bale_bot._pending_reset[pending_key] = {
            "trigger_id": 1, "prompt_id": 2, "extra_ids": [],
            "created_at": time.time(), "display_name": "علی", "step": "store",
        }
        message = {"chat": {"id": chat_id}, "from": {"id": user_id, "first_name": "علی"},
                   "message_id": 3, "text": "321"}
        try:
            with patch.object(bale_bot, "_normalize_code_input", side_effect=lambda t: t), \
                 patch.object(bale_bot.data_store, "lookup", return_value=[{"mobile": "0912"}]), \
                 patch.object(bale_bot, "send_message", return_value=5) as send, \
                 patch.object(bale_bot, "delete_message"), \
                 patch.object(bale_bot, "usage_stats"), \
                 patch.object(bale_bot, "_find_unresolved_reset_request", return_value=None), \
                 patch.object(bale_bot.reset_admins, "get_ids", return_value=["99"]), \
                 patch.object(bale_bot, "_save_reset_requests"):
                bale_bot.handle_group_message(message)
            markup = send.call_args_list[0].kwargs.get("reply_markup")
            self.assertIsNotNone(markup)
            request_id = markup["inline_keyboard"][0][0]["callback_data"].split(":")[2]
            self.assertIn(request_id, bale_bot._reset_requests)
            self.assertEqual(bale_bot._reset_requests[request_id]["requester_user_id"], user_id)
        finally:
            if old_pending is None:
                bale_bot._pending_reset.pop(pending_key, None)
            else:
                bale_bot._pending_reset[pending_key] = old_pending

    def test_group_reminder_keyboard_urgent_only_on_second_or_third(self):
        record = self._charge_record()
        kb1 = bale_bot._group_reminder_keyboard("charge", "req1", record, include_urgent=False)
        self.assertEqual([b["callback_data"] for b in kb1["inline_keyboard"][0]], ["charge:cancel:req1"])
        kb2 = bale_bot._group_reminder_keyboard("charge", "req1", record, include_urgent=True)
        self.assertEqual([b["callback_data"] for b in kb2["inline_keyboard"][0]],
                         ["charge:cancel:req1", "charge:urgent:req1"])

    def test_urgent_reminder_mentions_responsible_admins(self):
        record = self._charge_record()
        bale_bot._charge_requests["req1"] = record
        cb = self._callback("charge", "req1", 999)
        cb["data"] = "charge:urgent:req1"
        with patch.object(bale_bot, "presence"), \
             patch.object(bale_bot, "answer_callback_query") as answer, \
             patch.object(bale_bot.charge_admins, "list_all", return_value=[{"user_id": "99", "name": "Ali"}]), \
             patch.object(bale_bot, "send_message", return_value=77) as send, \
             patch.object(bale_bot, "_save_charge_requests") as save:
            bale_bot.handle_callback_query(cb)
        answer.assert_called_once()
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(args[0], -100)
        self.assertIn("[Ali](uid:99)", args[1])
        self.assertEqual(kwargs.get("parse_mode"), "Markdown")
        save.assert_called_once()
        self.assertGreater(record["last_urgent_reminded_at"], 0)

    def test_urgent_reminder_cooldown_blocks_spam(self):
        record = self._charge_record()
        record["last_urgent_reminded_at"] = time.time()
        bale_bot._charge_requests["req1"] = record
        cb = self._callback("charge", "req1", 999)
        cb["data"] = "charge:urgent:req1"
        with patch.object(bale_bot, "presence"), \
             patch.object(bale_bot, "answer_callback_query") as answer, \
             patch.object(bale_bot, "send_message") as send:
            bale_bot.handle_callback_query(cb)
        send.assert_not_called()
        self.assertIn("یادآوری فوری", str(answer.call_args))

    def test_duplicate_lookup_returns_id_needed_for_cancel_button(self):
        bale_bot._reset_requests["r3"] = {
            "group_chat_id": -100, "kind": "store", "target": "321",
            "requester_user_id": 42, "resolved": False,
        }
        found = bale_bot._find_unresolved_reset_request(-100, "store", "321")
        self.assertEqual(found[0], "r3")
        self.assertEqual(bale_bot._requester_cancel_keyboard("reset", found[0], found[1])
                         ["inline_keyboard"][0][0]["callback_data"], "reset:cancel:r3")

    def test_reset_requester_can_cancel_but_reset_admin_cannot(self):
        record = {
            "group_chat_id": -100,
            "answer_message_id": 5,
            "kind": "store",
            "target": "321",
            "requester_name": "علی",
            "requester_user_id": 42,
            "resolved": False,
            "admin_message_ids": {},
            "group_reminder_ids": [],
            "last_reminded_at": 0,
            "created_at": 0,
        }
        bale_bot._reset_requests["r2"] = record
        with patch.object(bale_bot, "presence"), \
             patch.object(bale_bot, "answer_callback_query"), \
             patch.object(bale_bot, "send_message"), \
             patch.object(bale_bot, "_auto_delete_message"), \
             patch.object(bale_bot, "_save_reset_requests"):
            bale_bot.handle_callback_query(self._callback("reset", "r2", 99))
            self.assertFalse(record["resolved"])
            bale_bot.handle_callback_query(self._callback("reset", "r2", 42))
        self.assertTrue(record["resolved"])
        self.assertEqual(record["resolved_action"], "cancelled")


if __name__ == "__main__":
    unittest.main()
