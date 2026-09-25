#!/usr/bin/env python
"""
tools/research_engulfing.py - is an engulfing candle worth trading on gold?

Read-only. XAUUSD 5m / 15m / 1h, 2024-01-01 .. 2026-08-21.

  PATTERN   bullish engulfing: previous bar bearish, this bar bullish, and this
            body covers the previous body (open <= prev close, close >= prev
            open). Bearish mirrored. A size floor (body >= 0.3 ATR) keeps doji
            pairs out.
  TRADE     decided at the close; entry at the NEXT bar's open, paying the
            bar's recorded spread (and 0.06 of commission per 0.01 lot);
            stop 0.1 ATR beyond the engulfing bar's extreme; target 1R or 2R;
            walked 48 bars; stop and target in the same bar = stop.
  BASELINE  the same trade on EVERY bar of the same colour with a body >=
            0.3 ATR - so the question is whether "engulfing" adds anything
            over "a decent candle in that direction".
  CONTEXT   split by the leg in progress (server/engine/legs.py, the avoid
            rules' read): AGAINST the leg = a reversal attempt, WITH it =
            continuation.

    python tools/research_engulfing.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import phase0b_leg_position as B                                    # noqa: E402
from server.datafeed import load_disk                               # noqa: E402
from server.engine.indicators import atr as atr_fn                  # noqa: E402

SYMBOL = 'XAUUSD.a'
POINT = 0.01
COMM = 0.06                 # price units: $6/lot round trip at 0.01 lot, $1 per $1
HORIZON = 48
OUT = ROOT / 'order_ledger' / 'research_engulfing_report.txt'


def trade(s, i, side, a, k_r):
    """Net R of the trade decided at the close of bar i."""
    j = i + 1
    if j >= len(s):
        return None
    sp = float(s.spread[j]) * POINT if s.spread is not None else 0.2
    buy = side > 0
    entry = s.o[j] + (sp if buy else 0.0)
    stop = (s.l[i] - 0.1 * a[i]) if buy else (s.h[i] + 0.1 * a[i] + sp)
    risk = (entry - stop) if buy else (stop - entry)
    if risk <= 0:
        return None
    tgt = entry + k_r * risk if buy else entry - k_r * risk
    for t in range(j, min(len(s), j + HORIZON)):
        bid_h, bid_l = s.h[t], s.l[t]
        # buys exit on the bid; sells exit on the ask (bid + spread)
        hit_stop = bid_l <= stop if buy else bid_h + sp >= stop
        hit_tgt = bid_h >= tgt if buy else bid_l + sp <= tgt
        if hit_stop:
            return -1.0 - COMM / risk
        if hit_tgt:
            return k_r - COMM / risk
    last = s.c[min(len(s), j + HORIZON) - 1]
    return ((last - entry) if buy else (entry - last - sp)) / risk - COMM / risk


def main() -> int:
    buf = io.StringIO()

    def out(line=''):
        print(line)
        buf.write(line + '\n')

    out('ENGULFING CANDLES ON GOLD   net of spread and commission   entry next open, '
        'stop 0.1 ATR beyond the candle')
    for tf in ('5m', '15m', '1h'):
        s = load_disk(SYMBOL, tf, [2024, 2025, 2026])
        a = atr_fn(s.h, s.l, s.c, 14)
        z = B.zigzag_states(s.h, s.l, a, 1.5)
        body = s.c - s.o
        big = np.abs(body) >= 0.3 * np.nan_to_num(a)
        years = (s.t / 1000 / 86400 / 365.25 + 1970).astype(int)
        groups = {}
        for i in range(20, len(s) - 2):
            if not big[i] or not a[i] > 0:
                continue
            side = 1 if body[i] > 0 else -1
            prev = body[i - 1]
            engulf = (side > 0 and prev < 0 and s.o[i] <= s.c[i - 1] and s.c[i] >= s.o[i - 1]) or \
                     (side < 0 and prev > 0 and s.o[i] >= s.c[i - 1] and s.c[i] <= s.o[i - 1])
            leg = int(z['trend'][i])
            ctx = 'none' if leg == 0 else ('with leg' if leg == side else 'against leg')
            for k_r in (1.0, 2.0):
                r = trade(s, i, side, a, k_r)
                if r is None:
                    continue
                name = 'ENGULFING' if engulf else 'any candle'
                for key in ((name, 'all', k_r), (name, ctx, k_r), (name, f'y{years[i]}', k_r)):
                    groups.setdefault(key, []).append(r)
        out()
        out(f'=== {tf}   ({len(s):,} bars)')
        out(f'    {"":12} {"subset":<12} {"target":>6} {"n":>7} {"win":>6} {"E[R] net":>9}')
        for k_r in (1.0, 2.0):
            for sub in ('all', 'against leg', 'with leg', 'y2024', 'y2025', 'y2026'):
                for name in ('ENGULFING', 'any candle'):
                    v = np.array(groups.get((name, sub, k_r), []))
                    if len(v) < 30:
                        continue
                    out(f'    {name:<12} {sub:<12} {k_r:>5.0f}R {len(v):>7,} '
                        f'{(v > 0).mean() * 100:>5.1f}% {v.mean():>+9.3f}')
            out()
    OUT.write_text(buf.getvalue(), encoding='utf-8')
    print(f'written {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
