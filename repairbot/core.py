"""Application core. It accepts Telegram Update dictionaries and never uses the network.

All client/staff access checks live here, not in the presentation. Updates, business
changes and the durable outgoing queue are committed in one SQLite transaction.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

STATUSES = {
    "new": "Новая", "diagnostics": "Диагностика", "in_progress": "В ремонте",
    "ready": "Готова к выдаче", "closed": "Выдана", "cancelled": "Отменена",
}
TRANSITIONS = {
    "new": ("diagnostics", "cancelled"),
    "diagnostics": ("in_progress", "cancelled"),
    "in_progress": ("ready", "cancelled"),
    "ready": ("closed",), "closed": (), "cancelled": (),
}
MAX_PHOTO_BYTES = 10 * 1024 * 1024
DENIED = "Заявка недоступна. Проверьте номер или откройте список своих заявок: /mine."


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2**63 - 1


def _bounded_messages(actions):
    """Keep every plain-text message below Telegram's UTF-16 length budget."""
    result = []
    for action in actions:
        if action["method"] != "sendMessage":
            result.append(action)
            continue
        params = action["params"]
        chunks, chunk, units = [], [], 0
        for char in params["text"]:
            width = 2 if ord(char) > 0xFFFF else 1
            if units + width > 4000:
                chunks.append("".join(chunk))
                chunk, units = [], 0
            chunk.append(char)
            units += width
        chunks.append("".join(chunk))
        for index, text in enumerate(chunks):
            part = {**params, "text": text}
            if index < len(chunks) - 1:
                part.pop("reply_markup", None)
            result.append({"method": "sendMessage", "params": part})
    return result


def _safe_reason(value):
    # The transport passes codes, never raw exceptions or Telegram response bodies.
    return value if isinstance(value, str) and re.fullmatch(r"(?:telegram_[0-9]{3}|[A-Za-z_:-]{1,80})", value) else "delivery_error"


