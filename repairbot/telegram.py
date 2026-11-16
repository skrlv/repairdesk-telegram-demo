"""Opt-in HTTPS Bot API transport and durable-outbox long polling.

No network request is made on import or construction. No automatic webhook or
bot configuration mutations are performed. Delivery is at-least-once: Telegram
has no idempotency key for sendMessage, so a crash after delivery and before the
SQLite acknowledgement may produce a duplicate reply.
"""

import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from .config import validate_token

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
ALLOWED_METHODS = frozenset({
    "getMe", "getWebhookInfo", "getUpdates", "sendMessage", "sendPhoto", "answerCallbackQuery"
})
OUTBOX_METHODS = frozenset({"sendMessage", "sendPhoto", "answerCallbackQuery"})


class TelegramError(RuntimeError):
    """Safe exception: never stores a URL, token, upstream description or body."""

    def __init__(
        self, code: str, error_code: Optional[int] = None,
        retry_after: Optional[int] = None, temporary: bool = False,
    ) -> None:
        # Caller-controlled strings are never retained. The only accepted codes
        # below are local constants or integer-derived HTTP/API codes.
        safe_codes = {"network", "invalid_response", "invalid_request", "request_too_large"}
        if code not in safe_codes and not (
            code.startswith(("http_", "telegram_")) and code.rsplit("_", 1)[-1].isdigit()
        ):
            code = "invalid_response"
        self.code = code
        self.error_code = error_code if type(error_code) is int else None
        self.temporary = bool(temporary)
        self.retry_after = (
            retry_after if type(retry_after) is int and retry_after > 0 else None
        )
        if self.error_code == 401:
            message = "Telegram отклонил токен. Проверьте BOT_TOKEN; значение скрыто."
        elif self.error_code == 409:
            message = (
                "Конфликт Telegram: проверьте, что работает один процесс long polling "
                "и webhook отключён вручную. Настройки бота автоматически не меняются."
            )
        elif self.error_code == 429:
            message = "Telegram ограничил частоту запросов. Нужна пауза перед повтором."
        elif code == "network":
            message = "Сеть Telegram недоступна; запрос можно повторить."
        elif code == "invalid_response":
            message = "Telegram вернул неподдерживаемый ответ; запрос можно повторить."
        else:
            message = "Запрос Telegram не выполнен (безопасный код: %s)." % code
        super().__init__(message)


def _retry_after(payload: Any) -> Optional[int]:
    if isinstance(payload, dict) and isinstance(payload.get("parameters"), dict):
        value = payload["parameters"].get("retry_after")
        if type(value) is int and value > 0:
            return value
    return None


def _api_error(status: int, payload: Any = None) -> TelegramError:
    if type(status) is not int or not 100 <= status <= 599:
        return TelegramError("invalid_response", temporary=True)
    code = status
    if isinstance(payload, dict):
        candidate = payload.get("error_code")
        if type(candidate) is int and 100 <= candidate <= 599:
            code = candidate
    return TelegramError(
        "telegram_%d" % code, error_code=code,
        retry_after=_retry_after(payload), temporary=(code == 429 or code >= 500),
    )


def _decode(raw: bytes) -> Dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TelegramError("invalid_response", temporary=True)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise TelegramError("invalid_response", temporary=True) from None
    if not isinstance(data, dict):
        raise TelegramError("invalid_response", temporary=True)
    return data


