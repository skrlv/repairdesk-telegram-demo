"""Loopback-only demonstration of the actual bot core. Never contacts Telegram."""
import json
import mimetypes
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import BotService

ACTORS = (
    {"id": "client", "name": "Клиент А", "telegram_id": 1101},
    {"id": "other", "name": "Клиент Б", "telegram_id": 1102},
    {"id": "staff", "name": "Сотрудник", "telegram_id": 9901},
)
BY_ACTOR = {item["id"]: item for item in ACTORS}
BY_CHAT = {item["telegram_id"]: item["id"] for item in ACTORS}
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class DemoError(ValueError):
    pass


class DemoApplication:
    def __init__(self, db_path):
        self.lock = threading.RLock()
        self.service = BotService(str(db_path), staff_ids={9901})
        self.history_db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.history_db.execute("""CREATE TABLE IF NOT EXISTS demo_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL, direction TEXT NOT NULL,
            method TEXT NOT NULL, params TEXT NOT NULL,
            outbox_id INTEGER UNIQUE
        )""")
        self.history_db.commit()
        with self.lock:
            self._drain()

    def _record(self, actor, direction, method, params, outbox_id=None):
        with self.history_db:
            self.history_db.execute(
                "INSERT OR IGNORE INTO demo_messages(actor,direction,method,params,outbox_id) VALUES(?,?,?,?,?)",
                (actor, direction, method, json.dumps(params, ensure_ascii=False), outbox_id),
            )

    def _drain(self):
        delivered = []
        for action in self.service.pending_outbox(limit=1000):
            params = action["params"]
            actor = BY_CHAT.get(params.get("chat_id"))
            if action["method"] == "answerCallbackQuery":
                pieces = str(params.get("callback_query_id", "")).split(":")
                actor = pieces[1] if len(pieces) == 3 and pieces[0] == "demo" and pieces[1] in BY_ACTOR else None
            if actor:
                self._record(actor, "out", action["method"], params, action["id"])
                delivered.append({"direction": "out", "method": action["method"], "params": params})
            # This is an offline sink; it acknowledges only in this separate demo database.
            self.service.mark_sent(action["id"])
        return delivered

    def history(self, actor):
        if not isinstance(actor, str) or actor not in BY_ACTOR:
            raise DemoError("Неизвестная учебная роль.")
        with self.lock:
            rows = self.history_db.execute(
                "SELECT direction,method,params FROM demo_messages WHERE actor=? ORDER BY id", (actor,)
            ).fetchall()
            return {"messages": [{"direction": row[0], "method": row[1], "params": json.loads(row[2])} for row in rows]}

    def update(self, payload):
        if not isinstance(payload, dict):
            raise DemoError("Ожидается объект JSON.")
        actor = payload.get("actor")
        if not isinstance(actor, str) or actor not in BY_ACTOR:
            raise DemoError("Выберите учебную роль.")
        kinds = [key for key in ("text", "callback_data", "photo", "contact") if key in payload]
        if len(kinds) != 1:
            raise DemoError("Передайте одно сообщение или действие.")
        kind = kinds[0]
        value = payload[kind]
        if kind == "text" and (not isinstance(value, str) or not value.strip() or len(value) > 4096):
            raise DemoError("Сообщение должно содержать от 1 до 4096 символов.")
        if kind == "callback_data" and (not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 64):
            raise DemoError("Некорректная кнопка.")
        if kind in ("photo", "contact") and value is not True:
            raise DemoError("Доступны только учебные вложения.")
        with self.lock:
            last_id = self.service.db.execute("SELECT COALESCE(MAX(update_id),0) FROM processed_updates").fetchone()[0]
            update_id = last_id + 1
            user_id = BY_ACTOR[actor]["telegram_id"]
            user = {"id": user_id, "is_bot": False, "first_name": BY_ACTOR[actor]["name"]}
            message = {"message_id": update_id, "date": int(time.time()), "chat": {"id": user_id, "type": "private"}, "from": user}
            incoming_method = "sendMessage"
            incoming_params = {}
            if kind == "callback_data":
                label = payload.get("label")
                if not isinstance(label, str) or not 1 <= len(label) <= 100:
                    label = "Действие: " + value
                update = {"update_id": update_id, "callback_query": {
                    "id": "demo:%s:%s" % (actor, update_id), "from": user,
                    "chat_instance": "offline-demo", "message": message, "data": value,
                }}
                incoming_params = {"text": label}
            else:
                if kind == "text":
                    message["text"] = value.strip()
                    incoming_params = {"text": value.strip()}
                elif kind == "photo":
                    message["photo"] = [{"file_id": "demo-photo", "file_unique_id": "offline-sample", "width": 600, "height": 400, "file_size": 1000}]
                    incoming_method = "sendPhoto"
                    incoming_params = {"photo": "demo-photo", "caption": "Учебное фото неисправности · демонстрация"}
                else:
                    # A non-dialable, explicitly synthetic fixture; never a person's phone number.
                    message["contact"] = {"phone_number": "+00000000000", "first_name": "Учебный контакт", "user_id": user_id}
                    incoming_params = {"text": "Передан учебный контакт (демо)"}
                update = {"update_id": update_id, "message": message}
            self.service.handle_update(update)
            self._record(actor, "in", incoming_method, incoming_params)
            return {"messages": self._drain(), "mode": "local", "update_id": update_id}

    def close(self):
        self.history_db.close()
        self.service.close()


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, application):
        self.application = application
        super().__init__(address, DemoHandler)


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "RepairDeskDemo/1.0"

    def log_message(self, *args):
        # Do not log URLs, user text, contacts or callback data.
        pass

    def _allowed_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in {"127.0.0.1:%s" % port, "localhost:%s" % port}

    def _headers(self, status, content_type, length):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def do_GET(self):
        if not self._allowed_host():
            self._json(403, {"error": "Стенд доступен только по локальному адресу."})
            return
        path = urlsplit(self.path)
        if path.path == "/api/session":
            self._json(200, {"mode": "local", "actors": list(ACTORS), "messages": []})
            return
        if path.path == "/api/history":
            try:
                actor = parse_qs(path.query).get("actor", [""])[0]
                self._json(200, self.server.application.history(actor))
            except DemoError as error:
                self._json(400, {"error": str(error)})
            return
        if path.path.startswith("/api/"):
            self._json(404, {"error": "Такого адреса нет."})
            return
        relative = "index.html" if path.path == "/" else path.path.lstrip("/")
        file_path = (WEB_ROOT / relative).resolve()
        try:
            file_path.relative_to(WEB_ROOT)
        except ValueError:
            self._json(404, {"error": "Файл не найден."})
            return
        if not file_path.is_file():
            self._json(404, {"error": "Файл не найден."})
            return
        body = file_path.read_bytes()
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def do_POST(self):
        if not self._allowed_host():
            self._json(403, {"error": "Недопустимый адрес стенда."})
            return
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        if origin and origin not in {"http://127.0.0.1:%s" % port, "http://localhost:%s" % port}:
            self._json(403, {"error": "Запрос с другого сайта запрещён."})
            return
        if self.path != "/api/update":
            self._json(404, {"error": "Такого адреса нет."})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._json(415, {"error": "Нужен запрос application/json."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                self._json(413, {"error": "Слишком большой или пустой запрос."})
                return
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            result = self.server.application.update(payload)
            self._json(200, result)
        except (ValueError, UnicodeError) as error:
            message = str(error) if isinstance(error, DemoError) else "Некорректный JSON или запрос."
            self._json(400, {"error": message})
        except Exception:
            self._json(500, {"error": "Не удалось обработать сообщение. Обновите историю перед повтором."})


def run_demo(db_path, port=8783):
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    application = DemoApplication(path)
    server = DemoServer(("127.0.0.1", port), application)
    print("Демонстрация: http://127.0.0.1:%s/" % server.server_address[1], flush=True)
    print("Без Telegram и без токена. Только учебные данные. Остановка: Ctrl+C.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        application.close()
