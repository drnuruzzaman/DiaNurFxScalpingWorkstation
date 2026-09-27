"""
server/lab/stats.py - what a session's trades add up to.

Money is the account's own (the broker simulation charges commission and the
recorded spread), R is measured from the fill to the ORIGINAL stop, as the
executor measures it. Breakdowns answer the research questions directly:
which playbook, which session, which hour, which way, and how it ended.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from ..config import session_quality


def _group(trades: list) -> dict:
    if not trades:
        return {'n': 0}
    r = np.array([t['r'] for t in trades], dtype=float)
    p = np.array([t['profit'] for t in trades], dtype=float)
    gw, gl = p[p > 0].sum(), -p[p < 0].sum()
    return {
        'n': int(len(trades)),
        'win': round(float((p > 0).mean() * 100), 1),
        'sum_r': round(float(r.sum()), 2),
        'exp_r': round(float(r.mean()), 3),
        'net': round(float(p.sum()), 2),
        'pf': round(float(gw / gl), 2) if gl > 0 else None,
    }


def _session_of(ms: int) -> str:
    h = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).hour
    return str(session_quality(h)[0])


def compute(trades: list, balance0: float) -> dict:
    out = {'n': len(trades), 'balance0': balance0}
    if not trades:
        out.update({'net': 0.0, 'sum_r': 0.0, 'equity': [], 'by': {}, 'hist': []})
        return out
    r = np.array([t['r'] for t in trades], dtype=float)
    p = np.array([t['profit'] for t in trades], dtype=float)
    wins, losses = p[p > 0], p[p <= 0]
    eq = balance0 + np.cumsum(p)
    peak = np.maximum.accumulate(np.concatenate(([balance0], eq)))[1:]
    dd = peak - eq
    k = int(np.argmax(dd)) if len(dd) else 0
    streak = worst = 0
    for x in p:
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    rw, rl = r[r > 0], r[r <= 0]
    out.update({
        'net': round(float(p.sum()), 2),
        'sum_r': round(float(r.sum()), 2),
        'exp_r': round(float(r.mean()), 3),
        'win': round(float(len(wins) / len(p) * 100), 1),
        'pf': round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
        'avg_win_r': round(float(rw.mean()), 2) if len(rw) else 0.0,
        'avg_loss_r': round(float(rl.mean()), 2) if len(rl) else 0.0,
        'best': round(float(p.max()), 2), 'worst': round(float(p.min()), 2),
        'max_dd': round(float(dd.max()), 2) if len(dd) else 0.0,
        'max_dd_pct': round(float(dd[k] / peak[k] * 100), 2) if len(dd) and peak[k] else 0.0,
        'max_losing_streak': worst,
        'avg_bars': round(float(np.mean([t['bars'] for t in trades])), 1),
        'end_balance': round(float(eq[-1]), 2),
        'equity': [[int(trades[0]['entry_t']), round(balance0, 2)]]
        + [[int(t['exit_t']), round(float(e), 2)] for t, e in zip(trades, eq)],
    })
    by = {}
    for name, key in (('playbook', lambda t: t['playbook']),
                      ('side', lambda t: t['side']),
                      ('outcome', lambda t: t['outcome']),
                      ('session', lambda t: _session_of(t['entry_t'])),
                      ('hour', lambda t: f"{dt.datetime.fromtimestamp(t['entry_t'] / 1000, dt.timezone.utc).hour:02d}")):
        groups: dict = {}
        for t in trades:
            groups.setdefault(key(t), []).append(t)
        by[name] = {g: _group(v) for g, v in sorted(groups.items())}
    out['by'] = by
    edges = np.arange(-3.0, 6.5, 0.5)
    clipped = np.clip(r, edges[0], edges[-1] - 1e-9)
    counts, _ = np.histogram(clipped, bins=edges)
    out['hist'] = [[round(float(edges[i]), 2), int(counts[i])] for i in range(len(counts))]
    return out


# What a gate stopping many signals usually means - shown beside the counts.
GATE_MEANING = {
    'mtf': 'signals fighting the higher-timeframe direction',
    'leg': 'the fade rules at work (fading a leg where it lost before)',
    'reward': 'net R after costs too low - spread and commission eat the target',
    'volatility': 'ATR too small for the costs, or in its extreme percentile',
    'spread': 'news or a thin session blowing out the spread',
    'room': 'a strong level blocks the path to TP1',
    'regime': 'playbook firing outside its home regimes',
    'session': 'thin or closed session',
    'news': 'high-impact release nearby',
    'sizing': 'stop too wide for the risk budget',
    'broker': "stop inside the broker's minimum distance",
    'daily': 'daily trade or loss limit reached',
    'exposure': 'too many positions open',
    'engine': 'qualification error',
}


def gates(verdicts: list, sent: set, floor: int) -> dict:
    """
    Which gates stop the most signals - each FINAL signal's FIRST verdict (its
    gate ledger on the bar it was made; SignalStore.first_verdict).

    Per gate:
      block    signals it BLOCKED (a signal can be blocked by several gates)
      sole     signals it was the ONLY block on - without it they would have
               reached at least 'watch'
      warn     signals it warned on, and the average confidence it took
      decided  'watch' signals its penalty alone put under the confidence
               floor (they would have qualified without it)
    'sent' counts signals that went on to an order anyway - a verdict is
    re-taken every bar until the signal expires, so a first block is not
    always the last word.
    """
    out = {'n': len(verdicts), 'floor': floor, 'status': {}, 'sent': 0, 'gates': {},
           'playbooks': {}, 'meaning': GATE_MEANING}
    if not verdicts:
        return out
    G: dict = {}
    for v in verdicts:
        q = v.get('first_qual') or {}
        st = q.get('status') or 'unknown'
        was_sent = v.get('id') in sent
        out['status'][st] = out['status'].get(st, 0) + 1
        out['sent'] += was_sent
        pb = out['playbooks'].setdefault(v.get('playbook') or '?', {
            'n': 0, 'qualified': 0, 'watch': 0, 'rejected': 0, 'sent': 0, 'blocks': {}})
        pb['n'] += 1
        pb[st] = pb.get(st, 0) + 1
        pb['sent'] += was_sent
        rows = q.get('gates') or []
        blocks = [g for g in rows if g[1] == 'BLOCK']
        for name, verdict, pen in rows:
            g = G.setdefault(name, {'block': 0, 'sole': 0, 'warn': 0, 'pen_sum': 0,
                                    'decided': 0, 'sent_after_block': 0})
            if verdict == 'BLOCK':
                g['block'] += 1
                g['sole'] += len(blocks) == 1
                g['sent_after_block'] += was_sent
                pb['blocks'][name] = pb['blocks'].get(name, 0) + 1
            elif verdict == 'WARN':
                g['warn'] += 1
                g['pen_sum'] += int(pen or 0)
                if st == 'watch' and not blocks and                         int(q.get('confidence') or 0) + int(pen or 0) >= floor:
                    g['decided'] += 1
    for name, g in G.items():
        g['pen_avg'] = round(g.pop('pen_sum') / g['warn'], 1) if g['warn'] else None
    out['gates'] = dict(sorted(G.items(), key=lambda kv: (-kv[1]['block'], -kv[1]['decided'],
                                                           -kv[1]['warn'])))
    return out


def summary(s: dict) -> dict:
    """The few numbers a session list shows."""
    return {k: s.get(k) for k in ('n', 'net', 'sum_r', 'exp_r', 'win', 'pf', 'max_dd',
                                  'end_balance')}


__all__ = ['compute', 'summary', 'gates', 'GATE_MEANING']
