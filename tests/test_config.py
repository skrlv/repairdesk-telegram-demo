import unittest

from repairbot.config import Config, ConfigError, load_config, validate_token


TOKEN = "123456:" + "x" * 35  # Synthetic syntax fixture, never a real token.


class ConfigTest(unittest.TestCase):
    def test_demo_does_not_require_credentials(self):
        config = load_config(environ={})
        self.assertEqual(config.bot_token, "")
        self.assertEqual(config.staff_ids, frozenset())
        self.assertEqual(config.db_path, "data/repairbot.sqlite3")
        self.assertEqual(config.poll_timeout, 25)

    def test_telegram_requires_token_and_explicit_staff(self):
        for env in ({}, {"BOT_TOKEN": TOKEN}, {"STAFF_IDS": "99"}):
            with self.subTest(keys=list(env)):
                with self.assertRaises(ConfigError) as caught:
                    load_config(require_token=True, environ=env)
                self.assertNotIn(TOKEN, str(caught.exception))
        config = Config.from_env(require_token=True, environ={
            "BOT_TOKEN": TOKEN, "STAFF_IDS": "99, 100,99", "DB_PATH": "private/bot.db", "POLL_TIMEOUT": "50",
        })
        self.assertEqual(config.staff_ids, frozenset({99, 100}))
        self.assertEqual(config.poll_timeout, 50)
        self.assertEqual(config.db_path, "private/bot.db")
        self.assertNotIn(TOKEN, repr(config))

    def test_bad_token_values_do_not_appear_in_errors(self):
        for token in (None, "", "secret-value", TOKEN + "\n", "0:" + "x" * 40, "12345:short"):
            with self.subTest(token_type=type(token).__name__):
                with self.assertRaises(ConfigError) as caught:
                    validate_token(token)
                if token:
                    self.assertNotIn(str(token), str(caught.exception))

    def test_staff_ids_fail_closed(self):
        for raw in ("0", "-1", "abc", "99,", "99,,100", "1.0", "99999999999999999999", "９９"):
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError):
                    load_config(environ={"STAFF_IDS": raw})

    def test_paths_and_poll_timeout_are_validated(self):
        for path in ("", "  ", "bad\x00path"):
            with self.subTest(path=path):
                with self.assertRaises(ConfigError):
                    load_config(environ={"DB_PATH": path})
        for timeout in ("0", "51", "-1", "1.5", "True", "", "100"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ConfigError):
                    load_config(environ={"POLL_TIMEOUT": timeout})


if __name__ == "__main__":
    unittest.main()
