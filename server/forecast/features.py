"""
server/forecast/features.py - one feature row per closed bar.

Everything a forecast may condition on at a bar's close, and nothing it may
not. The rows line up one-to-one with the market labels (same `t`).

  engine reads     the chart's regime, its votes and volatility read, the leg
                   in progress - reads.py, same windows as live
  MTF              each ladder rung's quick_trend read AS OF the close (the
                   last rung bar CLOSED by then, as the lab and live build it),
                   the alignment score, and the higher-timeframe direction the
                   leg gate reads (legs.htf_trend)
  realised range   mean true range over the horizon, a day and a week, in ATR
  momentum         net move over the horizon and over a day, in ATR
  time             UTC hour, weekday, the engine's session (true UTC)
  news             scheduled releases around the close (news.py)

Causal by construction: every rolling window ends at the bar, every as-of
join takes rung bars closed at or before the close, and news uses scheduled
times only. tools/test_forecast.py checks it by truncation - a feature row
computed on history cut at that bar must equal the row computed on all of it.
"""
from __future__ import annotations

import numpy as np

from ..config import MTF_LADDER, TF_SECONDS
from ..datafeed import load_disk
from . import HORIZON, TRADE_HORIZON, TRAIN_START, ms_of, news, reads
from . import timebase as tb
from .labels import _years as data_years

# The alignment weights and strength multipliers of regime.mtf_alignment().
MTF_WEIGHTS = {'1m': 1.0, '3m': 1.2, '5m': 1.5, '15m': 2.0, '30m': 2.3,
               '1h': 2.8, '2h': 3.0, '4h': 3.4, '1d': 4.0, '1w': 4.5}

BARS_PER_DAY = {'5m': 276, '15m': 92, '1h': 23, '4h': 6}

READ_COLS = ('atr', 'state', 'conf', 's_trend', 's_range', 's_transition', 's_squeeze', 'adx',
             'er', 'slope_atr', 'dir_share', 'vol', 'atr_pct', 'bbw_pct', 'squeeze', 'expanding',
             'contracting', 'leg_dir', 'leg_ext', 'leg_pull', 'leg_depth', 'leg_age', 'ext_age',
             'qt')


def rungs(tf: str) -> list:
    """The ladder rungs above `tf`, in ladder order, deduplicated."""
    return [x for x in dict.fromkeys(MTF_LADDER.get(tf, [tf])) if x != tf]


def asof(rung_t, rung_vals, rung_tf: str, close_ms, missing):
    """Each close's value from the last rung bar CLOSED at or before it."""
    rc = np.asarray(rung_t, dtype=np.int64) + TF_SECONDS[rung_tf] * 1000
    j = np.searchsorted(rc, np.asarray(close_ms, dtype=np.int64), 'right') - 1
    return np.where(j >= 0, rung_vals[np.maximum(j, 0)], missing)


def mtf_block(tf: str, own_qt, rung_qts: dict) -> dict:
    """
    regime.mtf_alignment() and legs.htf_trend(), vectorised over rows, from
    quick_trend codes (+-3 strong .. 0 ranging, QT_MISSING absent).
    """
    n = own_qt.size
    signed = np.zeros(n)
    total = np.zeros(n)
    pos = np.zeros(n)
    nz = np.zeros(n)
    for name, q in [(tf, own_qt)] + list(rung_qts.items()):
        present = q != reads.QT_MISSING
        w = MTF_WEIGHTS.get(name, 1.0)
        mult = np.select([np.abs(q) == 3, np.abs(q) == 2, np.abs(q) == 1], [1.0, 0.7, 0.4], 0.0)
        vote = np.where(present, np.sign(q) * mult, 0.0)
        signed += vote * w
        total += np.where(present, w, 0.0)
        pos += (vote > 0)
        nz += (vote != 0)
    with np.errstate(invalid='ignore', divide='ignore'):
        score = np.where(total > 0, np.round(100 * signed / np.where(total > 0, total, 1)), 0)
        agree = np.where(nz > 0, np.maximum(pos, nz - pos) / np.where(nz > 0, nz, 1), 0.0)
    # The rung the leg gate reads: the smallest rung at or above max(1h, base + 1s)
    # that has a read.
    want = max(3600, TF_SECONDS[tf] + 1)
    htf = np.zeros(n, dtype=np.int8)
    found = np.zeros(n, dtype=bool)
    for name in sorted(rung_qts, key=lambda x: TF_SECONDS[x]):
        if TF_SECONDS[name] < want:
            continue
        q = rung_qts[name]
        take = (~found) & (q != reads.QT_MISSING)
        htf = np.where(take, np.sign(q), htf).astype(np.int8)
        found |= take
    return {'mtf_score': score.astype(np.float32), 'mtf_agree': agree.astype(np.float32),
            'htf_dir': htf}


