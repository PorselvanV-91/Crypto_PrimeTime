"""Telegram credentials from environment/.env or private root config.py."""
import ast
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def load_env(path='.env'):
    """Load simple KEY=value files without overwriting service environment values."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key not in ('TELEGRAM_TOKEN', 'TELEGRAM_CHAT_ID'):
            raise ValueError('Invalid .env entry (expected Telegram settings only)')
        os.environ.setdefault(key, value.strip().strip('\"\''))


class Telegram:
    def __init__(self, config_path='config.py'):
        settings = {}
        path = Path(config_path)
        if path.is_file():
            try:
                # Read literal assignments only; don't execute a private config file.
                for node in ast.parse(path.read_text(encoding='utf-8-sig')).body:
                    if not isinstance(node, ast.Assign):
                        continue
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in ('TELEGRAM_TOKEN', 'TELEGRAM_CHAT_ID'):
                            value = ast.literal_eval(node.value)
                            if isinstance(value, bool) or not isinstance(value, (str, int)):
                                raise ValueError('Invalid credential type')
                            settings[target.id] = str(value).strip()
            except (SyntaxError, ValueError, TypeError):
                raise ValueError('Invalid Telegram config.py; use literal TELEGRAM_TOKEN and TELEGRAM_CHAT_ID assignments') from None
        self.token = os.environ.get('TELEGRAM_TOKEN') or settings.get('TELEGRAM_TOKEN', '')
        self.chat_id = os.environ.get('TELEGRAM_CHAT_ID') or settings.get('TELEGRAM_CHAT_ID', '')
        if bool(self.token) != bool(self.chat_id):
            raise ValueError('Set both TELEGRAM_TOKEN and TELEGRAM_CHAT_ID')

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def send_telegram_alert(self, message):
        if not self.enabled:
            raise RuntimeError('Telegram credentials are not configured')
        # Same sendMessage endpoint and payload as the old implementation.
        request = Request(f'https://api.telegram.org/bot{self.token}/sendMessage',
                          data=urlencode({'chat_id': self.chat_id, 'text': message}).encode(),
                          method='POST')
        try:
            with urlopen(request, timeout=10) as response:
                result = json.load(response)
            if not result.get('ok'):
                raise RuntimeError('Telegram rejected message')
        except Exception:
            # Never print an exception URL: it may contain the bot token.
            raise RuntimeError('Telegram delivery failed; queued for retry') from None
