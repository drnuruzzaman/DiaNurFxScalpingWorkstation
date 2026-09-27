#!/usr/bin/env python
"""
tools/playbook_quality.py - do the proposed quality rules make the live playbooks better?

BACKTEST ONLY. Read-only with respect to everything live: it reads data/ and
writes runs/research/playbook_quality/. The rules under test live in the engine
behind switches that are OFF in live (CONFIG.quality); switching one on here
changes only this process.

Two stages, because the analysis is the expensive part and the rules are not:

  capture   Replay the engine's analysis over every 5m bar of one year - with
            today's live settings, frozen once - and keep the analysis of every
            bar where one of the four playbooks under test (mtf_pullback,
            flag_continuation, pattern_break, false_break_fade) produces a raw
            signal. Every rule under test only tightens or re-scores signals on
            such bars, so nothing a rule could produce is missed. ~30 min a year.

  signals   For each arm (a set of switches): generate() and qualify_all() on
            the kept bars of one year, recorded as candidates the way
            tools/exp_exits.capture() records them.

  report    The trades exactly as tools/exp_exits.py evaluates them -
            cooldown, daily limits, one-bar fill delay, spread and commission,
            today's live exit (lock +0.5R at TP1, 1 ATR trail) - per arm, per
            playbook, per year, against a random-entry baseline.

    python tools/playbook_quality.py capture --year 2024
    python tools/playbook_quality.py signals --year 2024
    python tools/playbook_quality.py report

THE RULES UNDER TEST, fixed 2026-09-27 before any result was seen
(server/config.QualitySettings; every one OFF in live):

  A1  mtf_pullback needs a reclaim: an aligned rejection or sweep, or a CHoCH
      in the trend direction, within 5 bars (today it only adds evidence)
  A2  mtf_pullback needs |MTF score| >= 45 (today 30)
  A3  a 'deep' pullback needs a level scoring >= 65 inside the pocket
  A4  mtf_pullback -10 evidence when the leg WITH the entry has run > 3.5 ATR
  B1  flag_continuation needs the HTF trend with it, |MTF score| >= 45
  B2  flag quality >= 65 (detector floor today 48)
  B3  flag stop 0.5 ATR beyond the whole flag (today 0.2; the pattern itself
      calls a flag failed 0.3 ATR beyond it)
  C1  pattern_break only head & shoulders / double-triple top-bottom, quality >= 65
  C2  pattern_break only once broken, with momentum >= +15 with it and the
      last 3 bars' tick volume >= 1.2x the median of the 50 before
  C3  pattern_break only in a 'trend' regime
  F1  opposed HTF blocks every playbook; mtf_pullback and flag_continuation
      need it aligned (today opposed only warns for pattern_break and
      false_break_fade)
  F2  walk-forward track record per playbook x regime, from the years BEFORE
      the one traded: n >= 80 and mean net R <= 0 blocks the cell; n < 80
      costs 10 confidence
  F3  min_confidence 60 and 65 (today 55)
  F4  trading against a leg that has run >= 3 ATR costs 10 confidence (the
      avoid rules already block >= 4 ATR fades)
  F5  confidence calibrated to R, walk-forward: a confidence band (55-59,
      60-64, 65-69, 70+) with n >= 80 and mean net R <= 0 in the years
      before is blocked

HOW IT IS READ, also fixed in advance. Develop on 2018-2023, confirm on
2024-2026 (the confirm years are read once).
  - A rule is PICKED on 2018-2023 ONLY: on the playbook(s) it touches, mean net
    R per trade higher than today's and total net R not lower (it removes
    losers, not the winners). Its 2024-2026 column is shown, never used to pick.
  - PICK = every picked rule together (the higher picked confidence floor if
    both are picked), generated and traded as one arm.
  - A playbook is KEPT, under today's rules and under PICK, only if its mean
    net R is positive in each of 2024, 2025 and 2026, the 90% day-block
    interval of its 2024-2026 mean is above 0, and the mean beats the 95th
    percentile of a random-entry baseline (same trades, random bar and side).
  - About 20 arms are compared, so a single arm's small win is expected by
    chance; a rule needs both periods, not one. Nothing goes live from this -
    the shadow log first, and only if the user decides.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SYMBOL, TF = 'XAUUSD.a', '5m'
FOUR = ['mtf_pullback', 'flag_continuation', 'pattern_break', 'false_break_fade']
WINDOW = 600
OUT = ROOT / 'runs' / 'research' / 'playbook_quality'
DAY = 86_400_000


def _below_normal() -> None:
    from tools.forecast_build import _below_normal as bn
    bn()


def frozen_settings() -> dict:
    """Today's live settings, put on this process's CONFIG once."""
    from server.lab import settings as lab_settings
    eff = lab_settings.effective(None)
    lab_settings.activate(eff)
    return lab_settings.jsonable(eff)


def load_series():
    from server.config import MTF_LADDER
    from server.datafeed import load_disk
    years = list(range(2017, datetime.now(timezone.utc).year + 1))
    series = load_disk(SYMBOL, TF, years)
    htf = {n: load_disk(SYMBOL, n, years)
           for n in dict.fromkeys(x for x in MTF_LADDER[TF] if x != TF)}
    return series, htf


def capture(year: int) -> int:
    _below_normal()
    settings = frozen_settings()
    from server.engine.analysis import analyse, quick_trend
    from server.engine.signals import generate
    from server.lab.data import spec as lab_spec
    spec = lab_spec(SYMBOL)
    series, htf = load_series()

    def mtf_at(ts: float) -> dict:
        out = {}
        for name, s in htf.items():
            i = int(np.searchsorted(s.t, ts, 'right'))
            w = s.slice(max(0, i - 300), i)
            if len(w) >= 60:
                out[name] = quick_trend(w)
        return out

    y0 = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    y1 = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    # decision at the close of bar i-1 (the window ends there), fill from bar i
    idx = np.nonzero((series.t[:-1] >= y0) & (series.t[:-1] < y1))[0] + 1
    idx = idx[idx >= WINDOW]
    kept, t0 = [], time.time()
    for n, i in enumerate(idx):
        i = int(i)
        w = series.slice(i - WINDOW, i)
        bar_t = float(series.t[i - 1])
        snap = analyse(w, mtf_at(bar_t), spec)
        if not snap.get('ok'):
            continue
        if generate(snap, w, FOUR, True):
            kept.append((i, bar_t, zlib.compress(pickle.dumps(snap), 6)))
        if n % 5000 == 0:
            print(f'  {year}: {100 * n / max(1, idx.size):5.1f}%  kept {len(kept)}  '
                  f'{time.time() - t0:6.0f}s', flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    doc = {'year': year, 'symbol': SYMBOL, 'tf': TF, 'settings': settings,
           'settings_hash': hashlib.sha256(json.dumps(settings, sort_keys=True).encode())
           .hexdigest()[:12], 'bars': int(idx.size), 'kept': kept}
    (OUT / f'capture_{year}.pkl').write_bytes(pickle.dumps(doc))
    print(f'{year}: {idx.size} bars, {len(kept)} kept, settings {doc["settings_hash"]} '
          f'({time.time() - t0:.0f}s)', flush=True)
    return 0


# --------------------------------------------------------------------------- #
# arms                                                                        #
# --------------------------------------------------------------------------- #
A = {'pb_require_reclaim': True, 'pb_min_mtf_score': 45, 'pb_deep_needs_level': True,
     'pb_extended_atr': 3.5}
B = {'flag_need_htf': True, 'flag_min_quality': 65, 'flag_stop_pad_atr': 0.5}
C = {'pattern_classic_only': True, 'pattern_need_confirm': True, 'pattern_trend_only': True}
# engine arms: CONFIG.quality switches, need their own signals
ENGINE = {
    'base': {},
    'A1': {'pb_require_reclaim': True}, 'A2': {'pb_min_mtf_score': 45},
    'A3': {'pb_deep_needs_level': True}, 'A4': {'pb_extended_atr': 3.5}, 'A': A,
    'B1': {'flag_need_htf': True}, 'B2': {'flag_min_quality': 65},
    'B3': {'flag_stop_pad_atr': 0.5}, 'B': B,
    'C1': {'pattern_classic_only': True}, 'C2': {'pattern_need_confirm': True},
    'C3': {'pattern_trend_only': True}, 'C': C,
    'F1': {'htf_block': True}, 'F4': {'leg_fight_atr': 3.0},
    'ALL': {**A, **B, **C, 'htf_block': True, 'leg_fight_atr': 3.0},
}
# replay arms: (engine arm, min_confidence, walk-forward filters)
REPLAY = {
    **{k: (k, None, ()) for k in ENGINE},
    'F3-60': ('base', 60, ()), 'F3-65': ('base', 65, ()),
    'F2': ('base', None, ('cell',)), 'F5': ('base', None, ('calib',)),
    'ALL+F3-60': ('ALL', 60, ()), 'ALL+F2': ('ALL', None, ('cell',)),
    'ALL+F2+F5': ('ALL', None, ('cell', 'calib')),
}
TOUCHES = {'A': ['mtf_pullback'], 'B': ['flag_continuation'], 'C': ['pattern_break']}
SINGLES = ['A1', 'A2', 'A3', 'A4', 'B1', 'B2', 'B3', 'C1', 'C2', 'C3', 'F1', 'F4',
           'F3-60', 'F3-65', 'F2', 'F5']


def touches(name: str) -> list:
    """The playbooks a rule is judged on: its own for A/B/C, all four otherwise."""
    return TOUCHES.get(name[0], FOUR) if name[:3] != 'ALL' else FOUR
YEARS = list(range(2018, 2027))
DEV, CONFIRM = [y for y in YEARS if y <= 2023], [y for y in YEARS if y >= 2024]
CELL_N, CELL_PEN = 80, 10
EVAL_SKIP = {'reward', 'daily', 'exposure'}   # gates exp_exits.evaluate() re-decides
FLOOR = 55                                    # the lowest min_confidence of any arm
BANDS = [(55, 60), (60, 65), (65, 70), (70, 101)]


def set_quality(over: dict) -> None:
    from server.config import CONFIG, QualitySettings
    CONFIG.quality = QualitySettings(**over)


# --------------------------------------------------------------------------- #
# stage 2: signals per arm                                                    #
# --------------------------------------------------------------------------- #
def signals(year: int, arms: list = None, engine: dict = None, series=None) -> dict:
    """
    Every engine arm's candidates on one year's kept bars. Saved to
    signals_{year}.pkl unless `engine` (name -> switches) is passed, as the
    report does for PICK.
    """
    _below_normal()
    save = engine is None
    engine = engine or ENGINE
    doc = pickle.loads((OUT / f'capture_{year}.pkl').read_bytes())
    settings = frozen_settings()
    h = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:12]
    if h != doc['settings_hash']:
        raise SystemExit(f'{year}: settings changed since the capture '
                         f'({doc["settings_hash"]} -> {h}) - re-capture')
    from server.engine.qualify import qualify_all
    from server.engine.signals import generate
    from server.lab.data import spec as lab_spec
    spec = lab_spec(SYMBOL)
    point = float(spec.get('point') or 0.01)
    from server.config import CONFIG
    if CONFIG.gates.min_confidence < FLOOR:
        raise SystemExit(f'min_confidence {CONFIG.gates.min_confidence} < FLOOR {FLOOR}: '
                         'candidates below it would be pruned - lower FLOOR')
    if series is None:
        series, _ = load_series()
    arms = arms or list(engine)
    out = {a: [] for a in arms}
    t0 = time.time()
    ctx = {'equity': 25000.0, 'open_positions': 0, 'trades_today': 0, 'daily_pnl_pct': 0.0}
    for n, (i, bar_t, blob) in enumerate(doc['kept']):
        snap0 = pickle.loads(zlib.decompress(blob))
        w = series.slice(i - WINDOW, i)
        sp = snap0.get('spread_points')
        spread_price = float(sp) * point if sp is not None else point * 20
        for a in arms:
            set_quality(engine[a])
            snap = snap0          # generate() and qualify() only read it
            raw = generate(snap, w, FOUR, True)
            if not raw:
                continue
            raw_conf = [int(s.confidence) for s in raw]
            post = qualify_all(raw, snap, spec, ctx)
            for rc, q in zip(raw_conf, post):
                if q.status == 'conflicted':
                    continue
                # Only what exp_exits.evaluate() reads: WARN/BLOCK gates. A
                # candidate it can never trade under any arm - a block it
                # honours, or confidence under 55 before its own reward
                # penalty (every arm's floor is >= 55) - is not kept.
                gates = [(g['name'], g['verdict'], float(g.get('penalty') or 0))
                         for g in q.gates if g['verdict'] != 'PASS']
                live = [x for x in gates if x[0] not in EVAL_SKIP]
                if any(v == 'BLOCK' for _, v, _ in live) or \
                        rc - sum(p for _, v, p in live if v == 'WARN') < FLOOR:
                    continue
                out[a].append({
                    'i': i, 'bar_t': bar_t, 'playbook': q.playbook, 'side': q.side,
                    'entry_type': q.entry_type, 'trigger': q.trigger,
                    'entry': q.entry, 'stop': q.stop, 'tp1': q.tp1, 'tp2': q.tp2,
                    'atr': float(q.atr_at_signal or snap.get('atr') or 0.0),
                    'expiry': int(q.expiry_bars), 'raw_conf': rc, 'status': q.status,
                    'gates': gates,
                    'lots': float((q.sizing or {}).get('lots') or 0.0),
                    'spread_price': spread_price, 'levels': [],
                    'regime': (snap.get('regime') or {}).get('state', ''),
                })
        if n % 5000 == 0:
            print(f'  {year}: {100 * n / max(1, len(doc["kept"])):5.1f}%  '
                  f'{time.time() - t0:6.0f}s', flush=True)
    set_quality({})
    if save:
        (OUT / f'signals_{year}.pkl').write_bytes(pickle.dumps(
            {'year': year, 'settings': settings, 'settings_hash': h, 'arms': out}))
    print(f'{year}: ' + '  '.join(f'{a} {len(v)}' for a, v in out.items()) +
          f'  ({time.time() - t0:.0f}s)', flush=True)
    return out


# --------------------------------------------------------------------------- #
# stage 3: trades and the report                                              #
# --------------------------------------------------------------------------- #
def block_ci(trades: list, reps: int = 2000, seed: int = 7) -> tuple:
    """90% interval of the mean net R, resampling whole days."""
    if len(trades) < 10:
        return (float('nan'), float('nan'))
    days: dict = {}
    for t in trades:
        days.setdefault(t['entry_t'] // DAY, []).append(t['net_r'])
    groups = [np.array(v) for v in days.values()]
    sums = np.array([g.sum() for g in groups])
    cnts = np.array([g.size for g in groups])
    g = np.random.default_rng(seed)
    pick = g.integers(0, len(groups), (reps, len(groups)))
    means = sums[pick].sum(1) / cnts[pick].sum(1)
    return float(np.percentile(means, 5)), float(np.percentile(means, 95))


def summ(trades: list) -> dict:
    if not trades:
        return {'n': 0, 'mean': float('nan'), 'sum': 0.0, 'win': float('nan')}
    r = np.array([t['net_r'] for t in trades])
    return {'n': int(r.size), 'mean': float(r.mean()), 'sum': float(r.sum()),
            'win': float((r > 0).mean() * 100)}


class Book:
    """The period's price data and the exp_exits simulator, loaded once."""

    def __init__(self):
        sys.path.insert(0, str(ROOT / 'tools'))
        import exp_exits as X
        from phase1_tpsl import live_exit
        from server.datafeed import load_disk
        from server.engine.indicators import atr as atr_fn
        from server.lab.data import spec as lab_spec
        from server.config import CONFIG
        self.X, self.CONFIG = X, CONFIG
        self.exit = live_exit()
        self.spec = lab_spec(SYMBOL)
        self.series, _ = load_series()
        years = list(range(2017, datetime.now(timezone.utc).year + 1))
        m1 = load_disk(SYMBOL, '1m', years)
        self.m1 = None if m1.empty() else m1
        s = self.series
        self.atr = atr_fn(s.h, s.l, s.c, CONFIG.engine.atr_period)

    def trades(self, cands: list, min_conf=None, admit=None) -> list:
        """exp_exits.evaluate with today's gate and live exit; each trade keeps its candidate."""
        g = self.CONFIG.gates
        keep = g.min_confidence
        seen: dict = {}

        def adm(c, conf):
            if admit is not None and not admit(c, conf):
                return False
            # the first admitted per bar and key is the only one cooldown lets trade
            seen.setdefault((c['i'], c['playbook'], c['side']), (c, conf))
            return True
        try:
            if min_conf is not None:
                g.min_confidence = int(min_conf)
            out = self.X.evaluate({'spec': self.spec, 'candidates': cands}, self.series,
                                  self.m1, self.X.gate_current, self.exit, admit=adm)
        finally:
            g.min_confidence = keep
        for t in out:
            i = int(np.searchsorted(self.series.t, t['entry_t']))
            c, conf = seen[(i, t['playbook'], t['side'])]
            t.update(i=i, regime=c['regime'], conf=conf, expiry=c['expiry'],
                     stop_atr=abs(c['entry'] - c['stop']) / max(c['atr'], 1e-9),
                     tp2_r=abs(c['tp2'] - c['entry']) / max(abs(c['entry'] - c['stop']), 1e-9),
                     year=datetime.fromtimestamp(t['entry_t'] / 1000, timezone.utc).year)
        return out

    def random_means(self, trades: list, reps: int = 20, seed: int = 11) -> np.ndarray:
        """
        Mean net R of the same trades - same count per year, same stop in ATR,
        same TP2 in R, same live exit - entered at random bars with a random side.
        """
        s, point = self.series, float(self.spec.get('point') or 0.01)
        slip = float(self.CONFIG.instrument.default_slippage_points) * point
        g = np.random.default_rng(seed)
        by_year: dict = {}
        for t in trades:
            by_year.setdefault(t['year'], []).append(t)
        pools = {}
        for y in by_year:
            y0 = int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            y1 = int(datetime(y + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            idx = np.nonzero((s.t >= y0) & (s.t < y1))[0]
            pools[y] = idx[(idx > WINDOW) & (idx < len(s) - 50)]
        means = []
        for _ in range(reps):
            rs = []
            for y, ts in by_year.items():
                for t, i in zip(ts, g.choice(pools[y], len(ts))):
                    i = int(i)
                    a = float(self.atr[i - 1])
                    if not a > 0:
                        continue
                    side = 'buy' if g.random() < 0.5 else 'sell'
                    sign = 1.0 if side == 'buy' else -1.0
                    fill = float(s.o[i]) + sign * slip
                    risk = t['stop_atr'] * a
                    sp = s.spread[i - 1] if s.spread is not None else 20
                    c = {'side': side, 'entry': fill, 'stop': fill - sign * risk,
                         'tp1': fill + sign * risk, 'tp2': fill + sign * risk * t['tp2_r'],
                         'atr': a, 'expiry': t['expiry'], 'spread_price': float(sp) * point}
                    r = self.X.simulate(c, self.exit, s, self.m1, self.spec, fill, i + 1)
                    if r is not None:
                        rs.append(r['net_r'])
            means.append(np.mean(rs) if rs else np.nan)
        return np.array(means)


def walk_forward(book: Book, cands_by_year: dict, min_conf, filters: tuple,
                 history: dict) -> list:
    """F2 / F5: each year gated by the track record of the years before it."""
    out = []
    for y in YEARS:
        prior = [t for yy in YEARS if yy < y for t in history[yy]]
        cell: dict = {}
        band: dict = {}
        for t in prior:
            cell.setdefault((t['playbook'], t['regime']), []).append(t['net_r'])
            for lo, hi in BANDS:
                if lo <= t['conf'] < hi:
                    band.setdefault(lo, []).append(t['net_r'])
        floor = book.CONFIG.gates.min_confidence if min_conf is None else min_conf

        def admit(c, conf, cell=cell, band=band, floor=floor):
            if 'cell' in filters:
                r = cell.get((c['playbook'], c['regime']), [])
                if len(r) >= CELL_N and np.mean(r) <= 0:
                    return False
                if len(r) < CELL_N:
                    conf -= CELL_PEN
                    if conf < floor:
                        return False
            if 'calib' in filters:
                for lo, hi in BANDS:
                    if lo <= conf < hi:
                        r = band.get(lo, [])
                        if len(r) >= CELL_N and np.mean(r) <= 0:
                            return False
            return True
        out += book.trades(cands_by_year[y], min_conf, admit)
    return out


def report() -> int:
    _below_normal()
    frozen_settings()
    t0 = time.time()
    sig = {}
    for y in YEARS:
        f = OUT / f'signals_{y}.pkl'
        if not f.exists():
            raise SystemExit(f'missing {f.name} - run: signals --year {y}')
        sig[y] = pickle.loads(f.read_bytes())
    hashes = {d['settings_hash'] for d in sig.values()}
    if len(hashes) != 1:
        raise SystemExit(f'years captured under different settings: {hashes}')
    book = Book()
    print(f'loaded ({time.time() - t0:.0f}s)', flush=True)

    res: dict = {}
    plain: dict = {}                      # engine arm -> {year: trades}, for F2/F5 history
    for name, (eng, mc, filters) in REPLAY.items():
        cands = {y: sig[y]['arms'][eng] for y in YEARS}
        if not filters:
            by_year = {y: book.trades(cands[y], mc) for y in YEARS}
            if mc is None:
                plain[eng] = by_year
            res[name] = [t for y in YEARS for t in by_year[y]]
        else:
            res[name] = walk_forward(book, cands, mc, filters, plain[eng])
        print(f'  {name:<10} {len(res[name]):>6} trades ({time.time() - t0:.0f}s)', flush=True)

    # Rules picked on 2018-2023 ONLY, then generated and traded together.
    def dev_summ(name, pbs):
        return summ([t for t in res[name] if t['playbook'] in pbs and t['year'] in DEV])

    picked = []
    for name in SINGLES:
        pbs = touches(name)
        b, a = dev_summ('base', pbs), dev_summ(name, pbs)
        if a['n'] and a['mean'] > b['mean'] and a['sum'] >= b['sum']:
            picked.append(name)
    over = {k: v for n in picked if n in ENGINE for k, v in ENGINE[n].items()}
    floors = [REPLAY[n][1] for n in picked if n.startswith('F3')]
    filters = tuple(f for n in picked if n in ('F2', 'F5') for f in REPLAY[n][2])
    print(f'picked on 2018-2023: {picked or "nothing"}', flush=True)
    (OUT / 'pick.json').write_text(json.dumps({'picked': picked, 'switches': over,
                                               'min_confidence': max(floors) if floors else None,
                                               'filters': filters}, indent=1))
    if picked:
        pc = {y: signals(y, ['PICK'], {'PICK': over}, book.series)['PICK'] for y in YEARS}
        mc = max(floors) if floors else None
        plain_pick = {y: book.trades(pc[y], None) for y in YEARS}
        res['PICK'] = (walk_forward(book, pc, mc, filters, plain_pick) if filters else
                       [t for y in YEARS for t in book.trades(pc[y], mc)])
    else:
        res['PICK'] = res['base']
    arms = list(REPLAY) + ['PICK']

    lines = []
    w = lines.append
    w(f'Playbook quality - XAUUSD.a 5m, 2018-2026, live settings '
      f'{next(iter(hashes))}, live exit, full costs.')
    w(f'Dev 2018-2023, confirm 2024-2026. Rules and reading fixed in advance '
      f'(tools/playbook_quality.py docstring).  {datetime.now():%Y-%m-%d %H:%M}')
    w('')

    def row(label, ts):
        s = summ(ts)
        return (f'{label:<24}{s["n"]:>6} {s["mean"]:>+7.3f} {s["sum"]:>+8.1f}'
                if s['n'] else f'{label:<24}{0:>6}')

    # 1. every arm, every playbook: dev vs confirm
    w('1. Mean net R per trade (n, mean, total) - dev 2018-23 | confirm 2024-26')
    for pb in FOUR + ['ALL PLAYBOOKS']:
        w(f'\n  {pb}')
        for name in arms:
            ts = res[name] if pb == 'ALL PLAYBOOKS' else \
                [t for t in res[name] if t['playbook'] == pb]
            dv = [t for t in ts if t['year'] in DEV]
            cf = [t for t in ts if t['year'] in CONFIRM]
            w('    ' + row(name, dv) + '  | ' + row('', cf)[24:])

    # 2. each rule against today; picked on dev only
    w('\n2. Each rule against today, on the playbooks it touches. PICKED (on 2018-23 only) = '
      'mean up and total not lower. The confirm column is shown, not used.')
    for name in arms[1:]:
        pbs = touches(name)
        b = [t for t in res['base'] if t['playbook'] in pbs]
        a = [t for t in res[name] if t['playbook'] in pbs]
        bd, bc = summ([t for t in b if t['year'] in DEV]), summ([t for t in b if t['year'] in CONFIRM])
        ad, ac = summ([t for t in a if t['year'] in DEV]), summ([t for t in a if t['year'] in CONFIRM])
        w(f'  {name:<10} {",".join(p[:8] for p in pbs):<36} dev {bd["mean"]:+.3f} -> '
          f'{ad["mean"]:+.3f} (total {bd["sum"]:+.1f} -> {ad["sum"]:+.1f})  confirm '
          f'{bc["mean"]:+.3f} -> {ac["mean"]:+.3f} (total {bc["sum"]:+.1f} -> {ac["sum"]:+.1f})'
          f'{"  PICKED" if name in picked else ""}')
    w(f'\n  PICK = {" + ".join(picked) if picked else "nothing (PICK = today)"}')

    # 3. keep rule per playbook: today, PICK, and ALL for reference
    w('\n3. Keep rule per playbook: every confirm year > 0, 90% CI above 0, beats random p95')
    for name in ('base', 'PICK', 'ALL'):
        w(f'\n  arm {name}')
        for pb in FOUR:
            ts = [t for t in res[name] if t['playbook'] == pb]
            cf = [t for t in ts if t['year'] in CONFIRM]
            yrs = [summ([t for t in cf if t['year'] == y]) for y in CONFIRM]
            lo, hi = block_ci(cf)
            rnd = book.random_means(cf, reps=100) if cf else np.array([np.nan])
            p95 = float(np.nanpercentile(rnd, 95)) if np.isfinite(rnd).any() else float('nan')
            m = summ(cf)['mean']
            keep = (all(s['n'] and s['mean'] > 0 for s in yrs) and lo > 0 and m > p95)
            w(f'    {pb:<18} ' + '  '.join(f'{y} {s["n"]:>4} {s["mean"]:+.3f}'
                                           for y, s in zip(CONFIRM, yrs)) +
              f'  CI [{lo:+.3f}, {hi:+.3f}]  random p95 {p95:+.3f}  '
              f'{"KEEP" if keep else "drop"}')

    # 4. per year, today vs ALL
    w('\n4. Per year, all four playbooks (n, mean, total): base | PICK | ALL')
    for y in YEARS:
        w(f'  {y}  ' + '  | '.join(row('', [t for t in res[k] if t['year'] == y])[24:]
                                  for k in ('base', 'PICK', 'ALL')))

    txt = '\n'.join(lines)
    (OUT / 'report.txt').write_text(txt, encoding='utf-8')
    (OUT / 'trades.pkl').write_bytes(pickle.dumps(res))
    print(txt)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('capture')
    c.add_argument('--year', type=int, required=True)
    s = sub.add_parser('signals')
    s.add_argument('--year', type=int, required=True)
    s.add_argument('--arms', nargs='*', help='engine arms (default: all)')
    sub.add_parser('report')
    a = ap.parse_args()
    if a.cmd == 'capture':
        return capture(a.year)
    if a.cmd == 'signals':
        signals(a.year, a.arms)
        return 0
    return report()


if __name__ == '__main__':
    sys.exit(main())