class BotService:
    def __init__(self, db_path: str, staff_ids: set[int]):
        if any(not _positive(uid) for uid in staff_ids):
            raise ValueError("staff_ids must contain positive integer Telegram user IDs")
        self.staff_ids = frozenset(staff_ids)
        self._lock = threading.RLock()
        if db_path != ":memory:":
            db_path = str(Path(db_path).expanduser().resolve())
            Path(db_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY, step TEXT NOT NULL,
                draft TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL, device TEXT NOT NULL,
                description TEXT NOT NULL, photo_id TEXT,
                contact_phone TEXT, contact_consent_at TEXT,
                status TEXT NOT NULL CHECK(status IN
                    ('new','diagnostics','in_progress','ready','closed','cancelled')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tickets_owner ON tickets(owner_id, id);
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                actor_id INTEGER NOT NULL, kind TEXT NOT NULL,
                from_status TEXT, to_status TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS polling_cursor (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                update_id INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER NOT NULL REFERENCES processed_updates(update_id),
                method TEXT NOT NULL, params TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, sent_at TEXT, dead_at TEXT
            );
        """)
        self.db.execute("INSERT OR IGNORE INTO polling_cursor SELECT 1,update_id,created_at FROM processed_updates ORDER BY update_id DESC LIMIT 1")
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def get_offset(self) -> int:
        with self._lock:
            cursor = self.db.execute("SELECT update_id,updated_at FROM polling_cursor WHERE singleton=1").fetchone()
            if cursor is None:
                return 0
            # Telegram can choose a random update_id after a week of inactivity.
            # Its update retention is at most 24h, so dropping a stale offset is safe.
            if datetime.fromisoformat(cursor["updated_at"]) < datetime.now(timezone.utc) - timedelta(days=6):
                return 0
            return cursor["update_id"] + 1

    def pending_outbox(self, limit=100):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            rows = self.db.execute(
                "SELECT id,method,params,attempts FROM outbox WHERE sent_at IS NULL AND dead_at IS NULL ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            return [{"id": r["id"], "method": r["method"], "params": json.loads(r["params"]), "attempts": r["attempts"]} for r in rows]

    def mark_sent(self, outbox_id):
        with self._lock, self.db:
            self.db.execute("UPDATE outbox SET sent_at=?,attempts=attempts+1,last_error=NULL WHERE id=? AND sent_at IS NULL AND dead_at IS NULL", (_now(), outbox_id))

    def mark_failed(self, outbox_id, safe_reason):
        with self._lock, self.db:
            self.db.execute("UPDATE outbox SET attempts=attempts+1,last_error=? WHERE id=? AND sent_at IS NULL AND dead_at IS NULL", (_safe_reason(safe_reason), outbox_id))

    def mark_dead(self, outbox_id, safe_reason):
        with self._lock, self.db:
            self.db.execute("UPDATE outbox SET dead_at=?,attempts=attempts+1,last_error=? WHERE id=? AND sent_at IS NULL AND dead_at IS NULL", (_now(), _safe_reason(safe_reason), outbox_id))

    def handle_update(self, update: dict) -> list[dict]:
        if not isinstance(update, dict):
            return []
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool) or not 0 <= update_id < 2**63 - 1:
            return []
        with self._lock, self.db:
            previous = self.db.execute("SELECT created_at FROM processed_updates WHERE update_id=?", (update_id,)).fetchone()
            if previous and datetime.fromisoformat(previous["created_at"]) >= datetime.now(timezone.utc) - timedelta(days=7):
                return []
            self.db.execute("INSERT INTO processed_updates VALUES (?,?) ON CONFLICT(update_id) DO UPDATE SET created_at=excluded.created_at", (update_id, _now()))
            actions = _bounded_messages(self._process(update))
            for action in actions:
                self.db.execute("INSERT INTO outbox(update_id,method,params) VALUES (?,?,?)", (update_id, action["method"], json.dumps(action["params"], ensure_ascii=False)))
            self.db.execute("INSERT INTO polling_cursor VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET update_id=excluded.update_id,updated_at=excluded.updated_at", (update_id, _now()))
            return actions

    @staticmethod
    def _message(chat_id, text, markup=None):
        params = {"chat_id": chat_id, "text": text}
        if markup is not None:
            params["reply_markup"] = markup
        return {"method": "sendMessage", "params": params}

    @staticmethod
    def _inline(rows):
        return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}

    def _home(self, staff=False):
        rows = [[("Новая заявка", "new"), ("Мои заявки", "mine")]]
        if staff:
            rows.append([("Очередь мастерской", "staff")])
        return self._inline(rows)

    def _process(self, update):
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            user = callback.get("from")
            message = callback.get("message")
        else:
            callback = None
            message = update.get("message")
            user = message.get("from") if isinstance(message, dict) else None
        if not isinstance(user, dict) or not _positive(user.get("id")) or user.get("is_bot"):
            return []
        uid = user["id"]
        if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
            return self._answer(callback, "Откройте личный чат с ботом.")
        chat = message["chat"]
        if chat.get("type") != "private" or chat.get("id") != uid:
            return self._answer(callback, "Заявки доступны только в личном чате с ботом.")
        staff = uid in self.staff_ids
        if callback is not None:
            data = callback.get("data")
            if not isinstance(data, str) or len(data.encode("utf-8")) > 64:
                return self._answer(callback, "Кнопка недоступна.")
            actions = self._answer(callback)
            return actions + self._callback(uid, data, staff)
        text = message.get("text")
        if isinstance(text, str) and text.startswith("/"):
            return self._command(uid, text, staff)
        session = self._session(uid)
        if session is None:
            return [self._message(uid, "Чтобы оставить заявку, нажмите «Новая заявка» или отправьте /new.", self._home(staff))]
        step, draft = session
        if step in ("device", "description"):
            minimum, maximum = (3, 120) if step == "device" else (10, 2000)
            if not isinstance(text, str) or not minimum <= len(text.strip()) <= maximum:
                label = "Название устройства" if step == "device" else "Описание неисправности"
                return [self._message(uid, f"{label}: отправьте текст от {minimum} до {maximum} символов.")]
            draft[step] = text.strip()
            if step == "device":
                self._save(uid, "description", draft)
                return [self._message(uid, "2 / 5 · Что случилось?\nОпишите неисправность: от 10 до 2000 символов. Не пишите пароли и другие секреты.")]
            self._save(uid, "photo", draft)
            return [self._message(uid, "3 / 5 · Фото неисправности\nПришлите одно фото как фотографию (до 10 МБ). Уберите с кадра личные данные. Без фото: /skip.")]
        if step == "photo":
            photo = message.get("photo")
            valid = isinstance(photo, list) and 0 < len(photo) <= 20 and all(isinstance(p, dict) for p in photo)
            if valid:
                selected = photo[-1]
                file_id, file_size = selected.get("file_id"), selected.get("file_size")
                valid = isinstance(file_id, str) and 1 <= len(file_id) <= 512 and (file_size is None or (isinstance(file_size, int) and not isinstance(file_size, bool) and 0 <= file_size <= MAX_PHOTO_BYTES))
            if not valid:
                return [self._message(uid, "Нужно фото до 10 МБ, отправленное как фотография. Документы и видео не принимаются. Можно продолжить без фото: /skip.")]
            draft["photo_id"] = file_id
            return self._ask_consent(uid, draft)
        if step == "contact":
            contact = message.get("contact")
            if not isinstance(contact, dict) or contact.get("user_id") != uid or not _positive(contact.get("user_id")):
                return [self._message(uid, "Принимается только ваш контакт через кнопку «Поделиться своим контактом». Чужие карточки и номер текстом не сохраняются. Без телефона: /skip.")]
            raw = contact.get("phone_number")
            if not isinstance(raw, str) or not re.fullmatch(r"\+?[0-9 ()-]{5,32}", raw):
                return [self._message(uid, "Не удалось прочитать номер в карточке. Попробуйте снова или отправьте /skip.")]
            digits = re.sub(r"[^0-9]", "", raw)
            if not 5 <= len(digits) <= 20:
                return [self._message(uid, "Не удалось прочитать номер в карточке. Попробуйте снова или отправьте /skip.")]
            draft["contact_phone"] = ("+" if raw.startswith("+") else "") + digits
            draft["contact_consent_at"] = _now()
            return self._confirm(uid, draft)
        if step == "consent":
            return [self._message(uid, "4 / 5 · Выберите, согласны ли вы передать свой телефон мастерской.", self._consent_markup(draft))]
        if step == "confirm":
            return [self._message(uid, "5 / 5 · Проверьте заявку и нажмите «Создать заявку». До подтверждения заявка не создаётся.", self._confirm_markup(draft))]
        # Unknown persisted states must not silently create a ticket.
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return [self._message(uid, "Черновик не удалось восстановить. Начните заново: /new.")]

    def _answer(self, callback, text=None):
        if not callback or not isinstance(callback.get("id"), str) or not 1 <= len(callback["id"]) <= 256:
            return []
        params = {"callback_query_id": callback["id"]}
        if text:
            params["text"] = text
        return [{"method": "answerCallbackQuery", "params": params}]

    def _session(self, uid):
        row = self.db.execute("SELECT step,draft FROM sessions WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["draft"])
        except (json.JSONDecodeError, TypeError):
            value = None
        if not isinstance(value, dict):
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            return None
        return row["step"], value

    def _save(self, uid, step, draft):
        self.db.execute("INSERT INTO sessions(user_id,step,draft,updated_at) VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET step=excluded.step,draft=excluded.draft,updated_at=excluded.updated_at", (uid, step, json.dumps(draft, ensure_ascii=False), _now()))

    def _new(self, uid):
        if self._session(uid):
            return [self._message(uid, "У вас уже есть незавершённый черновик. Продолжите его или отмените: /cancel.")]
        self._save(uid, "device", {"nonce": secrets.token_hex(4), "photo_id": None, "contact_phone": None, "contact_consent_at": None})
        return [self._message(uid, "1 / 5 · Новая заявка\nЧто нужно отремонтировать? Название и модель: от 3 до 120 символов.\nОтменить черновик: /cancel.", {"remove_keyboard": True})]

    def _consent_markup(self, draft):
        nonce = draft["nonce"]
        return self._inline([[("Согласен, передать мой телефон", f"consent:{nonce}:yes")], [("Продолжить без телефона", f"consent:{nonce}:no")], [("Отменить черновик", f"cancel:{nonce}")]])

    def _ask_consent(self, uid, draft):
        self._save(uid, "consent", draft)
        return [self._message(uid, "4 / 5 · Контакт по вашему желанию\nРазрешаете передать ваш телефон сотрудникам мастерской для связи по этой заявке? Можно отказаться и следить за статусом здесь.\nПосле передачи контакта телефон временно хранится в черновике. /cancel удаляет черновик. Подтверждение создаёт заявку с телефоном. Позже удалить его: /withdrawcontact НОМЕР.", self._consent_markup(draft))]

    def _confirm_markup(self, draft):
        return self._inline([[("Создать заявку", f"submit:{draft['nonce']}")], [("Отменить черновик", f"cancel:{draft['nonce']}")]])

    @staticmethod
    def _masked(phone):
        return "не передан" if not phone else "••• " + phone[-4:]

    def _confirm(self, uid, draft):
        self._save(uid, "confirm", draft)
        text = f"5 / 5 · Проверьте заявку\n\nУстройство: {draft['device']}\nНеисправность: {draft['description']}\nФото: {'приложено' if draft.get('photo_id') else 'не приложено'}\nТелефон: {self._masked(draft.get('contact_phone'))}\n\nНажмите «Создать заявку». До подтверждения это только черновик."
        # Remove request_contact keyboard, then send inline confirmation.
        return [self._message(uid, "Данные получены. Осталось подтвердить заявку.", {"remove_keyboard": True}), self._message(uid, text, self._confirm_markup(draft))]

    def _submit(self, uid, nonce=None):
        session = self._session(uid)
        if not session or session[0] != "confirm" or (nonce is not None and session[1].get("nonce") != nonce):
            return [self._message(uid, "Эта кнопка устарела. Используйте кнопки текущего черновика или /new.")]
        draft = session[1]
        timestamp = _now()
        cursor = self.db.execute("INSERT INTO tickets(owner_id,device,description,photo_id,contact_phone,contact_consent_at,status,created_at,updated_at) VALUES (?,?,?,?,?,?,'new',?,?)", (uid, draft["device"], draft["description"], draft.get("photo_id"), draft.get("contact_phone"), draft.get("contact_consent_at"), timestamp, timestamp))
        ticket_id = cursor.lastrowid
        self.db.execute("INSERT INTO history(ticket_id,actor_id,kind,to_status,created_at) VALUES (?,?,'created','new',?)", (ticket_id, uid, timestamp))
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return [self._message(uid, f"Заявка №{ticket_id} создана. Статус: Новая.\nСотрудник увидит её в очереди мастерской. Посмотреть детали: /ticket {ticket_id}.", self._inline([[("Открыть заявку", f"ticket:{ticket_id}"), ("Мои заявки", "mine")]]))]

    def _cancel(self, uid, nonce=None):
        session = self._session(uid)
        if not session:
            return [self._message(uid, "Активного черновика нет. Новая заявка: /new.", {"remove_keyboard": True})]
        if nonce is not None and session[1].get("nonce") != nonce:
            return [self._message(uid, "Эта кнопка устарела. Текущий черновик не изменён.")]
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return [self._message(uid, "Черновик отменён. Его данные удалены. Заявка не создана.", {"remove_keyboard": True})]

    def _command(self, uid, text, staff):
        parts = text.strip().split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        if command in ("/start", "/help"):
            role = "сотрудник" if staff else "клиент"
            commands = "\n/staff — очередь мастерской\n/status НОМЕР СТАТУС — смена статуса" if staff else ""
            return [self._message(uid, f"Мастерская · приём заявок\nРоль: {role}.\n\n/new — новая заявка\n/mine — ваши заявки\n/ticket НОМЕР — детали\n/history НОМЕР — история\n/cancel — отменить черновик\n/withdrawcontact НОМЕР — удалить свой телефон из заявки\n/privacy — о хранении данных{commands}\n\nТочную стоимость и сроки бот не обещает: их согласует сотрудник.", self._home(staff))]
        if command == "/privacy":
            return [self._message(uid, "Сохраняются Telegram ID, устройство, описание, ссылка file_id на фото, статус и история. Телефон — только по вашему согласию и только собственный контакт. Его можно отозвать: /withdrawcontact НОМЕР. Черновик удаляется командой /cancel. Фото не скачивается: Telegram хранит его по своим правилам. Не отправляйте пароли или документы. Для рабочей мастерской владелец должен определить срок хранения и опубликовать свою политику обработки данных.")]
        if command == "/new" and not args:
            return self._new(uid)
        if command == "/cancel" and not args:
            return self._cancel(uid)
        if command == "/submit" and not args:
            return self._submit(uid)
        if command == "/skip" and not args:
            session = self._session(uid)
            if session and session[0] == "photo":
                return self._ask_consent(uid, session[1])
            if session and session[0] == "contact":
                draft = session[1]
                draft["contact_phone"] = draft["contact_consent_at"] = None
                return self._confirm(uid, draft)
            return [self._message(uid, "Сейчас нельзя пропустить этот шаг. Используйте кнопки текущего шага или /cancel.")]
        if command == "/mine" and not args:
            return self._list(uid, False)
        if command == "/staff" and not args:
            return self._list(uid, True) if staff else [self._message(uid, "Эта команда доступна только сотрудникам.")]
        if command in ("/ticket", "/history", "/withdrawcontact") and len(args) == 1 and re.fullmatch(r"[1-9][0-9]{0,17}", args[0]):
            tid = int(args[0])
            if command == "/withdrawcontact":
                return self._withdraw(uid, tid)
            return self._ticket(uid, tid, staff, command == "/history")
        if command == "/status":
            if not staff:
                return [self._message(uid, "Эта команда доступна только сотрудникам.")]
            if len(args) == 2 and re.fullmatch(r"[1-9][0-9]{0,17}", args[0]) and args[1] in STATUSES:
                return self._status(uid, int(args[0]), args[1])
            return [self._message(uid, "Формат: /status НОМЕР СТАТУС. Статусы: diagnostics, in_progress, ready, closed, cancelled. Переходы доступны в карточке заявки.")]
        return [self._message(uid, "Команда не распознана или неверен формат. /help — список команд. Текущий черновик сохранён.")]

    def _callback(self, uid, data, staff):
        if data == "new":
            return self._new(uid)
        if data == "mine":
            return self._list(uid, False)
        if data == "staff":
            return self._list(uid, True) if staff else [self._message(uid, "Эта команда доступна только сотрудникам.")]
        if re.fullmatch(r"(?:ticket|history):[1-9][0-9]{0,17}", data):
            kind, tid = data.split(":")
            return self._ticket(uid, int(tid), staff, kind == "history")
        match = re.fullmatch(r"status:([1-9][0-9]{0,17}):([a-z_]+):([a-z_]+)", data)
        if match:
            if not staff:
                return [self._message(uid, "Эта команда доступна только сотрудникам.")]
            return self._status(uid, int(match[1]), match[3], expected=match[2])
        match = re.fullmatch(r"(submit|cancel):([0-9a-f]{8})", data)
        if match:
            return self._submit(uid, match[2]) if match[1] == "submit" else self._cancel(uid, match[2])
        match = re.fullmatch(r"consent:([0-9a-f]{8}):(yes|no)", data)
        if match:
            session = self._session(uid)
            if not session or session[0] != "consent" or session[1].get("nonce") != match[1]:
                return [self._message(uid, "Эта кнопка устарела. Используйте кнопки текущего черновика.")]
            draft = session[1]
            if match[2] == "no":
                draft["contact_phone"] = draft["contact_consent_at"] = None
                return self._confirm(uid, draft)
            self._save(uid, "contact", draft)
            return [self._message(uid, "Вы согласились передать телефон. Нажмите кнопку ниже, чтобы отправить свой контакт. Можно передумать: /skip.", {"keyboard": [[{"text": "Поделиться своим контактом", "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True})]
        return [self._message(uid, "Эта кнопка устарела или недоступна. /help — доступные действия.")]

    def _list(self, uid, all_tickets):
        query = "SELECT id,device,status FROM tickets" + ("" if all_tickets else " WHERE owner_id=?") + " ORDER BY id DESC LIMIT 30"
        rows = self.db.execute(query, () if all_tickets else (uid,)).fetchall()
        title = "Очередь мастерской" if all_tickets else "Ваши заявки"
        if not rows:
            return [self._message(uid, f"{title}: пока пусто.", self._home(uid in self.staff_ids))]
        lines = [f"{title} · последние 30", ""]
        keyboard = []
        for row in rows:
            lines.append(f"№{row['id']} · {row['device'][:60]} · {STATUSES[row['status']]}")
            keyboard.append([(f"№{row['id']} · {STATUSES[row['status']]}", f"ticket:{row['id']}")])
        return [self._message(uid, "\n".join(lines), self._inline(keyboard))]

    def _authorized(self, uid, tid, staff):
        # Same query shape and same refusal for absent and somebody else's tickets.
        return self.db.execute("SELECT * FROM tickets WHERE id=? AND (owner_id=? OR ?)", (tid, uid, int(staff))).fetchone()

    def _ticket(self, uid, tid, staff, history_only=False):
        row = self._authorized(uid, tid, staff)
        if row is None:
            return [self._message(uid, DENIED)]
        if history_only:
            events = self.db.execute("SELECT * FROM history WHERE ticket_id=? ORDER BY id", (tid,)).fetchall()
            lines = [f"История заявки №{tid}", "Время: UTC"]
            for event in events:
                if event["kind"] == "created":
                    label = "Заявка создана · Новая"
                elif event["kind"] == "contact_withdrawn":
                    label = "Владелец отозвал телефон"
                else:
                    label = f"{STATUSES[event['from_status']]} → {STATUSES[event['to_status']]} · сотрудник"
                lines.append(f"{event['created_at']} · {label}")
            return [self._message(uid, "\n".join(lines), self._inline([[("К заявке", f"ticket:{tid}")]]))]
        phone = row["contact_phone"] if staff and row["contact_phone"] else self._masked(row["contact_phone"])
        text = f"Заявка №{tid}\n{STATUSES[row['status']]}\n\nУстройство: {row['device']}\nНеисправность: {row['description']}\nФото: {'приложено' if row['photo_id'] else 'нет'}\nТелефон: {phone}\nСоздана: {row['created_at']} (UTC)"
        buttons = [[("История заявки", f"history:{tid}")]]
        if staff:
            buttons.extend([[(STATUSES[target], f"status:{tid}:{row['status']}:{target}")] for target in TRANSITIONS[row["status"]]])
        actions = [self._message(uid, text, self._inline(buttons))]
        if row["photo_id"]:
            actions.append({"method": "sendPhoto", "params": {"chat_id": uid, "photo": row["photo_id"], "caption": f"Фото к заявке №{tid}"}})
        return actions

    def _status(self, uid, tid, target, expected=None):
        # Redundant check guards direct callers/refactors as well as routes.
        if uid not in self.staff_ids:
            return [self._message(uid, "Эта команда доступна только сотрудникам.")]
        row = self._authorized(uid, tid, True)
        if row is None:
            return [self._message(uid, DENIED)]
        source = row["status"]
        if expected is not None and expected != source:
            return [self._message(uid, "Карточка устарела. Статус не изменён. Откройте заявку заново.", self._inline([[("Обновить заявку", f"ticket:{tid}")]]))]
        if target not in TRANSITIONS[source]:
            return [self._message(uid, f"Переход из «{STATUSES[source]}» в этот статус запрещён. Используйте доступные кнопки в карточке заявки.")]
        timestamp = _now()
        self.db.execute("UPDATE tickets SET status=?,updated_at=? WHERE id=? AND status=?", (target, timestamp, tid, source))
        self.db.execute("INSERT INTO history(ticket_id,actor_id,kind,from_status,to_status,created_at) VALUES (?,?,'status_changed',?,?,?)", (tid, uid, source, target, timestamp))
        return [self._message(uid, f"Заявка №{tid}: {STATUSES[source]} → {STATUSES[target]}.")] + self._ticket(uid, tid, True)

    def _withdraw(self, uid, tid):
        row = self._authorized(uid, tid, False)
        if row is None:
            return [self._message(uid, DENIED)]
        phone = row["contact_phone"]
        if not phone:
            return [self._message(uid, f"В заявке №{tid} телефон не сохранён.")]
        self.db.execute("UPDATE tickets SET contact_phone=NULL,contact_consent_at=NULL,updated_at=? WHERE id=?", (_now(), tid))
        self.db.execute("INSERT INTO history(ticket_id,actor_id,kind,created_at) VALUES (?,?,'contact_withdrawn',?)", (tid, uid, _now()))
        # Redact only display text, including sent rows. Never change recipients,
        # file IDs or callback data even when they contain the same digits.
        # Already delivered Telegram messages / external backups cannot be recalled.
        for message in self.db.execute("SELECT id,params FROM outbox WHERE instr(params,?)>0", (phone,)).fetchall():
            params = json.loads(message["params"])
            changed = False
            for field in ("text", "caption"):
                value = params.get(field)
                if isinstance(value, str) and phone in value:
                    params[field] = value.replace(phone, "[контакт отозван]")
                    changed = True
            if changed:
                self.db.execute("UPDATE outbox SET params=? WHERE id=?", (json.dumps(params, ensure_ascii=False), message["id"]))
        return [self._message(uid, f"Телефон из заявки №{tid} удалён из базы и локальной очереди. Уже отправленные сообщения и резервные копии автоматически не удаляются.")]
