"""
Telegram commands: the bot answers, as well as posts.

    /profit   Profit today: +35.68 AUD
              This month:   +1,040.79 AUD

The same realised figures as the footer's PROFIT TODAY / THIS MONTH - one
calculation, so the phone and the screen can never disagree.

Only authorised chats get an answer. The bot's username is discoverable, and
account P&L is not something to hand to whoever messages it. A chat is
authorised if it is an enabled PRIVATE destination in configs/alerts.json (a
numeric chat id - channels and @handles do not count), or listed in
TELEGRAM_COMMAND_CHATS (comma-separated ids) in .env. Anyone else is ignored
and the attempt is logged.

Transport is getUpdates long polling on a daemon thread: no webhook, no open
port, nothing to expose. The last update id is saved, so a restart never
re-answers old commands; a command older than two minutes is dropped rather
than answered late with figures that have since moved.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = 'https://api.telegram.org/bot%s/%s'
STALE_S = 120            # a command this old is not answered


def money(v: float, ccy: str) -> str:
    """+1,040.79 AUD - sign always shown, thousands separated."""
    return f'{v:+,.2f} {ccy}'.strip()


def profit_text(today: float, month: float, ccy: str) -> str:
    # Aligned in a <pre> block, so the two figures line up on any phone.
    return (f'Profit today: {money(today, ccy)}\n'
            f'This month:   {money(month, ccy)}')


class TelegramCommands:
    def __init__(self, token_fn, allowed_fn, profit_fn, state_path: Path, log=print):
        self.token_fn = token_fn            # () -> bot token or ''
        self.allowed_fn = allowed_fn        # () -> set of chat ids (str)
        self.profit_fn = profit_fn          # () -> (today, month, ccy) or None
        self.state_path = Path(state_path)
        self.log = log
        self.offset = self._load_offset()
        self._thread = None
        self._stop = threading.Event()

    # ------------------------------------------------------------- plumbing
    def _load_offset(self) -> int:
        try:
            return int(json.loads(self.state_path.read_text(encoding='utf-8')).get('offset', 0))
        except (OSError, ValueError):
            return 0

    def _save_offset(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({'offset': self.offset}), encoding='utf-8')

    def _call(self, token: str, method: str, params: dict, timeout: float):
        data = urllib.parse.urlencode(params).encode('utf-8')
        req = urllib.request.Request(API % (token, method), data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))

    def reply(self, token: str, chat_id, text: str) -> None:
        self._call(token, 'sendMessage',
                   {'chat_id': chat_id, 'text': f'<pre>{text}</pre>', 'parse_mode': 'HTML'},
                   timeout=10)

    # --------------------------------------------------------------- logic
    def handle(self, update: dict, token: str) -> None:
        msg = update.get('message') or {}
        text = str(msg.get('text') or '').strip()
        chat = msg.get('chat') or {}
        chat_id = str(chat.get('id', ''))
        if not text.startswith('/') or not chat_id:
            return
        # "/profit" or "/profit@DiaNurFxBot" in a group
        command = text.split()[0].split('@')[0].lower()
        if command not in ('/profit', '/start', '/help'):
            return
        if time.time() - float(msg.get('date') or 0) > STALE_S:
            return                           # old news; figures have moved
        if chat_id not in self.allowed_fn():
            self.log(f'! telegram: {command} from unauthorised chat {chat_id} ignored')
            return
        if command == '/profit':
            got = self.profit_fn()
            text_out = (profit_text(*got) if got else
                        'Profit is unavailable - MetaTrader is not connected.')
        else:
            text_out = '/profit - realised profit today and this month'
        self.reply(token, chat_id, text_out)

    def poll_once(self, timeout: int = 25) -> None:
        token = self.token_fn()
        if not token:
            time.sleep(10)
            return
        try:
            res = self._call(token, 'getUpdates',
                             {'offset': self.offset, 'timeout': timeout,
                              'allowed_updates': json.dumps(['message'])},
                             timeout=timeout + 10)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                # A webhook is set, or another program is polling this bot.
                # Not ours to undo - say so and back off.
                self.log('! telegram: getUpdates conflict (409) - another program '
                         'or a webhook is using this bot; /profit is off until it stops')
                time.sleep(60)
            else:
                time.sleep(10)
            return
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            time.sleep(10)
            return
        for u in res.get('result') or []:
            self.offset = max(self.offset, int(u.get('update_id', 0)) + 1)
            try:
                self.handle(u, token)
            except Exception as exc:                          # noqa: BLE001
                self.log(f'! telegram command failed: {type(exc).__name__}: {exc}')
        if res.get('result'):
            self._save_offset()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def run():
            while not self._stop.is_set():
                self.poll_once()

        self._thread = threading.Thread(target=run, name='telegram-commands', daemon=True)
        self._thread.start()


def private_destinations(cfg: dict) -> set:
    """Enabled destinations that are private chats: numeric ids only."""
    out = set()
    for d in (cfg or {}).get('destinations') or []:
        t = str(d.get('target') or '').strip()
        # Positive ids are private chats; groups and channels are negative.
        if d.get('enabled') and t.isdigit():
            out.add(t)
    extra = os.environ.get('TELEGRAM_COMMAND_CHATS') or ''
    out.update(x.strip() for x in extra.split(',') if x.strip())
    return out


__all__ = ['TelegramCommands', 'profit_text', 'private_destinations']