def _rolling_mean(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.size, np.nan)
    if x.size >= n:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def assemble(symbol: str, tf: str, start_ms: int = None, end_ms: int = None,
             ev: dict = None) -> dict:
    """Feature rows for every `tf` bar from TRAIN_START (or start_ms) to end_ms."""
    start_ms = ms_of(TRAIN_START) if start_ms is None else start_ms
    bars = load_disk(symbol, tf, data_years(symbol, tf), None, end_ms)
    rd = reads.load(symbol, tf, reads.years_for(symbol, tf))
    if len(bars):                          # an end date cuts the bars; cut their reads with them
        keep = rd['t'] <= int(bars.t[-1])
        rd = {k: v[keep] for k, v in rd.items()}
    rung_reads = {r: reads.load(symbol, r, reads.years_for(symbol, r)) for r in rungs(tf)}
    return assemble_arrays(tf, bars, rd, rung_reads, start_ms, ev)


def assemble_arrays(tf: str, bars, rd: dict, rung_reads: dict, start_ms: int,
                    ev: dict = None) -> dict:
    """The feature rows from bars, their reads and the rungs' reads. Pure."""
    t_all = bars.t.astype(np.int64)
    tf_ms = TF_SECONDS[tf] * 1000
    H = HORIZON[tf]
    rows = np.nonzero(t_all >= start_ms)[0]
    t = t_all[rows]
    close = t + tf_ms
    out = {'t': t, 'close_ms': close}
    for col in READ_COLS:
        src = rd[col]
        fill = np.nan if src.dtype.kind == 'f' else (reads.QT_MISSING if col == 'qt' else -1)
        if col == 'leg_dir':
            fill = 0
        out[col] = reads.aligned(t_all, rd, col, fill)[rows]
    # ---- MTF
    rung_qts = {}
    for r, rr in rung_reads.items():
        rung_qts[r] = asof(rr['t'], rr['qt'], r, close, reads.QT_MISSING).astype(np.int8)
        out[f'qt_{r}'] = rung_qts[r]
    out.update(mtf_block(tf, out['qt'], rung_qts))
    # ---- realised range and momentum, in ATR of the bar
    h, l, c = bars.h, bars.l, bars.c
    prev = np.roll(c, 1)
    prev[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    atr = out['atr'].astype(np.float64)
    day = BARS_PER_DAY[tf]
    with np.errstate(invalid='ignore', divide='ignore'):
        out['rr_h'] = (_rolling_mean(tr, H)[rows] / atr).astype(np.float32)
        out['rr_d'] = (_rolling_mean(tr, day)[rows] / atr).astype(np.float32)
        out['rr_w'] = (_rolling_mean(tr, 5 * day)[rows] / atr).astype(np.float32)
        back_h = np.maximum(rows - H, 0)
        back_d = np.maximum(rows - day, 0)
        out['mom_h'] = np.where(rows >= H, (c[rows] - c[back_h]) / atr, np.nan).astype(np.float32)
        out['mom_d'] = np.where(rows >= day, (c[rows] - c[back_d]) / atr, np.nan).astype(np.float32)
    # ---- time (true UTC)
    close_utc = tb.broker_to_utc(close)
    out['close_utc'] = close_utc
    out['utc_hour'] = tb.utc_hour(close_utc)
    out['dow'] = tb.weekday(close_utc)
    out['session'] = tb.session_code(close_utc)
    out['day'] = tb.broker_day_start(close)
    # ---- news
    out.update(news.features(close_utc, H * tf_ms, TRADE_HORIZON * 60_000, ev))
    return out


def years_of(close_ms) -> np.ndarray:
    """Broker-time calendar year of each close."""
    c = np.asarray(close_ms, dtype='datetime64[ms]')
    return c.astype('datetime64[Y]').astype(np.int64) + 1970


__all__ = ['MTF_WEIGHTS', 'BARS_PER_DAY', 'READ_COLS', 'rungs', 'asof', 'mtf_block', 'assemble',
           'assemble_arrays', 'years_of']
