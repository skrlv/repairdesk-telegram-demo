import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

from repairbot.core import BotService
from repairbot.telegram import (
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, TelegramClient, TelegramError, TelegramRunner,
)


TOKEN = "123456:" + "x" * 35  # Synthetic fixture; every network call is replaced.


def reply(data):
    return io.BytesIO(json.dumps(data).encode("utf-8"))


def message(update_id, text="/start", uid=11):
    return {"update_id": update_id, "message": {
        "from": {"id": uid}, "chat": {"id": uid, "type": "private"}, "text": text,
    }}


class TelegramClientTest(unittest.TestCase):
    def test_constructing_client_never_contacts_telegram(self):
        opener = Mock(side_effect=AssertionError("Unexpected network"))
        client = TelegramClient(TOKEN, opener=opener)
        opener.assert_not_called()
        self.assertNotIn(TOKEN, repr(client))

    def test_https_post_utf8_and_long_poll_timeout(self):
        opener = Mock(return_value=reply({"ok": True, "result": []}))
        client = TelegramClient(TOKEN, opener=opener, io_timeout=15)
        self.assertEqual(client.request("getUpdates", {"timeout": 25, "note": "Привет"}), [])
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.telegram.org/bot" + TOKEN + "/getUpdates")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertIn("Привет".encode(), request.data)
        self.assertEqual(opener.call_args.kwargs["timeout"], 40)

    def test_success_result_is_returned_unchanged(self):
        result = {"id": 123456, "is_bot": True}
        client = TelegramClient(TOKEN, opener=Mock(return_value=reply({"ok": True, "result": result})))
        self.assertEqual(client.request("getMe", {}), result)

    def test_invalid_local_requests_fail_without_network(self):
        opener = Mock(side_effect=AssertionError("Unexpected network"))
        client = TelegramClient(TOKEN, opener=opener)
        invalid = [
            ("deleteWebhook", {}), ([], {}), ("getMe", []), ("getMe", {"x": float("nan")}),
            ("getMe", {"x": object()}), ("getUpdates", {"timeout": True}),
            ("getUpdates", {"timeout": 51}), ("getUpdates", {"timeout": -1}),
        ]
        for method, params in invalid:
            with self.subTest(method=str(method)):
                with self.assertRaises(TelegramError) as caught:
                    client.request(method, params)
                self.assertEqual(caught.exception.code, "invalid_request")
        with self.assertRaises(TelegramError) as caught:
            client.request("sendMessage", {"text": "x" * MAX_REQUEST_BYTES})
        self.assertEqual(caught.exception.code, "request_too_large")
        opener.assert_not_called()

    def test_network_errors_hide_url_token_and_upstream_text(self):
        for error in (urllib.error.URLError("https://api.telegram.org/bot" + TOKEN), TimeoutError(TOKEN), OSError(TOKEN)):
            with self.subTest(error_type=type(error).__name__):
                client = TelegramClient(TOKEN, opener=Mock(side_effect=error))
                with self.assertRaises(TelegramError) as caught:
                    client.request("getMe", {})
                self.assertTrue(caught.exception.temporary)
                self.assertEqual(caught.exception.code, "network")
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn(TOKEN, repr(caught.exception))

    def test_http_error_uses_safe_code_and_preserves_full_retry_after(self):
        body = reply({"ok": False, "error_code": 429, "description": TOKEN, "parameters": {"retry_after": 321}})
        error = urllib.error.HTTPError("https://api.telegram.org/bot" + TOKEN, 429, TOKEN, {}, body)
        client = TelegramClient(TOKEN, opener=Mock(side_effect=error))
        with self.assertRaises(TelegramError) as caught:
            client.request("getMe", {})
        self.assertEqual(caught.exception.retry_after, 321)
        self.assertTrue(caught.exception.temporary)
        self.assertEqual(caught.exception.error_code, 429)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertTrue(body.closed)

    def test_api_failures_treat_auth_conflict_and_temporary_errors_differently(self):
        for code in (400, 401, 403, 409, 429, 500, 503):
            with self.subTest(code=code):
                client = TelegramClient(TOKEN, opener=Mock(return_value=reply({
                    "ok": False, "error_code": code, "description": TOKEN,
                })))
                with self.assertRaises(TelegramError) as caught:
                    client.request("getMe", {})
                self.assertEqual(caught.exception.error_code, code)
                self.assertEqual(caught.exception.temporary, code == 429 or code >= 500)
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_malformed_or_oversize_api_response_is_retryable(self):
        for raw in (b"not JSON " + TOKEN.encode(), b"\xff", b"[]", b"null", b'{}', b'{"ok":true}', b'{"ok":false,"error_code":true}', b"x" * (MAX_RESPONSE_BYTES + 1)):
            with self.subTest(length=len(raw)):
                client = TelegramClient(TOKEN, opener=Mock(return_value=io.BytesIO(raw)))
                with self.assertRaises(TelegramError) as caught:
                    client.request("getMe", {})
                self.assertTrue(caught.exception.temporary)
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_invalid_error_code_is_rejected_as_malformed_response(self):
        client = TelegramClient(TOKEN, opener=Mock(return_value=reply({"ok": False, "error_code": -1})))
        with self.assertRaises(TelegramError) as caught:
            client.request("getMe", {})
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertTrue(caught.exception.temporary)


class TelegramRunnerTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / "bot.db")
        self.service = BotService(self.path, {99})
        self.client = Mock()
        self.logs = []
        self.sleeps = []
        self.runner = TelegramRunner(self.client, self.service, sleep=self.sleeps.append, logger=self.logs.append)

    def tearDown(self):
        self.service.close()
        self.folder.cleanup()

    def outbox(self):
        return self.service.db.execute("SELECT * FROM outbox ORDER BY id").fetchall()

    def test_inbound_updates_are_sorted_and_duplicate_receipts_do_not_duplicate_replies(self):
        self.client.request.side_effect = lambda method, params: [message(12), message(10), message(12)] if method == "getUpdates" else {"message_id": 1}
        self.assertEqual(self.runner.run_once(), 3)
        self.assertEqual(self.service.get_offset(), 13)
        self.assertEqual(self.service.db.execute("SELECT COUNT(*) FROM processed_updates").fetchone()[0], 2)
        self.assertEqual(len(self.outbox()), 2)
        self.assertTrue(all(row["sent_at"] for row in self.outbox()))
        self.runner.run_once()
        self.assertEqual(len(self.outbox()), 2)
        get_updates = [call for call in self.client.request.call_args_list if call.args[0] == "getUpdates"]
        self.assertEqual(get_updates[0].args[1], {"offset": 0, "timeout": 25, "limit": 100, "allowed_updates": ["message", "callback_query"]})
        self.assertEqual(get_updates[1].args[1]["offset"], 13)

    def test_pending_messages_are_sent_before_polling_new_updates(self):
        self.service.handle_update(message(1))
        self.client.request.side_effect = lambda method, params: [] if method == "getUpdates" else True
        self.runner.run_once()
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["sendMessage", "getUpdates"])
        self.assertEqual(self.outbox()[0]["attempts"], 1)

    def test_permanent_delivery_failure_is_retained_and_does_not_stop_other_messages(self):
        self.service.handle_update(message(1))
        self.service.handle_update(message(2, uid=12))
        self.client.request.side_effect = [TelegramError("telegram_403", error_code=403), True]
        self.assertEqual(self.runner.drain_outbox(), 1)
        rows = self.outbox()
        self.assertIsNotNone(rows[0]["dead_at"])
        self.assertEqual(rows[0]["last_error"], "telegram_403")
        self.assertIsNotNone(rows[1]["sent_at"])
        self.assertEqual(len(self.logs), 1)

    def test_transient_or_global_auth_conflict_error_preserves_outbox_for_retry(self):
        for code, temporary in ((401, False), (409, False), (429, True), (503, True)):
            with self.subTest(code=code):
                self.service.handle_update(message(code))
                self.client.request.side_effect = TelegramError("telegram_%s" % code, error_code=code, temporary=temporary)
                with self.assertRaises(TelegramError):
                    self.runner.drain_outbox()
                first = self.service.pending_outbox()[0]
                self.assertGreater(first["attempts"], 0)
                self.assertTrue(all(row["dead_at"] is None and row["sent_at"] is None for row in self.outbox()))

    def test_outbox_survives_restart_and_is_recovered_once(self):
        self.service.handle_update(message(1))
        self.service.close()
        self.service = BotService(self.path, {99})
        runner = TelegramRunner(self.client, self.service, logger=self.logs.append)
        self.client.request.return_value = True
        self.assertEqual(runner.drain_outbox(), 1)
        self.assertEqual(runner.drain_outbox(), 0)
        self.assertEqual(self.client.request.call_count, 1)

    def test_invalid_stored_method_is_not_executed(self):
        self.service.handle_update(message(1))
        with self.service.db:
            self.service.db.execute("UPDATE outbox SET method='deleteWebhook'")
        self.assertEqual(self.runner.drain_outbox(), 0)
        self.client.request.assert_not_called()
        self.assertEqual(self.outbox()[0]["last_error"], "invalid_outbox_method")

    def test_invalid_update_batch_is_not_partially_committed(self):
        for batch in ({}, [message(2), {"update_id": True}], [message(2**80)], [message(3)] * 101):
            with self.subTest(batch_type=type(batch).__name__):
                self.client.request.return_value = batch
                with self.assertRaises(TelegramError):
                    self.runner.run_once()
                self.assertEqual(self.service.get_offset(), 0)

    def test_backoff_uses_retry_after_and_stop_prevents_real_sleep(self):
        calls = []
        def run_once():
            calls.append(1)
            if len(calls) == 1:
                raise TelegramError("telegram_429", error_code=429, temporary=True, retry_after=321)
        self.runner.run_once = run_once
        self.runner.run_forever(stop=lambda: len(calls) >= 2)
        self.assertEqual(self.sleeps, [321])
        self.assertIn("321", self.logs[0])

    def test_backoff_caps_local_exponential_delay_and_auth_error_stops(self):
        calls = []
        def run_once():
            calls.append(1)
            if len(calls) <= 6:
                raise TelegramError("network", temporary=True)
            raise TelegramError("telegram_401", error_code=401)
        self.runner.run_once = run_once
        with self.assertRaises(TelegramError):
            self.runner.run_forever()
        self.assertEqual(self.sleeps, [2, 4, 8, 16, 30, 30])


if __name__ == "__main__":
    unittest.main()
