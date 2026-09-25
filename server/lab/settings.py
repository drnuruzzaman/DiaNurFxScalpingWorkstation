"""
server/lab/settings.py - a session's strategy settings.

Every session starts from the LIVE settings (configs/settings.json over the
code defaults) and layers its own overrides on top, so "what does the live
strategy do here" is the default question and "what if min_stop_atr were 2.0"
is one change away. The result is applied to this process's CONFIG only - the
lab runs in its own process - and settings.json is only ever read.
"""
from __future__ import annotations

import copy
import json
from dataclasses import fields
from pathlib import Path

from ..config import CONFIG

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / 'configs' / 'settings.json'
GROUPS = ('risk', 'gates', 'execution', 'engine', 'instrument')

# The code defaults, captured before anything is applied. Every session is
# built from these, so a setting one session changed can never leak into the
# next one.
_PRISTINE = {g: copy.deepcopy(getattr(CONFIG, g)) for g in GROUPS}


def _cast(current, value):
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, tuple):
        return tuple(value)
    if isinstance(current, str):
        return str(value)
    return value


def live_saved() -> dict:
    """configs/settings.json as saved by the live app, with legacy keys migrated."""
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    ex = saved.get('execution')
    if isinstance(ex, dict) and ('min_lots' in ex or 'max_lots' in ex):
        old = [float(v) for v in (ex.pop('min_lots', None), ex.pop('max_lots', None))
               if v is not None]
        if old:
            ex.setdefault('lots_gold', min(old))
    return saved


def effective(overrides: dict = None) -> dict:
    """{group: {key: value}} for every setting: defaults <- live <- overrides."""
    out = {}
    layers = (live_saved(), overrides or {})
    for g in GROUPS:
        base = _PRISTINE[g]
        vals = {f.name: copy.deepcopy(getattr(base, f.name)) for f in fields(base)}
        for layer in layers:
            for k, v in (layer.get(g) or {}).items():
                if k in vals:
                    try:
                        vals[k] = _cast(vals[k], v)
                    except (TypeError, ValueError):
                        continue
        out[g] = vals
    return out


def activate(eff: dict) -> None:
    """Put a session's settings onto this process's CONFIG."""
    for g in GROUPS:
        target = getattr(CONFIG, g)
        for k, v in (eff.get(g) or {}).items():
            if hasattr(target, k):
                setattr(target, k, copy.deepcopy(v))


def changed_from_live(eff: dict) -> dict:
    """The keys where a session differs from the live strategy, for the UI."""
    live = effective(None)
    out = {}
    for g in GROUPS:
        for k, v in (eff.get(g) or {}).items():
            if live[g].get(k) != v:
                out.setdefault(g, {})[k] = {'live': live[g].get(k), 'session': v}
    return out


def jsonable(eff: dict) -> dict:
    return {g: {k: (list(v) if isinstance(v, tuple) else v) for k, v in vals.items()}
            for g, vals in eff.items()}


# The settings the Strategy panel offers. Curated rather than every field:
# these are the ones a strategy question is actually about. type drives the
# editor; lo/hi are sanity bounds, not recommendations.
SCHEMA = [
    {'group': 'risk', 'key': 'equity', 'label': 'Starting balance', 'type': 'float',
     'lo': 100, 'hi': 10_000_000, 'hint': 'account currency'},
    {'group': 'execution', 'key': 'lots_gold', 'label': 'Lots (gold)', 'type': 'float',
     'lo': 0.01, 'hi': 5},
    {'group': 'execution', 'key': 'lots_non_gold', 'label': 'Lots (other symbols)',
     'type': 'float', 'lo': 0.01, 'hi': 5},
    {'group': 'execution', 'key': 'entry_tolerance_atr', 'label': 'Entry tolerance (ATR)',
     'type': 'float', 'lo': 0, 'hi': 2, 'hint': 'market if price is this close, else pending'},
    {'group': 'execution', 'key': 'max_per_symbol_tf', 'label': 'Max orders per slot',
     'type': 'int', 'lo': 0, 'hi': 20},
    {'group': 'execution', 'key': 'broker_tp', 'label': 'Broker TP', 'type': 'enum',
     'options': ['none', 'tp1', 'tp2', 'cap']},
    {'group': 'risk', 'key': 'exit_mode', 'label': 'Exit', 'type': 'enum',
     'options': ['trail', 'partial']},
    {'group': 'risk', 'key': 'trail_lock_r', 'label': 'Lock at TP1 (R)', 'type': 'float',
     'lo': 0, 'hi': 0.95},
    {'group': 'risk', 'key': 'trail_atr', 'label': 'Trail distance (ATR)', 'type': 'float',
     'lo': 0.2, 'hi': 5},
    {'group': 'risk', 'key': 'min_stop_atr', 'label': 'Min stop (ATR)', 'type': 'float',
     'lo': 0.1, 'hi': 5},
    {'group': 'risk', 'key': 'tp1_r', 'label': 'TP1 (R)', 'type': 'float', 'lo': 0.3, 'hi': 5},
    {'group': 'risk', 'key': 'tp2_r', 'label': 'TP2 (R)', 'type': 'float', 'lo': 0.5, 'hi': 10},
    {'group': 'risk', 'key': 'min_rr', 'label': 'Min R:R (net, TP2)', 'type': 'float',
     'lo': 0, 'hi': 5},
    {'group': 'risk', 'key': 'max_concurrent', 'label': 'Max open orders', 'type': 'int',
     'lo': 1, 'hi': 50},
    {'group': 'risk', 'key': 'max_daily_trades', 'label': 'Max trades per day', 'type': 'int',
     'lo': 1, 'hi': 500},
    {'group': 'risk', 'key': 'max_daily_loss_pct', 'label': 'Daily loss limit %',
     'type': 'float', 'lo': 0.1, 'hi': 20},
    {'group': 'gates', 'key': 'min_confidence', 'label': 'Min confidence', 'type': 'int',
     'lo': 0, 'hi': 100},
    {'group': 'gates', 'key': 'max_spread_points', 'label': 'Max spread (points)',
     'type': 'float', 'lo': 1, 'hi': 500},
    {'group': 'gates', 'key': 'min_mtf_score', 'label': 'Min MTF score', 'type': 'int',
     'lo': 0, 'hi': 100},
    {'group': 'gates', 'key': 'require_mtf_agreement', 'label': 'Continuation needs HTF',
     'type': 'bool'},
    {'group': 'gates', 'key': 'cooldown_bars', 'label': 'Cooldown (bars)', 'type': 'int',
     'lo': 0, 'hi': 100},
    {'group': 'gates', 'key': 'avoid_fades', 'label': 'Avoid fading the leg', 'type': 'bool'},
    {'group': 'gates', 'key': 'session_blocks', 'label': 'Block thin sessions', 'type': 'bool'},
    {'group': 'gates', 'key': 'disabled_playbooks', 'label': 'Playbooks off', 'type': 'playbooks'},
]


__all__ = ['effective', 'activate', 'changed_from_live', 'jsonable', 'live_saved', 'SCHEMA']