class TelegramClient:
    def __init__(
        self, token: str, opener: Optional[Callable[..., Any]] = None,
        io_timeout: int = 15,
    ) -> None:
        validate_token(token)
        self._token = token
        self._opener = opener or urllib.request.urlopen
        self.io_timeout = max(5, min(int(io_timeout), 60))

    def __repr__(self) -> str:
        return "TelegramClient(token=<hidden>)"

    def request(self, method: str, params: Dict[str, Any]) -> Any:
        if not isinstance(method, str) or method not in ALLOWED_METHODS or not isinstance(params, dict):
            raise TelegramError("invalid_request")
        try:
            body = json.dumps(params, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise TelegramError("invalid_request") from None
        if len(body) > MAX_REQUEST_BYTES:
            raise TelegramError("request_too_large")
        timeout = self.io_timeout
        if method == "getUpdates":
            poll = params.get("timeout", 0)
            if type(poll) is not int or not 0 <= poll <= 50:
                raise TelegramError("invalid_request")
            timeout += poll
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self._token + "/" + method,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "RepairdeskDemo/1.0"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code if type(exc.code) is int else 500
            payload = None
            try:
                payload = _decode(exc.read(MAX_RESPONSE_BYTES + 1))
            except (TelegramError, OSError, http.client.HTTPException):
                pass
            finally:
                exc.close()
            raise _api_error(status, payload) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            # Exception text can contain the token-bearing request URL.
            raise TelegramError("network", temporary=True) from None
        data = _decode(raw)
        if data.get("ok") is True and "result" in data:
            return data["result"]
        if data.get("ok") is False and type(data.get("error_code")) is int:
            raise _api_error(data["error_code"], data)
        raise TelegramError("invalid_response", temporary=True)


class TelegramRunner:
    """Use one runner per SQLite database and one long-polling process per bot."""

    def __init__(
        self, client: TelegramClient, service: Any, poll_timeout: int = 25,
        sleep: Callable[[float], None] = time.sleep, logger: Any = None,
    ) -> None:
        if type(poll_timeout) is not int or not 1 <= poll_timeout <= 50:
            raise ValueError("poll_timeout должен быть от 1 до 50 секунд.")
        self.client = client
        self.service = service
        self.poll_timeout = poll_timeout
        self.sleep = sleep
        self.logger = logger

    def _log(self, message: str) -> None:
        if self.logger is None:
            logging.getLogger(__name__).warning(message)
        elif callable(self.logger):
            self.logger(message)
        else:
            self.logger.warning(message)

    def drain_outbox(self) -> int:
        """Stop on transient failures; retain permanent failures as dead letters."""
        sent = 0
        while True:
            pending = self.service.pending_outbox(limit=100)
            if not pending:
                return sent
            for action in pending:
                action_id = action["id"]
                method = action["method"]
                if method not in OUTBOX_METHODS:
                    self.service.mark_dead(action_id, "invalid_outbox_method")
                    self._log("Исходящее действие остановлено: invalid_outbox_method.")
                    continue
                try:
                    self.client.request(method, action["params"])
                except TelegramError as exc:
                    if exc.temporary or exc.error_code in (401, 409):
                        self.service.mark_failed(action_id, exc.code)
                        raise
                    self.service.mark_dead(action_id, exc.code)
                    self._log("Исходящее действие остановлено: %s." % exc.code)
                    continue
                self.service.mark_sent(action_id)
                sent += 1

    def run_once(self) -> int:
        self.drain_outbox()
        updates = self.client.request("getUpdates", {
            "offset": self.service.get_offset(), "timeout": self.poll_timeout,
            "limit": 100, "allowed_updates": ["message", "callback_query"],
        })
        if not isinstance(updates, list) or len(updates) > 100:
            raise TelegramError("invalid_response", temporary=True)
        for update in updates:
            if not isinstance(update, dict) or type(update.get("update_id")) is not int or not 0 <= update["update_id"] < 2**63 - 1:
                raise TelegramError("invalid_response", temporary=True)
        processed = 0
        for update in sorted(updates, key=lambda item: item["update_id"]):
            self.service.handle_update(update)
            processed += 1
        self.drain_outbox()
        return processed

    def run_forever(self, stop: Optional[Callable[[], bool]] = None) -> None:
        failures = 0
        should_stop = stop or (lambda: False)
        while not should_stop():
            try:
                self.run_once()
                failures = 0
            except TelegramError as exc:
                if not exc.temporary:
                    self._log(str(exc))
                    raise
                failures += 1
                delay = exc.retry_after or min(2 ** min(failures, 5), 30)
                self._log("Повтор Telegram через %d с; код: %s." % (delay, exc.code))
                if not should_stop():
                    self.sleep(delay)
