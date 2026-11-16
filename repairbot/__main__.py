"""Explicit entry points: demo never imports or starts Telegram transport."""
import argparse
import logging
import sqlite3
import sys

from .config import ConfigError, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="Приёмная · демонстрационный бот мастерской")
    sub = parser.add_subparsers(dest="mode", required=True)
    demo = sub.add_parser("demo", help="локальный стенд без токена и сети Telegram")
    demo.add_argument("--db", default="data/demo.sqlite3", help="отдельная учебная база")
    demo.add_argument("--port", type=int, default=8783)
    sub.add_parser("check-config", help="проверка переменных окружения без сетевых запросов")
    telegram = sub.add_parser("telegram", help="явный запуск отдельного бота через Bot API")
    telegram.add_argument("--allow-network", action="store_true", help="подтверждение запуска настоящих ответов в Telegram")
    args = parser.parse_args(argv)
    if args.mode == "demo":
        if not 1 <= args.port <= 65535:
            parser.error("Порт должен быть от 1 до 65535.")
        from .demo import run_demo
        try:
            run_demo(args.db, args.port)
        except (OSError, sqlite3.Error):
            print("Не удалось открыть учебную базу или порт. Проверьте права и выберите свободный порт.", file=sys.stderr)
            return 1
        return 0
    if args.mode == "telegram" and not args.allow_network:
        parser.error("Для настоящего Telegram нужен явный флаг --allow-network. Для показа используйте demo.")
    try:
        config = load_config(require_token=True)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.mode == "check-config":
        print("Формат настроек корректен. Токен скрыт. Подлинность токена и ID в Telegram не проверялась.")
        return 0
    from .core import BotService
    from .telegram import TelegramClient, TelegramError, TelegramRunner
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    service = None
    try:
        client = TelegramClient(config.bot_token)
        identity = client.request("getMe", {})
        if not isinstance(identity, dict) or identity.get("is_bot") is not True:
            raise TelegramError("invalid_response")
        info = client.request("getWebhookInfo", {})
        if not isinstance(info, dict):
            raise TelegramError("invalid_response")
        if info.get("url"):
            print("У бота уже подключён webhook. Запуск остановлен; настройки бота не изменены.", file=sys.stderr)
            return 2
        service = BotService(config.db_path, set(config.staff_ids))
        # Never deliver offline demo file IDs or queued demo replies to Telegram.
        if service.db.execute("SELECT 1 FROM sqlite_master WHERE name='demo_messages'").fetchone():
            print("Это учебная база. Укажите отдельный DB_PATH для Telegram.", file=sys.stderr)
            return 2
        print("Запущен Telegram-режим. Бот отвечает входящим пользователям. Остановка: Ctrl+C.", flush=True)
        TelegramRunner(client, service, config.poll_timeout).run_forever()
    except KeyboardInterrupt:
        return 0
    except TelegramError as error:
        print("Telegram-режим остановлен: " + error.code + ". Секреты скрыты.", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("Telegram-режим остановлен: ошибка локального хранилища или доступа.", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
