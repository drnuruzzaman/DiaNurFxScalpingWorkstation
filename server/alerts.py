"""
server/alerts.py - fire a Telegram message when a signal qualifies.

Config lives in configs/alerts.json and follows the shape already in use in the
sibling project: a `watch` list of {symbol, tf, enabled} cells, a `destinations`
list of channels, and a `news` block. The Settings UI rewrites the whole file,
so hand-added comments do not survive a save - use the per-cell `note` field,
which does.

THE BOT TOKEN IS NEVER STORED HERE. It comes from TELEGRAM_BOT_TOKEN in the
environment. A chat id is an address, not a secret, so destinations keep theirs;
a token is a credential that can post as you, and a credential does not belong
in a file the UI rewrites.

Three things keep this from becoming a spam machine:

  DEDUPE      a signal id fires ONCE. Ids are stable per playbook+side+bar, so
              the same setup re-detected on the next tick is not a new alert.
  COOLDOWN    a per-cell floor between messages, regardless of signal id.
  QUIET HOURS an optional UTC window where nothing is sent.

Every attempt is appended to logs/alerts.jsonl - sent, suppressed, or failed,
with the reason. An alert you cannot explain later is worse than no alert.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG, LOG_DIR, ROOT, TF_SECONDS

CONFIGS_DIR = ROOT / 'configs'
CONFIGS_DIR.mkdir(exist_ok=True)
ALERTS_PATH = CONFIGS_DIR / 'alerts.json'
ALERT_LOG = LOG_DIR / 'alerts.jsonl'

TG_API = 'https://api.telegram.org/bot%s/sendMessage'
TG_BASE = 'https://api.telegram.org/bot%s/%s'

# What a destination can subscribe to. A destination with NO kind ticked
# receives nothing - that is the honest default for a thing that messages
# people, and the UI says so in words rather than leaving it ambiguous.
KINDS = ['signals', 'news', 'scalper']

_LOCK = threading.Lock()
_SENT: dict = {}        # signal_id -> ms sent
_LAST_CELL: dict = {}   # (symbol, tf) -> ms of last message

_NOTE = (
    'Written by the Settings modal in the DiaNurFx workstation and read by '
    'server/alerts.py on every signal. Safe to hand-edit; the UI rewrites the '
    'whole file, so a comment added here will not survive a save - put '
    'explanations in the per-cell `note` field, which does. The Telegram bot '
    'token is NOT in this file and never will be: it comes from '
    'TELEGRAM_BOT_TOKEN in the environment.'
)


def default_config() -> dict:
    """
    A config that alerts on nothing until you choose.

    Defaulting to "everything on" for a thing that messages people is the wrong
    failure mode - the first run would fire on whatever the scanner happened to
    find.
    """
    return {
        '_note': _NOTE,
        'enabled': False,
        'min_confidence': CONFIG.gates.min_confidence,
        'statuses': ['qualified'],
        'cooldown_minutes': 15,
        'quiet_hours_utc': {'enabled': False, 'from': 22, 'to': 6},
        'include_challenge': True,
        'watch': [],
        'destinations': [],
        'news': {'enabled': False, 'lead_minutes': 10, 'impact': 'high'},
        # Chats outside a private DM that may run bot commands.
        #
        # Empty by default and opt-in per chat, because a command reply
        # carries account figures and a group is other people. Kept in config
        # rather than code so it is visible next to the destinations rather
        # than being an invisible property of the build.
        'command_chats': [],
    }


def load() -> dict:
    if not ALERTS_PATH.exists():
        cfg = default_config()
        save(cfg)
        return cfg
    try:
        with open(ALERTS_PATH, 'r', encoding='utf-8') as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f'! alerts.json unreadable ({exc}) - using defaults, not overwriting')
        return default_config()
    base = default_config()
    base.update(cfg)
    base['_note'] = _NOTE
    return base


def save(cfg: dict) -> dict:
    cfg = dict(cfg)
    cfg['_note'] = _NOTE
    # Never persist a token even if one is posted in by mistake.
    for dest in cfg.get('destinations', []):
        dest.pop('token', None)
        dest.pop('bot_token', None)
    tmp = ALERTS_PATH.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, ALERTS_PATH)
    return cfg


# --------------------------------------------------------------------------- #
# the watch matrix                                                            #
# --------------------------------------------------------------------------- #
def watch_key(symbol: str, tf: str) -> str:
    return f'{symbol}|{tf}'


def is_watched(cfg: dict, symbol: str, tf: str) -> bool:
    for cell in cfg.get('watch', []):
        if cell.get('symbol') == symbol and cell.get('tf') == tf:
            return bool(cell.get('enabled'))
    return False


def set_watch(cfg: dict, symbol: str, tf: str, enabled: bool,
              note: str = None) -> dict:
    for cell in cfg.get('watch', []):
        if cell.get('symbol') == symbol and cell.get('tf') == tf:
            cell['enabled'] = bool(enabled)
            if note is not None:
                cell['note'] = note
            return cfg
    cfg.setdefault('watch', []).append({
        'symbol': symbol, 'tf': tf, 'enabled': bool(enabled),
        'note': note or '',
    })
    return cfg


def matrix(cfg: dict, symbols: list, timeframes: list) -> dict:
    """The grid the Settings UI renders: symbol x timeframe -> bool."""
    grid = {}
    for sym in symbols:
        grid[sym] = {tf: is_watched(cfg, sym, tf) for tf in timeframes}
    return grid


# --------------------------------------------------------------------------- #
# telegram                                                                    #
# --------------------------------------------------------------------------- #
def bot_token(name: str = None) -> str:
    """
    The token for a named bot, or the primary when unnamed.

    Alternates live in TELEGRAM_BOT_TOKEN_<NAME>. This exists because posting
    rights are per-bot, not per-account: a bot that is an administrator of one
    channel is a stranger to the next, and the fix is choosing the right sender
    rather than re-permissioning everything onto one.
    """
    if name:
        key = 'TELEGRAM_BOT_TOKEN_' + str(name).strip().upper().replace('-', '_')
        tok = (os.environ.get(key) or '').strip()
        if tok:
            return tok
    return (os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip()


def available_bots() -> list:
    """Names the UI can offer. The primary is always first, as 'default'."""
    out = []
    if (os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip():
        out.append('default')
    prefix = 'TELEGRAM_BOT_TOKEN_'
    for key in sorted(os.environ):
        if key.startswith(prefix) and os.environ[key].strip():
            out.append(key[len(prefix):].lower())
    return out


def telegram_ready(name: str = None) -> bool:
    return bool(bot_token(name))


def describe_bot(name: str = None) -> dict:
    """getMe for the chosen sender, so the UI can name who is posting."""
    token = bot_token(name)
    if not token:
        return {'ok': False, 'error': 'no token configured'}
    try:
        with urllib.request.urlopen(TG_BASE % (token, 'getMe'), timeout=10) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        u = body.get('result') or {}
        return {'ok': bool(body.get('ok')), 'username': u.get('username'),
                'id': u.get('id'), 'name': u.get('first_name')}
    except Exception as exc:                                  # noqa: BLE001
        return {'ok': False, 'error': str(exc)}


def normalise_target(target: str) -> str:
    """
    Accept anything a human might paste and return what the API wants.

    A t.me/name link is a browser URL; the API wants @name. A numeric id is
    already correct. Doing this once, here, means a destination copied out of
    Telegram works without the user knowing the difference.
    """
    chat = (target or '').strip()
    if chat.startswith('http'):
        chat = chat.rstrip('/').split('/')[-1]
    if chat.startswith('t.me/'):
        chat = chat.split('/', 1)[1]
    if chat and not chat.startswith(('@', '-')) and not chat.lstrip('-').isdigit():
        chat = '@' + chat
    return chat


def target_problem(chat: str) -> str:
    """
    Catch a destination that can never work, before Telegram does.

    A @name ending in "bot" is another BOT, and Telegram refuses bot-to-bot
    messages with USER_BOT_TO_BOT_DISABLED - a code that tells you nothing
    about what you did wrong. The mistake is easy to make because the bot's
    @name is the most visible string you have when setting this up, so the
    error should name the confusion rather than echo the API.
    """
    if not chat:
        return 'no target'
    name = chat.lstrip('@').lower()
    if chat.startswith('@') and name.endswith('bot'):
        return ('that is a BOT, not a chat. A bot is the sender, not a '
                'destination, and Telegram blocks bot-to-bot messages. Use a '
                'numeric chat id, or the @name of a channel or group the bot '
                'has been added to.')
    return ''


def resolve_chat(target: str, timeout: float = 10.0, bot: str = None) -> dict:
    """
    Ask Telegram what this chat actually is, without sending anything.

    getChat is read-only. It is how the UI can say "channel, sends to
    @rayo_scalper" or "chat not found" BEFORE a signal fires, instead of the
    first failure being a message that silently went nowhere.
    """
    token = bot_token(bot)
    if not token:
        return {'ok': False, 'error': f'no token for bot {bot or "default"}'}
    chat = normalise_target(target)
    problem = target_problem(chat)
    if problem:
        return {'ok': False, 'error': problem, 'resolved': chat}
    url = TG_BASE % (token, 'getChat') + '?' + urllib.parse.urlencode({'chat_id': chat})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        r = body.get('result') or {}
        kind = r.get('type')
        label = {'private': 'private chat', 'group': 'group',
                 'supergroup': 'supergroup', 'channel': 'channel'}.get(kind, kind)
        detail = label
        if r.get('username'):
            detail = f"{label} · sends to @{r['username']}"
        # "Public" means anyone can read what is posted there. A PRIVATE chat
        # with a @handle is still one person's DM - flagging it as public put a
        # broadcast warning on the user's own inbox and would have taught them
        # to ignore the warning that matters.
        public = kind in ('channel', 'supergroup') and bool(r.get('username'))
        return {'ok': True, 'type': kind, 'title': r.get('title') or r.get('first_name'),
                'username': r.get('username'), 'detail': detail,
                'public': public, 'resolved': chat}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode('utf-8')).get('description')
        except Exception:                                      # noqa: BLE001
            detail = f'HTTP {exc.code}'
        return {'ok': False, 'error': detail, 'resolved': chat}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return {'ok': False, 'error': str(exc), 'resolved': chat}


def can_post(target: str, bot: str = None, timeout: float = 10.0) -> dict:
    """
    Will a send actually succeed? getChat cannot answer this.

    Any bot can getChat a PUBLIC channel - reading is open to everyone. Posting
    needs membership plus can_post_messages, and the only way to know before
    the first real alert is getChatMember. Measured here: @CarbonPlusBot could
    read @rayo_scalper perfectly well and could not post to it, which is
    exactly the failure this catches.
    """
    token = bot_token(bot)
    if not token:
        return {'ok': False, 'error': 'no token for bot %s' % (bot or 'default')}
    me = describe_bot(bot)
    if not me.get('ok'):
        return {'ok': False, 'error': me.get('error', 'cannot identify bot')}
    chat = normalise_target(target)
    url = TG_BASE % (token, 'getChatMember') + '?' + urllib.parse.urlencode(
        {'chat_id': chat, 'user_id': me['id']})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        r = body.get('result') or {}
        state = r.get('status')
        allowed = state in ('creator', 'administrator', 'member')
        if state == 'administrator':
            allowed = bool(r.get('can_post_messages', True))
        return {'ok': allowed, 'status': state,
                'can_post_messages': r.get('can_post_messages'),
                'bot': me.get('username')}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode('utf-8')).get('description')
        except Exception:                                      # noqa: BLE001
            detail = 'HTTP %s' % exc.code
        # A private chat has no member list; that is not a failure to post.
        if 'member list is inaccessible' in (detail or '').lower():
            detail = ('the bot is not in this chat - add @%s to it (as an '
                      'administrator, for a channel)' % me.get('username'))
        return {'ok': False, 'error': detail, 'bot': me.get('username')}
    except Exception as exc:                                   # noqa: BLE001
        return {'ok': False, 'error': str(exc), 'bot': me.get('username')}


def send_telegram(target: str, text: str, timeout: float = 10.0,
                  bot: str = None) -> dict:
    """POST one message as `bot`. Returns {'ok': bool, ...} and never raises."""
    token = bot_token(bot)
    if not token:
        return {'ok': False, 'error': f'no token for bot {bot or "default"}'}
    chat = normalise_target(target)
    problem = target_problem(chat)
    if problem:
        return {'ok': False, 'chat': chat, 'error': problem}

    payload = urllib.parse.urlencode({
        'chat_id': chat,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'true',
    }).encode('utf-8')
    try:
        req = urllib.request.Request(TG_API % token, data=payload)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        return {'ok': bool(body.get('ok')), 'chat': chat,
                'message_id': (body.get('result') or {}).get('message_id'),
                'error': None if body.get('ok') else body.get('description')}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode('utf-8')).get('description')
        except Exception:                                      # noqa: BLE001
            detail = f'HTTP {exc.code}'
        return {'ok': False, 'chat': chat, 'error': detail}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return {'ok': False, 'chat': chat, 'error': str(exc)}


# --------------------------------------------------------------------------- #
# receiving commands                                                          #
# --------------------------------------------------------------------------- #
# The bot has only ever spoken. To answer /profit it has to listen too, and
# listening is a different risk: this bot administrates a PUBLIC channel, so
# anyone who finds it can open a private chat and type. Account P&L must not
# be one message away from a stranger, hence authorised_chat below.


def get_updates(offset: int = 0, bot: str = None, timeout: int = 25) -> dict:
    """
    Long-poll Telegram for new messages.

    `timeout` is Telegram's own long-poll window - the request parks on the
    server until something arrives or it expires, so a 25s timeout is one
    request per 25 idle seconds rather than a busy loop.
    """
    token = bot_token(bot)
    if not token:
        return {'ok': False, 'error': 'no token'}
    params = urllib.parse.urlencode({
        'timeout': timeout,
        'offset': offset or 0,
        'allowed_updates': json.dumps(['message']),
    })
    url = f'{TG_BASE % (token, "getUpdates")}?{params}'
    try:
        # Read timeout must outlast the long poll or every call raises.
        with urllib.request.urlopen(url, timeout=timeout + 10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as exc:                                   # noqa: BLE001
        return {'ok': False, 'error': str(exc)}


def authorised_chat(cfg: dict, chat_id, username: str = None,
                    chat_type: str = 'private') -> bool:
    """
    May this chat be answered?

    Two conditions, both required.

    PRIVATE CHATS: must also be a configured destination. That is the set the
    owner has explicitly pointed the bot at, so there is no second allowlist
    to drift out of step. With nothing configured this returns False for
    everyone, deliberately - the failure mode of guessing wrong is publishing
    account figures to whoever asked.

    GROUPS AND CHANNELS: blocked unless named in `command_chats`. A reply
    carries account figures and a group is other people, so this is opt-in per
    chat rather than a blanket switch - enabling one group must not quietly
    enable every other destination the bot can post to.
    """
    if chat_id is None:
        return False

    if (chat_type or '') != 'private':
        allowed = {normalise_target(x).strip().lower()
                   for x in (cfg.get('command_chats') or []) if x}
        here = {str(chat_id).strip().lower()}
        if username:
            here.add('@' + str(username).strip().lstrip('@').lower())
        return bool(allowed & here)
    wanted = {str(chat_id).strip().lower()}
    if username:
        wanted.add('@' + str(username).strip().lstrip('@').lower())
    for dest in cfg.get('destinations') or []:
        if not dest.get('enabled'):
            continue
        target = normalise_target(dest.get('target', '') or '').strip().lower()
        if target and target in wanted:
            return True
    return False


def parse_command(text: str) -> str:
    """
    The bare command from a message, or ''.

    Handles the "/profit@MyBot" form Telegram produces in groups, where
    commands are addressed to a specific bot.
    """
    t = (text or '').strip()
    if not t.startswith('/'):
        return ''
    word = t.split()[0][1:]
    return word.split('@', 1)[0].lower()


# --------------------------------------------------------------------------- #
# message formatting                                                          #
# --------------------------------------------------------------------------- #
def _esc(text) -> str:
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))


def format_signal(sig: dict, snap: dict = None, challenge: dict = None) -> str:
    """
    The message. Written to be readable on a phone in three seconds.

    Includes the counter-evidence deliberately: a signal alert that only argues
    one side trains the reader to stop thinking, and this system's whole point
    is that it argues both.
    """
    side = sig['side'].upper()
    arrow = '\U0001F7E2' if sig['side'] == 'buy' else '\U0001F534'
    sizing = sig.get('sizing') or {}

    lines = [
        f"{arrow} <b>{_esc(side)} {_esc(sig['symbol'])}</b> · {_esc(sig['tf'])}",
        f"<i>{_esc(sig['label'])}</i> · confidence {sig['confidence']}",
        '',
        f"Entry  <code>{sig['entry']}</code>",
        f"Stop   <code>{sig['stop']}</code>",
        f"TP1    <code>{sig['tp1']}</code>  ({sig['rr1']}R)",
    ]
    plan = sig.get('exit_plan') or {}
    if plan.get('mode') == 'trail':
        lines += [
            f"Exit   at TP1 stop locks +{plan.get('lock_r')}R, "
            f"then trails {plan.get('trail_atr')} ATR",
            f"Aim    <code>{sig['tp2']}</code>  ({sig['rr2']}R, objective, no order)",
        ]
    else:
        lines.append(f"TP2    <code>{sig['tp2']}</code>  ({sig['rr2']}R)")
    if sizing.get('lots'):
        lines.append(
            f"Size   <code>{sizing['lots']}</code> lots · risk "
            f"{sizing.get('risk_cash')} {CONFIG.risk.account_currency} "
            f"({sizing.get('risk_pct')}%)")
    if sizing.get('net_rr2') is not None:
        lines.append(f"Net    {sizing.get('net_rr1')}R / {sizing.get('net_rr2')}R after costs")

    if snap:
        regime = (snap.get('regime') or {}).get('label')
        sess = (snap.get('session') or {}).get('primary')
        mtf = (snap.get('mtf') or {}).get('verdict')
        lines += ['', f"Context: {_esc(regime)} · {_esc(sess)} · MTF {_esc(mtf)}"]

    why = [e for e in sig.get('evidence', []) if e.get('weight', 0) > 0][:3]
    if why:
        lines.append('')
        lines.append('<b>Why</b>')
        for e in why:
            lines.append(f"• {_esc(e['text'])}")

    against = list(sig.get('against') or [])
    if challenge:
        against = [o['text'] for o in challenge.get('objections', [])
                   if o.get('severity') in ('severe', 'moderate')] or against
    if against:
        lines.append('')
        lines.append('<b>Against</b>')
        for a in against[:3]:
            lines.append(f"• {_esc(a)}")

    if sig.get('invalidation'):
        lines += ['', f"<i>Invalid if: {_esc(sig['invalidation'])}</i>"]

    lines += ['', '<i>DiaNurFx — analysis, not advice. You place the trade.</i>']
    return '\n'.join(lines)


# --------------------------------------------------------------------------- #
# the gate                                                                    #
# --------------------------------------------------------------------------- #
@dataclass
class AlertVerdict:
    send: bool
    reason: str


def should_alert(cfg: dict, sig: dict, now_ms: int = None) -> AlertVerdict:
    now_ms = now_ms or int(time.time() * 1000)

    if not cfg.get('enabled'):
        return AlertVerdict(False, 'alerting is switched off')
    if sig.get('status') not in (cfg.get('statuses') or ['qualified']):
        return AlertVerdict(False, f"status {sig.get('status')} is not alerted on")
    if int(sig.get('confidence', 0)) < int(cfg.get('min_confidence', 0)):
        return AlertVerdict(
            False, f"confidence {sig.get('confidence')} below "
                   f"{cfg.get('min_confidence')}")
    if not is_watched(cfg, sig['symbol'], sig['tf']):
        return AlertVerdict(False, f"{sig['symbol']} {sig['tf']} is not watched")

    quiet = cfg.get('quiet_hours_utc') or {}
    if quiet.get('enabled'):
        hour = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).hour
        lo, hi = int(quiet.get('from', 22)), int(quiet.get('to', 6))
        inside = (lo <= hour < hi) if lo < hi else (hour >= lo or hour < hi)
        if inside:
            return AlertVerdict(False, f'quiet hours ({lo:02d}-{hi:02d} UTC)')

    with _LOCK:
        if sig['id'] in _SENT:
            return AlertVerdict(False, 'already sent for this signal')
        cell = (sig['symbol'], sig['tf'])
        last = _LAST_CELL.get(cell, 0)
        cooldown_ms = float(cfg.get('cooldown_minutes', 15)) * 60_000
        if now_ms - last < cooldown_ms:
            remain = (cooldown_ms - (now_ms - last)) / 60_000
            return AlertVerdict(False, f'cooldown, {remain:.1f} min remaining')

    live = [d for d in cfg.get('destinations', []) if d.get('enabled')]
    if not live:
        return AlertVerdict(False, 'no destination is enabled')
    # A destination is only reachable if ITS bot has a token. Checking only the
    # primary would pass a config whose destinations all send as an alternate
    # that was never configured.
    if not any(telegram_ready(d.get('bot')) for d in live):
        return AlertVerdict(False, 'no configured bot token for any destination')
    return AlertVerdict(True, 'ok')


# Reasons that are routine and would flood the log if recorded: they fire for
# most signals on most scans and say nothing a human needs to read.
ROUTINE_SUPPRESSIONS = (
    'is not watched', 'is not alerted on', 'below', 'already sent',
    'alerting is switched off',
)


def is_notable(reason: str) -> bool:
    """
    Worth a log line?

    An operational block - no token, no destination, quiet hours, cooldown -
    means an alert you EXPECTED did not arrive, and that must be explainable
    after the fact. A routine filter means the signal was never a candidate,
    and logging those buries the ones that matter under thousands of lines.
    """
    low = (reason or '').lower()
    return not any(r in low for r in ROUTINE_SUPPRESSIONS)


def _log(entry: dict) -> None:
    try:
        with open(ALERT_LOG, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry) + '\n')
    except OSError:
        pass


def dispatch(cfg: dict, sig: dict, snap: dict = None,
             challenge: dict = None, force: bool = False) -> dict:
    """
    Decide and, if allowed, send. Always returns what happened and why.

    `force` is for the Settings "send a test" button and skips the gates but
    NOT the token check - there is nothing to test without a token.
    """
    now_ms = int(time.time() * 1000)
    verdict = AlertVerdict(True, 'forced test') if force else should_alert(cfg, sig, now_ms)
    if not verdict.send:
        entry = {'ts': now_ms, 'action': 'suppressed', 'reason': verdict.reason,
                 'signal_id': sig.get('id'), 'symbol': sig.get('symbol'),
                 'tf': sig.get('tf')}
        # Only notable suppressions reach the log; see is_notable().
        if is_notable(verdict.reason):
            _log(entry)
        return entry

    text = format_signal(sig, snap, challenge)
    results = []
    for dest in cfg.get('destinations', []):
        if not dest.get('enabled'):
            continue
        # No kind ticked means no kind subscribed. Treating an empty list as
        # "everything" is the wrong default for something that messages people:
        # a destination you added but have not configured should stay silent.
        if 'signals' not in (dest.get('kinds') or []):
            continue
        res = send_telegram(dest.get('target', ''), text,
                            bot=dest.get('bot'))
        results.append({'label': dest.get('label'), 'target': dest.get('target'),
                        'bot': dest.get('bot') or 'default', **res})

    if not results:
        # The gate passed but no destination subscribes to this kind. That is a
        # configuration state, not a delivery failure, and logging it as
        # "failed: ok" was actively confusing - it read like Telegram rejected
        # something when in fact nothing was ever addressed.
        entry = {'ts': now_ms, 'action': 'suppressed',
                 'reason': 'no destination has the "signals" kind ticked',
                 'signal_id': sig.get('id'), 'symbol': sig.get('symbol'),
                 'tf': sig.get('tf')}
        _log(entry)
        return entry

    ok = any(r.get('ok') for r in results)
    if ok and not force:
        with _LOCK:
            _SENT[sig['id']] = now_ms
            _LAST_CELL[(sig['symbol'], sig['tf'])] = now_ms
            # Keep the dedupe table from growing without bound across a long
            # session: ids older than a day can never be re-detected anyway.
            cutoff = now_ms - 86_400_000
            for key in [k for k, v in _SENT.items() if v < cutoff]:
                _SENT.pop(key, None)

    entry = {'ts': now_ms, 'action': 'sent' if ok else 'failed',
             'reason': verdict.reason, 'signal_id': sig.get('id'),
             'symbol': sig.get('symbol'), 'tf': sig.get('tf'),
             'side': sig.get('side'), 'confidence': sig.get('confidence'),
             'results': results}
    _log(entry)
    return entry


# --------------------------------------------------------------------------- #
# news alerts                                                                 #
# --------------------------------------------------------------------------- #
# Separate from signal dispatch on purpose. A signal is a thing to act on and
# carries a cooldown, a confidence floor and a watch matrix; a macro release is
# a thing to get out of the way of, and none of those gates apply to it.

_SENT_NEWS: dict = {}

IMPACT_ORDER = {'holiday': 0, 'low': 1, 'medium': 2, 'high': 3}

_IMPACT_MARK = {'high': '\U0001F534', 'medium': '\U0001F7E0', 'low': '\U0001F7E1'}


def news_key(ev: dict) -> str:
    return f"{ev.get('ts')}:{ev.get('currency')}:{(ev.get('title') or '')[:30]}"


def format_news(ev: dict, minutes: float) -> str:
    """One release, as a Telegram message."""
    mark = _IMPACT_MARK.get(ev.get('impact'), '\u26AA')
    when = (f'in {int(round(minutes))} min' if minutes >= 1
            else 'now' if minutes >= -1 else f'{int(round(-minutes))} min ago')
    lines = [
        f"{mark} <b>{_esc(ev.get('currency'))} {_esc(ev.get('title'))}</b>",
        f"<i>{_esc(ev.get('impact'))} impact \u00b7 {_esc(when)}</i>",
    ]
    stamp = ev.get('ts')
    if stamp:
        t = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
        lines.append(f"\u23f0 {t.strftime('%d %b %H:%M')} UTC")

    # Only print the numbers that exist. A row of em-dashes reads as "the data
    # is bad" rather than "this release has no forecast published".
    figures = [(k, ev.get(k)) for k in ('forecast', 'previous', 'actual') if ev.get(k)]
    if figures:
        lines.append('  '.join(f'{k}: <code>{_esc(v)}</code>' for k, v in figures))

    src = ev.get('source') or ''
    if src:
        lines.append(f'<i>via {_esc(src)}</i>')
    return '\n'.join(lines)


def should_alert_news(cfg: dict, ev: dict, now_ms: int) -> AlertVerdict:
    news_cfg = cfg.get('news') or {}
    if not news_cfg.get('enabled'):
        return AlertVerdict(False, 'news alerts are off')
    floor = IMPACT_ORDER.get(news_cfg.get('impact', 'high'), 3)
    if IMPACT_ORDER.get(ev.get('impact'), 0) < floor:
        return AlertVerdict(False, f"below the {news_cfg.get('impact')} impact floor")
    # A date-only event has no minute to count down to. Alerting "in 43 min"
    # off a midnight placeholder would be inventing a time.
    if not ev.get('time_known'):
        return AlertVerdict(False, 'release time unknown')

    lead_ms = float(news_cfg.get('lead_minutes', 10)) * 60_000
    delta = (ev.get('ts') or 0) - now_ms
    if delta > lead_ms:
        return AlertVerdict(False, 'not due yet')
    # Anything more than a lead window past is stale - typically the archive
    # being read for the first time after a restart.
    if delta < -lead_ms:
        return AlertVerdict(False, 'already gone')
    with _LOCK:
        if news_key(ev) in _SENT_NEWS:
            return AlertVerdict(False, 'already alerted')
    return AlertVerdict(True, f"{ev.get('impact')} impact within the lead window")


def dispatch_news(cfg: dict, ev: dict, force: bool = False) -> dict:
    """Send one release to every destination subscribing to the news kind."""
    now_ms = int(time.time() * 1000)
    verdict = (AlertVerdict(True, 'forced test') if force
               else should_alert_news(cfg, ev, now_ms))
    if not verdict.send:
        return {'ts': now_ms, 'action': 'suppressed', 'reason': verdict.reason,
                'kind': 'news', 'title': ev.get('title')}

    minutes = ((ev.get('ts') or now_ms) - now_ms) / 60_000.0
    text = format_news(ev, minutes)

    results = []
    for dest in cfg.get('destinations', []):
        if not dest.get('enabled'):
            continue
        if 'news' not in (dest.get('kinds') or []):
            continue
        res = send_telegram(dest.get('target', ''), text, bot=dest.get('bot'))
        results.append({'label': dest.get('label'), 'target': dest.get('target'),
                        'bot': dest.get('bot') or 'default', **res})

    if not results:
        entry = {'ts': now_ms, 'action': 'suppressed', 'kind': 'news',
                 'reason': 'no destination has the "news" kind ticked',
                 'title': ev.get('title')}
        _log(entry)
        return entry

    ok = any(r.get('ok') for r in results)
    if ok and not force:
        with _LOCK:
            _SENT_NEWS[news_key(ev)] = now_ms
            cutoff = now_ms - 86_400_000
            for k in [k for k, v in _SENT_NEWS.items() if v < cutoff]:
                _SENT_NEWS.pop(k, None)

    entry = {'ts': now_ms, 'action': 'sent' if ok else 'failed', 'kind': 'news',
             'reason': verdict.reason, 'title': ev.get('title'),
             'currency': ev.get('currency'), 'impact': ev.get('impact'),
             'results': results}
    _log(entry)
    return entry


def recent_log(limit: int = 60) -> list:
    if not ALERT_LOG.exists():
        return []
    try:
        with open(ALERT_LOG, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.reverse()
    return out


def status(cfg: dict) -> dict:
    with _LOCK:
        sent = len(_SENT)
    return {
        'bots': available_bots(),
        'enabled': bool(cfg.get('enabled')),
        'telegram_ready': telegram_ready(),
        'destinations': len([d for d in cfg.get('destinations', []) if d.get('enabled')]),
        'watched_cells': len([c for c in cfg.get('watch', []) if c.get('enabled')]),
        'sent_this_session': sent,
        'config_path': str(ALERTS_PATH),
    }


__all__ = ['KINDS', 'available_bots', 'can_post', 'describe_bot',
           'load', 'save', 'default_config', 'dispatch',
           'should_alert', 'resolve_chat', 'normalise_target',
           'format_signal', 'send_telegram', 'telegram_ready', 'matrix',
           'set_watch', 'is_watched', 'recent_log', 'status', 'ALERTS_PATH']
