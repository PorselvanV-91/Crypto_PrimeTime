import os
import unittest
from unittest.mock import patch
from prime_time.telegram import Telegram


class ConfigTests(unittest.TestCase):
    def make(self, content, env=None):
        with patch.dict(os.environ, env or {}, clear=True), \
                patch('prime_time.telegram.Path.is_file', return_value=True), \
                patch('prime_time.telegram.Path.read_text', return_value=content):
            return Telegram()

    def test_literal_private_config_and_numeric_chat(self):
        client = self.make('TELEGRAM_TOKEN = "test-token"\nTELEGRAM_CHAT_ID = -12345')
        self.assertTrue(client.enabled)
        self.assertEqual(client.chat_id, '-12345')

    def test_environment_overrides_config(self):
        client = self.make('TELEGRAM_TOKEN = "file-token"\nTELEGRAM_CHAT_ID = "file-chat"',
                           {'TELEGRAM_TOKEN': 'env-token', 'TELEGRAM_CHAT_ID': 'env-chat'})
        self.assertEqual((client.token, client.chat_id), ('env-token', 'env-chat'))

    def test_no_credentials_keeps_alerts_disabled(self):
        with patch.dict(os.environ, {}, clear=True), patch('prime_time.telegram.Path.is_file', return_value=False):
            self.assertFalse(Telegram().enabled)

    def test_incomplete_configuration_fails(self):
        with self.assertRaises(ValueError):
            self.make('TELEGRAM_TOKEN = "test-token"')

    def test_expressions_not_executed(self):
        with self.assertRaises(ValueError):
            self.make('TELEGRAM_TOKEN = str("secret")\nTELEGRAM_CHAT_ID = "chat"')

    def test_syntax_error_does_not_expose_config_source(self):
        with self.assertRaises(ValueError) as error:
            self.make('TELEGRAM_TOKEN = "private-credential')
        self.assertNotIn('private-credential', str(error.exception))
