import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from repairbot.core import BotService, DENIED, MAX_PHOTO_BYTES, TRANSITIONS
from repairbot.telegram import TelegramRunner


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "bot.sqlite3")
        self.bot = BotService(self.path, {99})
        self.update_id = 0

    def tearDown(self):
        self.bot.close()
        self.folder.cleanup()

    def send(self, text=None, user=11, **extra):
        message = {"from": {"id": user}, "chat": {"id": user, "type": "private"}, **extra}
        if text is not None:
            message["text"] = text
        return self.update({"message": message})

    def update(self, payload):
        self.update_id += 1
        return self.bot.handle_update({"update_id": self.update_id, **payload})

    def click(self, data, user=11):
        return self.update({"callback_query": {"id": f"callback_{self.update_id}", "from": {"id": user}, "message": {"chat": {"id": user, "type": "private"}}, "data": data}})

    @staticmethod
    def text(actions):
        return "\n".join(a["params"].get("text", "") for a in actions)

    @staticmethod
    def button(actions, prefix):
        for action in actions:
            markup = action["params"].get("reply_markup", {})
            for row in markup.get("inline_keyboard", []):
                for button in row:
                    if button["callback_data"].startswith(prefix):
                        return button["callback_data"]
        raise AssertionError(f"No callback starting {prefix!r}")

    def draft(self, user=11, phone=False, photo=False):
        self.send("/new", user)
        self.send("Тестовый ноутбук", user)
        self.send("Демонстрация: устройство не включается", user)
        if photo:
            actions = self.send(user=user, photo=[{"file_id": "demo_photo", "file_size": 128}])
        else:
            actions = self.send("/skip", user)
        consent = self.button(actions, "consent:")
        if phone:
            self.click(consent.rsplit(":", 1)[0] + ":yes", user)
            actions = self.send(user=user, contact={"user_id": user, "phone_number": "+70000000011"})
        else:
            actions = self.click(consent.rsplit(":", 1)[0] + ":no", user)
        return self.button(actions, "submit:")

    def create(self, user=11, phone=False, photo=False):
        submit = self.draft(user=user, phone=phone, photo=photo)
        actions = self.click(submit, user)
        self.assertIn("создана", self.text(actions))
        return self.bot.db.execute("SELECT MAX(id) FROM tickets").fetchone()[0]

    def test_draft_not_submitted_until_confirmation(self):
        self.draft()
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)
        self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "confirm")
        self.send("/cancel")
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_contact_opt_out_is_saved_without_phone(self):
        tid = self.create()
        row = self.bot.db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        self.assertIsNone(row["contact_phone"])
        self.assertIsNone(row["contact_consent_at"])
        self.assertEqual(row["status"], "new")

    def test_phone_needs_consent_and_own_telegram_contact(self):
        self.send("/new")
        self.send("Тестовый телефон")
        self.send("Тест: разбит экран устройства")
        actions = self.send("/skip")
        self.send(contact={"user_id": 11, "phone_number": "+70000000011"})
        self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "consent")
        consent = self.button(actions, "consent:")
        self.click(consent.rsplit(":", 1)[0] + ":yes")
        for contact in ({"user_id": 12, "phone_number": "+70000000012"}, {"phone_number": "+70000000011"}, {"user_id": 11, "phone_number": "invalid"}):
            self.send(contact=contact)
            self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "contact")
        self.send("+70000000011")
        self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "contact")
        actions = self.send(contact={"user_id": 11, "phone_number": "+7 (000) 000-00-11"})
        self.click(self.button(actions, "submit:"))
        row = self.bot.db.execute("SELECT contact_phone,contact_consent_at FROM tickets").fetchone()
        self.assertEqual(row[0], "+70000000011")
        self.assertTrue(row[1])

    def test_consent_can_be_changed_by_skip_without_saving_phone(self):
        self.send("/new")
        self.send("Тестовое устройство")
        self.send("Описание тестовой неисправности")
        actions = self.send("/skip")
        consent = self.button(actions, "consent:")
        self.click(consent.rsplit(":", 1)[0] + ":yes")
        actions = self.send("/skip")
        self.click(self.button(actions, "submit:"))
        self.assertIsNone(self.bot.db.execute("SELECT contact_phone FROM tickets").fetchone()[0])

    def test_each_valid_status_transition_and_audit(self):
        tid = self.create()
        for source, target in [("new", "diagnostics"), ("diagnostics", "in_progress"), ("in_progress", "ready"), ("ready", "closed")]:
            actions = self.click(f"status:{tid}:{source}:{target}", 99)
            self.assertNotIn("запрещён", self.text(actions))
            self.assertEqual(self.bot.db.execute("SELECT status FROM tickets WHERE id=?", (tid,)).fetchone()[0], target)
        actions = self.send(f"/status {tid} diagnostics", 99)
        self.assertIn("запрещён", self.text(actions))
        events = self.bot.db.execute("SELECT from_status,to_status FROM history WHERE kind='status_changed'").fetchall()
        self.assertEqual([tuple(event) for event in events], [("new", "diagnostics"), ("diagnostics", "in_progress"), ("in_progress", "ready"), ("ready", "closed")])
        self.assertIn("Выдана", self.text(self.send(f"/history {tid}")))

    def test_every_pair_of_states_respects_transition_graph(self):
        for source in TRANSITIONS:
            for target in TRANSITIONS:
                with self.subTest(source=source, target=target):
                    tid = self.create()
                    self.bot.db.execute("UPDATE tickets SET status=? WHERE id=?", (source, tid))
                    self.bot.db.commit()
                    before = self.bot.db.execute("SELECT COUNT(*) FROM history WHERE ticket_id=?", (tid,)).fetchone()[0]
                    self.send(f"/status {tid} {target}", 99)
                    after = self.bot.db.execute("SELECT status FROM tickets WHERE id=?", (tid,)).fetchone()[0]
                    count = self.bot.db.execute("SELECT COUNT(*) FROM history WHERE ticket_id=?", (tid,)).fetchone()[0]
                    permitted = target in TRANSITIONS[source]
                    self.assertEqual(after, target if permitted else source)
                    self.assertEqual(count, before + int(permitted))

    def test_client_cannot_see_other_ticket_photo_phone_or_history(self):
        tid = self.create(phone=True, photo=True)
        for command in (f"/ticket {tid}", f"/history {tid}", f"/withdrawcontact {tid}", "/ticket 999999"):
            actions = self.send(command, 12)
            self.assertEqual(self.text(actions), DENIED)
            self.assertFalse(any(a["method"] == "sendPhoto" for a in actions))
        self.assertNotIn("Тестовый ноутбук", self.text(self.send("/mine", 12)))
        staff_actions = self.send(f"/ticket {tid}", 99)
        self.assertIn("+70000000011", self.text(staff_actions))
        self.assertTrue(any(a["method"] == "sendPhoto" for a in staff_actions))
        own_actions = self.send(f"/ticket {tid}", 11)
        self.assertNotIn("+70000000011", self.text(own_actions))
        self.assertIn("••• 0011", self.text(own_actions))

    def test_client_cannot_escalate_role_or_change_status(self):
        tid = self.create()
        for action in (lambda: self.send("/staff"), lambda: self.click("staff"), lambda: self.send(f"/status {tid} diagnostics"), lambda: self.click(f"status:{tid}:new:diagnostics")):
            self.assertIn("только сотрудникам", self.text(action()))
        self.assertEqual(self.bot.db.execute("SELECT status FROM tickets").fetchone()[0], "new")

    def test_stale_status_button_does_not_apply_to_new_state(self):
        tid = self.create()
        self.click(f"status:{tid}:new:diagnostics", 99)
        actions = self.click(f"status:{tid}:new:cancelled", 99)
        self.assertIn("устарела", self.text(actions))
        self.assertEqual(self.bot.db.execute("SELECT status FROM tickets").fetchone()[0], "diagnostics")

    def test_stale_submit_cannot_create_second_ticket(self):
        old_submit = self.draft()
        self.click(old_submit)
        current_submit = self.draft()
        self.assertNotEqual(current_submit, old_submit)
        self.assertIn("устарела", self.text(self.click(old_submit)))
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        self.click(current_submit)
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 2)

    def test_stale_consent_and_cancel_leave_current_draft(self):
        self.send("/new")
        self.send("Тестовый ноутбук")
        self.send("Тестовая неисправность устройства")
        old = self.send("/skip")
        old_consent = self.button(old, "consent:")
        old_cancel = self.button(old, "cancel:")
        self.send("/cancel")
        self.send("/new")
        self.send("Другой ноутбук")
        self.send("Другая неисправность устройства")
        self.send("/skip")
        self.assertIn("устарела", self.text(self.click(old_consent)))
        self.assertIn("устарела", self.text(self.click(old_cancel)))
        self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "consent")

    def test_update_deduplication_is_durable(self):
        submit = self.draft()
        update_id = self.update_id + 1
        update = {"update_id": update_id, "callback_query": {"id": "cb", "from": {"id": 11}, "message": {"chat": {"id": 11, "type": "private"}}, "data": submit}}
        self.bot.handle_update(update)
        queued = len(self.bot.pending_outbox())
        self.assertEqual(self.bot.handle_update(update), [])
        self.assertEqual(len(self.bot.pending_outbox()), queued)
        self.bot.close()
        self.bot = BotService(self.path, {99})
        self.assertEqual(self.bot.handle_update(update), [])
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        self.assertEqual(self.bot.get_offset(), update_id + 1)

    def test_draft_survives_restart(self):
        self.send("/new")
        self.send("Тестовый ноутбук")
        self.bot.close()
        self.bot = BotService(self.path, {99})
        actions = self.send("Описание после перезапуска")
        self.assertIn("Фото неисправности", self.text(actions))

    def test_photo_validation_rejects_documents_large_files_bad_ids(self):
        self.send("/new")
        self.send("Тестовый ноутбук")
        self.send("Тестовая неисправность устройства")
        for extra in ({"document": {"file_id": "bad"}}, {"photo": []}, {"photo": [None]}, {"photo": [{"file_id": "", "file_size": 1}]}, {"photo": [{"file_id": "x", "file_size": MAX_PHOTO_BYTES + 1}]}, {"photo": [{"file_id": "x", "file_size": -1}]}):
            self.send(**extra)
            self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "photo")
        actions = self.send(photo=[{"file_id": "real_file_id", "file_size": MAX_PHOTO_BYTES}])
        self.assertIn("Контакт по вашему желанию", self.text(actions))

    def test_text_boundaries_and_unknown_commands_preserve_step(self):
        self.send("/new")
        for value in ("ab", " " * 6, "x" * 121):
            self.send(value)
            self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "device")
        self.send("x" * 120)
        for value in ("short", "x" * 2001):
            self.send(value)
            self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "description")
        self.send("/unknown")
        self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "description")
        self.send("x" * 2000)
        self.assertEqual(self.bot.db.execute("SELECT step FROM sessions").fetchone()[0], "photo")

    def test_group_and_malformed_updates_create_no_tickets_or_drafts(self):
        malformed = [None, [], {}, {"update_id": True}, {"update_id": -1}, {"update_id": "1"}]
        for update in malformed:
            self.assertEqual(self.bot.handle_update(update), [])
        for payload in ({"message": None}, {"message": {}}, {"message": {"from": {"id": True}, "chat": {"type": "private", "id": True}, "text": "/new"}}, {"message": {"from": {"id": 11}, "chat": {"type": "group", "id": -1}, "text": "/new"}}, {"message": {"from": {"id": 11}, "chat": {"type": "private", "id": 12}, "text": "/new"}}):
            self.assertEqual(self.update(payload), [])
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_withdraw_contact_removes_local_copies_and_records_history(self):
        tid = self.create(phone=True)
        self.send(f"/ticket {tid}", 99)
        self.assertIn("+70000000011", " ".join(r[0] for r in self.bot.db.execute("SELECT params FROM outbox")))
        self.send(f"/withdrawcontact {tid}")
        row = self.bot.db.execute("SELECT contact_phone,contact_consent_at FROM tickets").fetchone()
        self.assertEqual(tuple(row), (None, None))
        self.assertNotIn("+70000000011", " ".join(r[0] for r in self.bot.db.execute("SELECT params FROM outbox")))
        self.assertIn("отозвал", self.text(self.send(f"/history {tid}")))
        self.assertIn("не сохранён", self.text(self.send(f"/withdrawcontact {tid}")))

    def test_staff_cannot_withdraw_someone_elses_contact(self):
        tid = self.create(phone=True)
        self.assertEqual(self.text(self.send(f"/withdrawcontact {tid}", 99)), DENIED)
        self.assertTrue(self.bot.db.execute("SELECT contact_phone FROM tickets").fetchone()[0])

    def test_withdraw_preserves_colliding_chat_and_service_ids_after_restart(self):
        staff, phone = 79990000000, "79990000000"  # Synthetic collision fixture.
        self.bot.close()
        self.bot = BotService(self.path, {staff})
        self.send("/new")
        self.send("Учебный телефон")
        self.send("Демонстрация: не включается экран")
        photo_id = "offline-photo-" + phone + "-test"
        consent = self.send(photo=[{"file_id": photo_id, "file_size": 128}])
        self.click(self.button(consent, "consent:").rsplit(":", 1)[0] + ":yes")
        confirmation = self.send(contact={"user_id": 11, "phone_number": phone})
        self.click(self.button(confirmation, "submit:"))
        self.send("/ticket 1", staff)
        callback_data = "ticket:" + phone
        with self.bot.db:
            self.bot.db.execute("INSERT INTO outbox(update_id,method,params) VALUES (?,'sendPhoto',?)", (self.update_id, json.dumps({"chat_id": staff, "photo": photo_id, "caption": "Контакт: " + phone, "reply_markup": {"inline_keyboard": [[{"text": "К заявке", "callback_data": callback_data}]]}})))
        before = {a["id"]: a["params"] for a in self.bot.pending_outbox()}
        self.assertTrue(any(phone in params.get("text", "") for params in before.values()))
        self.send("/withdrawcontact 1")
        for row in self.bot.db.execute("SELECT params FROM outbox"):
            params = json.loads(row["params"])
            for field in ("text", "caption"):
                self.assertNotIn(phone, params.get(field, ""))
        self.bot.close()
        self.bot = BotService(self.path, {staff})
        pending = self.bot.pending_outbox()
        for action in pending:
            if action["id"] in before:
                original, params = before[action["id"]], action["params"]
                self.assertEqual({k: v for k, v in params.items() if k not in ("text", "caption")}, {k: v for k, v in original.items() if k not in ("text", "caption")})
        self.assertIsNone(self.bot.db.execute("SELECT contact_phone FROM tickets WHERE id=1").fetchone()[0])
        self.assertEqual(TelegramRunner(Mock(), self.bot).drain_outbox(), len(pending))
        self.assertEqual(self.bot.pending_outbox(), [])

    def test_outbox_mark_sent_retry_and_dead_are_persistent(self):
        self.send("/help")
        first = self.bot.pending_outbox()[0]
        self.bot.mark_failed(first["id"], "network_timeout")
        self.assertEqual(self.bot.pending_outbox()[0]["attempts"], 1)
        self.bot.mark_failed(first["id"], "token:123456:secret +70000000011")
        self.assertEqual(self.bot.db.execute("SELECT last_error FROM outbox WHERE id=?", (first["id"],)).fetchone()[0], "delivery_error")
        self.bot.mark_dead(first["id"], "forbidden")
        self.assertEqual(self.bot.pending_outbox(), [])
        self.send("/help")
        second = self.bot.pending_outbox()[0]
        self.bot.mark_sent(second["id"])
        self.assertEqual(self.bot.pending_outbox(), [])
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            self.bot.pending_outbox(0)

    def test_transaction_rolls_back_receipt_outbox_and_business_write_on_error(self):
        self.bot.db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        self.bot.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.send("/new")
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM processed_updates").fetchone()[0], 0)
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_large_ids_are_rejected_without_database_overflow(self):
        self.assertEqual(self.bot.handle_update({"update_id": 2**80}), [])
        self.assertEqual(self.send("/new", user=2**80), [])
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_poll_cursor_recovers_after_a_week_and_accepts_lower_ids(self):
        self.bot.handle_update({"update_id": 500, "message": {"from": {"id": 11}, "chat": {"id": 11, "type": "private"}, "text": "/help"}})
        self.assertEqual(self.bot.get_offset(), 501)
        self.bot.db.execute("UPDATE polling_cursor SET updated_at='2026-01-01T00:00:00+00:00'")
        self.bot.db.commit()
        self.assertEqual(self.bot.get_offset(), 0)
        self.bot.handle_update({"update_id": 3, "message": {"from": {"id": 11}, "chat": {"id": 11, "type": "private"}, "text": "/help"}})
        self.assertEqual(self.bot.get_offset(), 4)
        self.bot.db.execute("UPDATE processed_updates SET created_at='2026-01-01T00:00:00+00:00' WHERE update_id=500")
        self.bot.db.commit()
        self.assertTrue(self.bot.handle_update({"update_id": 500, "message": {"from": {"id": 11}, "chat": {"id": 11, "type": "private"}, "text": "/help"}}))
        self.assertEqual(self.bot.get_offset(), 501)

    def test_emoji_and_long_history_fit_telegram_message_limit(self):
        self.send("/new")
        self.send("🔧" * 120)
        self.send("🔧" * 2000)
        actions = self.send("/skip")
        consent = self.button(actions, "consent:")
        actions = self.click(consent.rsplit(":", 1)[0] + ":no")
        self.click(self.button(actions, "submit:"))
        actions = self.send("/ticket 1")
        self.assertGreater(len(actions), 1)
        self.assertTrue(all(len(a["params"]["text"].encode("utf-16-le")) // 2 <= 4000 for a in actions))
        for _ in range(150):
            self.bot.db.execute("INSERT INTO history(ticket_id,actor_id,kind,from_status,to_status,created_at) VALUES (1,99,'status_changed','new','diagnostics','2026-11-15T12:00:00Z')")
        self.bot.db.commit()
        history = self.send("/history 1")
        self.assertGreater(len(history), 1)
        self.assertTrue(all(len(a["params"]["text"].encode("utf-16-le")) // 2 <= 4000 for a in history))
        self.assertIn("reply_markup", history[-1]["params"])
        self.assertNotIn("reply_markup", history[0]["params"])


if __name__ == "__main__":
    unittest.main()
