#!/usr/bin/env python
"""
tools/test_forecast.py - the forecast engine's guarantees, checked (Phase A).

    1. broker time <-> UTC is exact across both clock changes; sessions
    2. the cached reads ARE the engine's reads: analyse() and the lab agree
    3. features are causal: history cut at a bar gives that bar the same row
    4. MTF alignment matches regime.mtf_alignment / legs.htf_trend
    5. labels: the minute-path rule, excursions, and trade outcomes identical
       to the lab broker actually filling the trade
    6. baselines use only outcomes resolved before the day starts
    7. scoring rules give their known values
    8. news features see scheduled times only, correctly
    9. a batch run is deterministic, and nothing imports the live API
   10. Phase B: monthly tables are resolved-only and shrink thin cells; the
       range cells decode; grid barrier odds match the exact replay; the lab's
       forecast payload has no look-ahead and settles against the labels; each
       symbol reads its own gates, never another's
   11. Phase C: lift tables are resolved-only and shrink thin cells; forecasts
       stay probabilities; the confidence rules; analogs only from resolved,
       thinned history
   12. Phase D: the forecast filters veto what they say they veto, unknown
       filters are refused, a filtered replay journals its vetoes, and a
       filter test keeps one frozen copy of the settings
   13. Phase E: the news model refines Phase B's news cell and nothing else;
       the challenge's fact sheet, rule findings and number tracing
   14. Shadow live: from a closed-bar analysis the shadow reproduces the offline
       regime forecast, logs 5m sends only, abstains rather than guesses

Run:  python tools/test_forecast.py        (needs tools/forecast_build.py caches)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0
SYM = 'XAUUSD.a'


def check(name: str, ok: bool, detail: str = '') -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f'  [ok]   {name}' + (f' - {detail}' if detail else ''))
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f' - {detail}' if detail else ''))


def section(title: str) -> None:
    print(f'\n{title}\n' + '-' * len(title))


def _same(a, b) -> bool:
    a, b = np.asarray(a), np.asarray(b)
    if a.dtype.kind == 'f' or b.dtype.kind == 'f':
        x, y = float(a), float(b)
        return (np.isnan(x) and np.isnan(y)) or abs(x - y) < 1e-6
    return bool(a == b)


def ms(s: str) -> int:
    from datetime import datetime, timezone
    return int(datetime.strptime(s, '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp()
               * 1000)


def main() -> int:
    from server.forecast import TEMPLATES, TRADE_HORIZON, sync_engine_settings
    sync_engine_settings()          # analyse() here must run with the live engine settings
    from server.forecast import baseline as bl
    from server.forecast import features as ft
    from server.forecast import labels as lb
    from server.forecast import news as nw
    from server.forecast import reads as rd
    from server.forecast import score as sc
    from server.forecast import timebase as tb

    # ------------------------------------------------------------------ 1 --- #
    section('1. broker time <-> UTC, sessions')
    pairs = [
        ('2024-01-02 01:00', '2024-01-01 23:00', 'winter: broker = UTC+2'),
        ('2024-07-01 01:00', '2024-06-30 22:00', 'summer: broker = UTC+3'),
        ('2024-03-08 23:00', '2024-03-08 21:00', 'the Friday before the US change: +2'),
        ('2024-03-11 01:00', '2024-03-10 22:00', 'the Monday after it: +3'),
        ('2024-03-20 12:00', '2024-03-20 09:00', 'between the US and EU changes: +3 (US rule)'),
        ('2024-11-01 12:00', '2024-11-01 09:00', 'before the US autumn change: +3'),
        ('2024-11-04 12:00', '2024-11-04 10:00', 'after it: +2'),
    ]
    for b, u, what in pairs:
        got = int(tb.broker_to_utc([ms(b)])[0])
        check(what, got == ms(u), f'{b} broker -> {u} UTC')
    rng = np.random.default_rng(3)
    xs = ms('2018-01-01 00:00') + rng.integers(0, 8 * 365 * 86400, 5000) * 1000
    # Trading days only: the broker clock itself skips / repeats an hour on the
    # Sunday mornings of the US change, when the market is shut.
    xs = xs[(((xs // 86_400_000) + 3) % 7) < 5]
    check('round trip broker -> UTC -> broker (Mon-Fri)', bool(np.array_equal(
        tb.utc_to_broker(tb.broker_to_utc(xs)), xs)), f'{xs.size} instants')
    s_codes = tb.session_code([ms('2025-05-06 13:00'), ms('2025-05-06 03:00'),
                               ms('2025-05-06 22:00'), ms('2025-05-06 08:00')])
    check('sessions from true UTC', [tb.SESSION_NAMES[i] for i in s_codes] ==
          ['overlap', 'tokyo', 'sydney', 'london'], str([tb.SESSION_NAMES[i] for i in s_codes]))
    check('weekday in UTC', int(tb.weekday([ms('2026-09-25 12:00')])[0]) == 4, 'a Friday')

    # ------------------------------------------------------------------ 2 --- #
    section('2. the cached reads are the engine\'s reads')
    from server.datafeed import load_disk
    from server.engine.analysis import analyse, quick_trend
    for tf, yr in (('5m', 2025), ('15m', 2024), ('1h', 2026), ('4h', 2024)):
        z = rd.load(SYM, tf, [yr])
        s = load_disk(SYM, tf, [yr - 1, yr])
        bad, qbad = 0, 0
        for j in np.random.default_rng(len(tf) + yr).choice(z['t'].size, 12, replace=False):
            k = int(np.searchsorted(s.t, z['t'][j]))
            snap = analyse(s.slice(k + 1 - 600, k + 1))
            reg, leg = snap['regime'], snap['leg']
            same = (rd.STATES[z['state'][j]] == reg['label'] and z['conf'][j] == reg['confidence']
                    and int(z['s_trend'][j]) == reg['scores']['trend']
                    and abs(float(z['atr'][j]) - snap['atr']) < 1e-3
                    and ((leg is None and z['leg_dir'][j] == 0) or
                         (leg is not None and z['leg_dir'][j] == leg['dir']
                          and abs(z['leg_ext'][j] - leg['ext_atr']) < 1e-4
                          and abs(z['leg_pull'][j] - leg['pull_atr']) < 1e-4)))
            bad += not same
            qbad += int(z['qt'][j]) != rd.qt_code(quick_trend(s.slice(k + 1 - 300, k + 1)))
        check(f'{tf} {yr}: regime and leg identical to analyse()', bad == 0, f'{bad} of 12 differ')
        check(f'{tf} {yr}: MTF read identical to quick_trend()', qbad == 0, f'{qbad} of 12 differ')

    # The lab: a real replay session's snapshots against the feature rows.
    from server.lab.session import ReplaySession
    sess = ReplaySession({'symbol': SYM, 'tf': '15m', 'start': '2025-03-03T00:00',
                          'end': '2025-03-07T00:00', 'record': False})
    sess.advance(160)
    F15 = ft.assemble(SYM, '15m', ms('2025-03-01 00:00'), ms('2025-03-08 00:00'))
    lab_bad, rows = 0, 0
    for k in range(sess.i0, sess.k + 1, 7):
        snap = sess.snapshot_at(k)
        j = int(np.searchsorted(F15['t'], int(sess.series.t[k])))
        if j >= F15['t'].size or F15['t'][j] != int(sess.series.t[k]) or not snap.get('ok'):
            lab_bad += 1
            continue
        rows += 1
        mtf = {r['tf']: r for r in snap['mtf']['rows']}
        same = (rd.STATES[F15['state'][j]] == snap['regime']['label']
                and int(F15['conf'][j]) == snap['regime']['confidence']
                and int(F15['mtf_score'][j]) == snap['mtf']['score']
                and abs(float(F15['mtf_agree'][j]) - snap['mtf']['agreement']) < 0.006
                and all(int(F15[f'qt_{r}'][j]) == rd.qt_code(
                    {'state': mtf[r]['state'], 'strength': mtf[r]['strength']}) if r in mtf
                    else int(F15[f'qt_{r}'][j]) == rd.QT_MISSING for r in ft.rungs('15m'))
                and int(F15['htf_dir'][j]) == int(snap['leg']['htf_dir'] if snap['leg'] else
                                                  F15['htf_dir'][j]))
        lab_bad += not same
    check('lab replay (15m) regime, MTF rows, alignment and leg-gate rung match the features',
          lab_bad == 0 and rows > 15, f'{rows} bars compared, {lab_bad} differ')

    # ------------------------------------------------------------------ 3 --- #
    section('3. features are causal (truncation)')
    for tf in ('5m', '1h'):
        yrs = rd.years_for(SYM, tf)
        bars = load_disk(SYM, tf, [2024, 2025])
        r_all = rd.load(SYM, tf, [y for y in yrs if y in (2024, 2025)])
        rung = {r: rd.load(SYM, r, [y for y in rd.years_for(SYM, r) if y in (2023, 2024, 2025)])
                for r in ft.rungs(tf)}
        start = ms('2025-01-01 00:00')
        full = ft.assemble_arrays(tf, bars, r_all, rung, start)
        bad = 0
        picks = np.random.default_rng(5).choice(full['t'].size - 1, 6, replace=False)
        for j in picks:
            t_j = int(full['t'][j])
            k = int(np.searchsorted(bars.t, t_j))
            cut = bars.slice(0, k + 1)
            r_cut = {c: v[r_all['t'] <= t_j] for c, v in r_all.items()}
            rung_cut = {r: {c: v[x['t'] <= t_j] for c, v in x.items()} for r, x in rung.items()}
            part = ft.assemble_arrays(tf, cut, r_cut, rung_cut, start)
            jj = part['t'].size - 1
            for col, v in full.items():
                if not _same(v[j], part[col][jj]):
                    bad += 1
                    print(f'         {tf} row {j} column {col}: {v[j]} vs {part[col][jj]}')
        check(f'{tf}: every feature column unchanged when later history is removed', bad == 0,
              f'{len(picks)} rows x {len(full)} columns')

    # ------------------------------------------------------------------ 4 --- #
    section('4. MTF alignment = the engine\'s own functions')
    from server.engine import legs as lgs
    from server.engine import regime as rgm
    F5 = ft.assemble(SYM, '5m', ms('2025-06-01 00:00'), ms('2025-06-15 00:00'))
    bad = 0
    for j in np.random.default_rng(9).choice(F5['t'].size, 40, replace=False):
        reads_d = {}
        for name in ['5m'] + ft.rungs('5m'):
            q = int(F5['qt'][j] if name == '5m' else F5[f'qt_{name}'][j])
            if q == rd.QT_MISSING:
                continue
            reads_d[name] = {'state': 'trending up' if q > 0 else 'trending down' if q < 0
                             else 'ranging',
                             'strength': {3: 'HIGH', 2: 'NORMAL', 1: 'WEAK'}.get(abs(q), 'NORMAL')}
        m = rgm.mtf_alignment(reads_d, '5m')
        hd, _ = lgs.htf_trend(m, '5m')
        bad += (m['score'] != int(F5['mtf_score'][j]) or abs(m['agreement'] - F5['mtf_agree'][j]) >
                0.006 or hd != int(F5['htf_dir'][j]))
    check('score, agreement and htf direction on 40 rows', bad == 0, f'{bad} differ')

    # ------------------------------------------------------------------ 5 --- #
    section('5. labels')
    O = np.array([[100.0, 100.0]])
    # up minute (open 100 -> low 99 -> high 102 -> close 101): the low comes first
    up_m = (O, np.array([[102.0, 102.0]]), np.array([[99.0, 99.0]]), np.array([[101.0, 101.0]]))
    tp = lb.first_touch(*up_m, np.array([[101.5]]), True)
    sl = lb.first_touch(*up_m, np.array([[99.5]]), False)
    check('up minute: low (phase 1) before high (phase 2)', int(sl[0]) == 1 and int(tp[0]) == 2,
          f'sl {int(sl[0])} tp {int(tp[0])}')
    dn_m = (O, np.array([[102.0, 102.0]]), np.array([[99.0, 99.0]]), np.array([[99.5, 99.5]]))
    tp = lb.first_touch(*dn_m, np.array([[101.5]]), True)
    sl = lb.first_touch(*dn_m, np.array([[99.5]]), False)
    check('down minute: high (phase 1) before low (phase 2)', int(tp[0]) == 1 and int(sl[0]) == 2)
    gap = lb.first_touch(np.array([[100.0, 103.0]]), np.array([[100.5, 104.0]]),
                         np.array([[99.8, 102.5]]), np.array([[100.2, 103.5]]),
                         np.array([[102.0]]), True)
    check('a level the open is already beyond fills at the open (phase 0)', int(gap[0]) == 4,
          f'{int(gap[0])} = minute 1, phase 0')
    miss = lb.first_touch(*up_m, np.array([[110.0]]), True)
    check('never reached -> NEVER', int(miss[0]) == int(lb.NEVER))

    from server.datafeed import Series
    t = np.arange(20, dtype=np.float64) * 300_000 + ms('2025-01-06 01:00')
    c = 100 + np.arange(20, dtype=np.float64)
    syn = Series(symbol='X', tf='5m', t=t, o=c - 0.5, h=c + 1.0, l=c - 1.0, c=c,
                 v=np.ones(20), spread=np.full(20, 10.0))
    Ms = lb.market_arrays(syn, np.full(20, 2.0), np.zeros(20, dtype=np.int8), '5m', int(t[0]))
    check('market labels: up excursion after 1 and 12 bars (ATR 2)',
          abs(Ms['up'][0, 0] - 1.0) < 1e-6 and abs(Ms['up'][0, 11] - 6.5) < 1e-6,
          f"{Ms['up'][0, 0]:.2f}, {Ms['up'][0, 11]:.2f} ATR")
    check('market labels: direction and incomplete tail',
          Ms['ret'][0, 0] > 0 and not Ms['complete'][-1] and Ms['complete'][0])
    # A decision closed before the first minute on disk has no path to walk: it is
    # left out, never walked on the minutes that come long after it.
    t5 = np.arange(40, dtype=np.float64) * 300_000 + ms('2025-01-06 01:00')
    c5 = np.full(40, 100.0)
    s5 = Series(symbol='X', tf='5m', t=t5, o=c5, h=c5 + 1.0, l=c5 - 1.0, c=c5,
                v=np.ones(40), spread=np.full(40, 10.0))
    t1m = np.arange(2000, dtype=np.float64) * 60_000 + ms('2025-01-06 02:00')
    c1m = np.full(2000, 100.0)
    m1s = Series(symbol='X', tf='1m', t=t1m, o=c1m, h=c1m + 0.5, l=c1m - 0.5, c=c1m,
                 v=np.ones(2000), spread=np.full(2000, 10.0))
    TLs = lb.trade_arrays(s5, np.full(40, 2.0), m1s, '5m', int(t5[0]), 0.01, horizon=60, digits=2)
    check('trade labels: decisions before the first 1m bar on disk are left out',
          TLs['close_ms'].size == 29 and int(TLs['close_ms'].min()) >= int(t1m[0]),
          f"{TLs['close_ms'].size} of 40 kept (11 closed before the 1m history starts)")

    # Trade labels vs the lab broker filling the same trades minute by minute.
    from server.lab.broker import REASON_SL, REASON_TP, SimBroker
    s15 = load_disk(SYM, '15m', [2025])
    r15 = rd.load(SYM, '15m', [2025])
    atr15 = rd.aligned(s15.t, r15, 'atr', np.nan)
    m1 = load_disk(SYM, '1m', [2025])
    lab_spec = {'point': 0.01, 'tick_size': 0.01, 'tick_value': 1.0, 'digits': 2,
                'commission_per_lot_side': 0.0}
    TL = lb.trade_arrays(s15, atr15, m1, '15m', ms('2025-02-01 00:00'), 0.01, digits=2)
    # 50 random decisions plus 10 that straddle the daily break or a weekend,
    # where the lab acts on the bar's close instead of the next minute's open.
    g21 = np.random.default_rng(21)
    far = np.nonzero(m1.t[np.searchsorted(m1.t, TL['close_ms'])] >= TL['close_ms'] + 900_000)[0]
    far = far[far < TL['t'].size - 2000]
    picks = np.concatenate([g21.choice(TL['t'].size - 2000, 50, replace=False),
                            g21.choice(far, min(10, far.size), replace=False)])
    bad, n = 0, 0
    for j in picks:
        k15 = int(np.searchsorted(s15.t, TL['t'][j]))
        i0 = int(np.searchsorted(m1.t, TL['close_ms'][j]))
        sp_dec = float(s15.spread[k15]) or 20.0
        for ti, (side, sl_atr, tp_r) in enumerate(TEMPLATES):
            b = SimBroker(SYM, lab_spec, 100_000.0, slippage_points=3.0, clock=lambda: 0)
            # the quote the lab acts on: the next minute's open, or the bar's close
            # when that minute is more than a bar away (daily break, weekend)
            nxt = float(m1.o[i0]) if int(m1.t[i0]) < int(TL['close_ms'][j]) + 900_000                 else float(s15.c[k15])
            b.set_quote(nxt, sp_dec)
            risk = sl_atr * float(atr15[k15])
            e = b.ask + b.slip if side == 'buy' else b.bid - b.slip
            sgn = 1 if side == 'buy' else -1
            _, res = b.trade('/order/send', side=side, lots=1.0, kind='market',
                             sl=round(e - sgn * risk, 2), tp=round(e + sgn * tp_r * risk, 2))
            closed = []
            b.on_close = lambda p, d: closed.append(d['reason'])
            for m in range(i0, i0 + TRADE_HORIZON):
                spm = float(m1.spread[m]) or sp_dec
                b.run_minute(int(m1.t[m]), float(m1.o[m]), float(m1.h[m]), float(m1.l[m]),
                             float(m1.c[m]), spm)
                if closed:
                    break
            got = 1 if closed and closed[0] == REASON_TP else -1 if closed and \
                closed[0] == REASON_SL else 0
            n += 1
            if not res.get('ok') or got != int(TL['tpl'][j, ti]):
                bad += 1
    check('template outcomes identical to SimBroker filling each trade', bad == 0,
          f'{n} trades ({len(picks)} decisions x {len(TEMPLATES)} templates), {bad} differ')
    far_ = np.abs(TL['raw_up'] - 1.0) > 1e-4           # skip float ties at the level itself
    check('raw excursion and the first-touch grid agree (1 ATR reached iff up >= 1 ATR)',
          bool(np.array_equal((TL['raw_up'] >= 1.0)[far_], (TL['up_tau'][:, 3] != lb.NEVER)[far_])))
    check('buy MFE = raw up excursion - spread - slippage (all in ATR)',
          bool(np.allclose(TL['buy_mfe'], TL['raw_up'] - TL['spread_atr'] - TL['slip_atr'],
                           atol=1e-4)))
    # The baseline's replay: the same trades at constant spreads, as SimBroker fills them.
    from server.forecast import COST_LEVELS
    bad, n = 0, 0
    for j in picks[:15]:
        k15 = int(np.searchsorted(s15.t, TL['t'][j]))
        i0 = int(np.searchsorted(m1.t, TL['close_ms'][j]))
        for ci in (0, 4, 8):
            c_pts = COST_LEVELS[ci] * float(atr15[k15]) / 0.01
            for ti, (side, sl_atr, tp_r) in enumerate(TEMPLATES):
                b = SimBroker(SYM, lab_spec, 100_000.0, slippage_points=3.0, clock=lambda: 0)
                b.set_quote(float(TL['entry_bid'][j]), c_pts)
                risk = sl_atr * float(atr15[k15])
                e = b.ask + b.slip if side == 'buy' else b.bid - b.slip
                sgn = 1 if side == 'buy' else -1
                _, res = b.trade('/order/send', side=side, lots=1.0, kind='market',
                                 sl=round(e - sgn * risk, 2), tp=round(e + sgn * tp_r * risk, 2))
                closed = []
                b.on_close = lambda p, d: closed.append(d['reason'])
                for m in range(i0, i0 + TRADE_HORIZON):
                    b.run_minute(int(m1.t[m]), float(m1.o[m]), float(m1.h[m]), float(m1.l[m]),
                                 float(m1.c[m]), c_pts)
                    if closed:
                        break
                got = 1 if closed and closed[0] == REASON_TP else -1 if closed and \
                    closed[0] == REASON_SL else 0
                n += 1
                bad += (not res.get('ok')) or got != int(TL['tpl_cf'][j, ti, ci])
    check('outcomes replayed at a constant cost identical to SimBroker at that spread',
          bad == 0, f'{n} replayed trades, {bad} differ')
    rate = [float(np.mean(TL['tpl_cf'][:, 0, ci] == 1)) for ci in range(len(COST_LEVELS))]
    check('dearer trades reach the target first less often (buy 1:1, by cost level)',
          all(a >= b for a, b in zip(rate, rate[1:])), ' '.join(f'{x:.3f}' for x in rate))

    # ------------------------------------------------------------------ 6 --- #
    section('6. baselines: resolved-only, weighted, quantiles')
    D0 = ms('2025-01-01 00:00')
    days = bl.Days(D0, D0 + 3 * bl.DAY)
    res_ms = np.array([D0 + 10 * 3_600_000, D0 + bl.DAY, D0 + bl.DAY + 1])
    y = np.array([1.0, 0.0, 1.0])
    p = bl.binary(days, days.known_from(res_ms), y, 1.0)
    check('day 0 knows nothing; day 1 knows what resolved by its start; day 2 the rest',
          np.isnan(p[0]) and abs(p[1] - 0.5) < 1e-12 and abs(p[2] - 2 / 3) < 1e-12,
          f'{p[0]}, {p[1]:.3f}, {p[2]:.3f}')
    p_hl = bl.binary(days, days.known_from(np.array([D0, D0 + bl.DAY])), np.array([1.0, 0.0]),
                     bl.decay_factor(1))
    check('recency: a one-day half-life weighs yesterday half as much',
          abs(p_hl[1] - (0.5 / 1.5)) < 1e-12, f'{p_hl[1]:.4f}')
    xq = np.random.default_rng(2).gamma(2.0, 0.8, 20000)
    qd = bl.Days(D0, D0)
    qq = bl.quantiles(qd, np.zeros(xq.size, dtype=np.int64), xq, 1.0)[0]
    ref = np.quantile(xq, bl.QS)
    check('histogram quantiles match numpy within a bin', bool(np.all(np.abs(qq - ref) <= bl.BIN)),
          f'{np.round(qq, 3)} vs {np.round(ref, 3)}')
    cn = bl.counts(days, days.known_from(res_ms), 1.0)
    check('counts and effective n', cn['n'][2] == 3 and abs(cn['n_eff'][2] - 3) < 1e-9)
    from server.forecast import COST_LEVELS as CL
    K, TT = len(CL), len(TEMPLATES)
    cf = np.zeros((1, K, TT, 3))
    cf[0, :, :, 0] = np.linspace(0.6, 0.4, K)[:, None]          # P(target) falls with cost
    cf[0, :, :, 1] = 1 - cf[0, :, :, 0]
    mid = (CL[2] + CL[3]) / 2
    got = bl.pick_tpl({'p_tpl_cf': cf}, np.array([0]), np.array([mid]))[0, 0, 0]
    check('a cost between two replay levels interpolates between their odds',
          abs(got - (cf[0, 2, 0, 0] + cf[0, 3, 0, 0]) / 2) < 1e-12, f'{got:.4f}')
    xs = np.random.default_rng(4).gamma(2.0, 1.0, 50_000)
    h = bl._hist(qd, np.zeros(xs.size, dtype=np.int64), xs, 1.0)
    tabs = {'h_up': h, 'c_up': np.cumsum(h, axis=1)}
    thr = np.array([0.5, 1.0, 1.73, 3.0])
    sv = bl.survival(tabs, 'up', np.zeros(4, dtype=np.int64), thr)
    direct = np.array([np.mean(xs >= t) for t in thr])
    check('survival read from the histogram matches direct counting',
          bool(np.all(np.abs(sv - direct) < 0.005)), f'{np.round(sv, 3)} vs {np.round(direct, 3)}')

    # ------------------------------------------------------------------ 7 --- #
    section('7. scoring rules')
    yy = np.array([0, 1, 1, 0, 1], dtype=float)
    check('Brier of 50% is 0.25', abs(sc.brier(np.full(5, 0.5), yy).mean() - 0.25) < 1e-12)
    check('log loss of 50% is ln 2', abs(sc.log_loss(np.full(5, 0.5), yy).mean() - np.log(2))
          < 1e-12)
    pp = np.repeat([0.2, 0.7], 1000)
    yc = np.concatenate([np.r_[np.ones(200), np.zeros(800)], np.r_[np.ones(700), np.zeros(300)]])
    check('ECE of a calibrated forecast is 0', sc.ece(pp, yc) < 1e-12)
    check('AUC: perfect 1, constant 0.5', abs(sc.auc(np.arange(10.0), np.r_[np.zeros(5),
          np.ones(5)]) - 1) < 1e-12 and abs(sc.auc(np.ones(10), np.r_[np.zeros(5),
          np.ones(5)]) - 0.5) < 1e-12)
    check('pinball loss', abs(sc.pinball(1.0, 3.0, 0.8) - 1.6) < 1e-12 and
          abs(sc.pinball(1.0, 0.0, 0.8) - 0.2) < 1e-12)
    P3 = np.array([[0.7, 0.2, 0.1]])
    check('multiclass Brier and log loss', abs(sc.brier(P3, [0])[0] - (0.09 + 0.04 + 0.01))
          < 1e-12 and abs(sc.log_loss(P3, [0])[0] + np.log(0.7)) < 1e-12)
    check('skill: half the loss is +50%', abs(sc.skill(np.ones(4), np.full(4, 2.0)) - 0.5) < 1e-12)
    check('n_eff counts overlapping outcomes once', sc.n_eff(1200, 12) == 100)

    # ------------------------------------------------------------------ 8 --- #
    section('8. news: scheduled times only')
    ev = {'major': (np.array([ms('2025-03-07 13:30'), ms('2025-03-12 12:30')]),
                    np.array([0, 1], dtype=np.int8)),
          'minor': (np.array([ms('2025-03-06 13:30')]), np.array([7], dtype=np.int8))}
    f = nw.features([ms('2025-03-07 13:00'), ms('2025-03-07 13:30'), ms('2025-03-07 14:00')],
                    60 * 60_000, TRADE_HORIZON * 60_000, ev)
    check('30 minutes before NFP: due in 30, inside a 1h horizon', f['news_next_min'][0] == 30
          and f['news_in_h'][0] == 1 and nw.KINDS[f['news_next_kind'][0]] == 'NFP')
    check('at the release minute it is the PAST one (known, not "due")',
          f['news_prev_min'][1] == 0 and f['news_in_h'][1] == 0)
    check('30 minutes after: 30 since, nothing due within the cap',
          f['news_prev_min'][2] == 30 and f['news_next_kind'][2] == -1)
    real = nw.events()
    check('history file loaded', real['n'] > 1000, f"{real['n']} scheduled releases")

    # ------------------------------------------------------------------ 9 --- #
    section('9. batch determinism and separation')
    from server.forecast.batch import Run
    a = Run(tfs=('4h',), trade_tfs=(), eval_years=(2025,), reps=60, run_id='_test_a',
            log=lambda m: None)
    a.market_tf('4h', keep_rows=False)
    b = Run(tfs=('4h',), trade_tfs=(), eval_years=(2025,), reps=60, run_id='_test_b',
            log=lambda m: None)
    b.market_tf('4h', keep_rows=False)
    import json as _json
    same = _json.dumps(a.rows, sort_keys=True, default=str) == \
        _json.dumps(b.rows, sort_keys=True, default=str)
    check('two runs give identical scorecards', same, f'{len(a.rows)} rows')
    st = [r for r in a.rows if r['output'] == 'state' and r['forecaster'] == 'state_transition'
          and r['year'] == '2025']
    check('the reference forecaster (regime persistence) shows positive skill',
          bool(st) and st[0]['skill'] > 0, f"{st[0]['skill']:+.3f}" if st else 'missing')
    check('the forecast engine never imported the live API', 'server.main' not in sys.modules)

    # ----------------------------------------------------------------- 10 --- #
    section('10. Phase B: conditional tables, the range model, barrier odds, the lab')
    from server.forecast import COST_LEVELS
    from server.forecast import range_model as rmod
    from server.forecast import store as fst
    from server.forecast.tables import Months, _hist, quantile_table
    mo = Months(ms('2025-01-01 00:00'), ms('2025-03-15 00:00'))
    lin = np.array([[0, 0], [0, 1]])                    # all rows -> two cells
    fine = np.r_[np.zeros(4000, dtype=np.int64), np.ones(4000, dtype=np.int64)]
    xs = np.r_[np.full(4000, 1.0), np.full(4000, 3.0)]
    res = np.full(8000, ms('2025-01-15 00:00'))         # all resolve mid-January
    tq = quantile_table(mo, mo.known_from(res), lin, fine, xs, 1.0, 1, (0.2, 0.5, 0.8))
    check('a monthly table knows nothing in the month its outcomes resolve',
          bool(np.isnan(tq['q'][0]).all()), 'January snapshot empty')
    check('...and each cell speaks for itself once it is well sampled',
          abs(float(tq['q'][1, 0, 1]) - 1.0) < 0.06 and abs(float(tq['q'][1, 1, 1]) - 3.0) < 0.06,
          f"P50 {float(tq['q'][1, 0, 1]):.2f} / {float(tq['q'][1, 1, 1]):.2f}")
    thin = np.r_[np.zeros(4000, dtype=np.int64), np.ones(10, dtype=np.int64)]
    tq2 = quantile_table(mo, mo.known_from(np.full(4010, ms('2025-01-15 00:00'))), lin, thin,
                         np.r_[np.full(4000, 1.0), np.full(10, 3.0)], 10.0, 1, (0.2, 0.5, 0.8))
    check('a thin cell is shrunk to its parent: weight n_eff / (n_eff + k)',
          abs(float(tq2['w'][1, 1]) - 10 / 20) < 1e-6 and float(tq2['q'][1, 1, 0]) < 1.5,
          f"w {float(tq2['w'][1, 1]):.2f}, P20 {float(tq2['q'][1, 1, 0]):.2f} "
          f"(the cell alone says 3.00, its parent 1.00)")
    h = _hist(mo, np.array([0]), np.array([0]), 1, np.array([1.0]), half_life_months=1)
    check('a one-month half-life halves last month', abs(h[1].sum() - 0.5) < 1e-12)
    cell = int(rmod.cells([ms('2025-03-05 15:00')], [1], [1.3], '15m')[0])
    d = rmod.decode(cell)
    check('range cells decode back to hour, news and volatility bucket',
          d['hour'] == 15 and d['news'] == 1 and d['vol'] == 3, str(d))

    # Barrier odds from the grid vs the exact replayed outcomes (same month).
    st15 = fst.load(SYM, '15m')
    T15 = lb.trade(SYM, '15m')
    tm = Months(int(T15['close_ms'][0]), int(T15['close_ms'][-1]))
    m_i = int(tm.of([ms('2025-06-10 12:00')])[0])
    known = tm.known_from(T15['resolve_ms']) <= m_i
    ci = 3
    exact = float(np.mean(T15['tpl_cf'][known, 0, ci] == 1))
    slip = float(np.median(T15['slip_atr'][known]))
    grid = st15.barrier(ms('2025-06-10 12:00'), 'buy', 1.0, 1.0, COST_LEVELS[ci], slip)
    check('grid barrier odds agree with the exact replay (buy 1 ATR / 1R at 0.03 ATR spread)',
          abs(grid['target'] - exact) < 0.02, f"grid {grid['target']:.3f} vs exact {exact:.3f}")

    # The lab: no look-ahead in the forecast payload, and settling = the labels.
    s_short = ReplaySession({'symbol': SYM, 'tf': '15m', 'start': '2025-05-05T00:00',
                             'end': '2025-05-07T00:00', 'record': False})
    s_long = ReplaySession({'symbol': SYM, 'tf': '15m', 'start': '2025-05-05T00:00',
                            'end': '2025-05-30T00:00', 'record': False})
    n = s_short.last - s_short.i0
    s_short.advance(n)
    s_long.advance(n)
    s_short.set_view(s_short.k)
    fa = s_short.view_payload()['forecast']
    s_long.set_view(s_short.k)
    fb = s_long.view_payload()['forecast']
    import json as _j
    check('the forecast at a bar ignores every bar after it (short vs long replay)',
          fa.get('available') and _j.dumps(fa, sort_keys=True) == _j.dumps(fb, sort_keys=True),
          f"{fa.get('tf')} at {fa.get('t')}, {len(fa.get('mtf') or [])} MTF rows, "
          f"{len(fa.get('trades') or [])} trade odds")
    M15 = lb.market(SYM, '15m')
    rows = s_short.fc.rows(1000)
    bad = 0
    for rec in rows:
        j = int(np.searchsorted(M15['t'], rec['t']))
        bad += abs(M15['up'][j, -1] - rec['up']) > 2e-3 or abs(M15['dn'][j, -1] - rec['dn']) > 2e-3
    check('each settled forecast scored against the same outcome the batch labels hold',
          bool(rows) and bad == 0, f'{len(rows)} settled, {bad} differ')
    st_ = s_short.fc.stats()
    check('session scoring adds up', st_['n'] == len(rows) and 0 <= st_['cov_up'] <= 1)
    check('the lab still never imported the live API', 'server.main' not in sys.modules)

    # Gates are per symbol: a symbol never scored has none, and never borrows gold's.
    import json as _json
    import shutil as _shutil
    import tempfile as _tempfile
    from server import forecast as fpkg
    from server.lab import forecast as lab_fc
    gold_run = fpkg.latest_run(SYM)
    check("the lab reads each symbol's own gates - none for one never scored",
          fa.get('symbol') == SYM and lab_fc.summary('NOPE.a') == {}
          and lab_fc._gate('15m', 'NOPE.a') is None and lab_fc.news_summary('NOPE.a') == {}
          and (gold_run is None or lab_fc.summary(SYM).get('run_id') == gold_run.name),
          f"gold's newest run {gold_run.name if gold_run else None}")
    tmpb = Path(_tempfile.mkdtemp())
    real_batch = fpkg.BATCH_DIR
    try:
        for name, sym in (('20990101-000000', 'USDJPY.a'), ('20980101-000000', None),
                          ('20970101-000000', 'XAUUSD.a')):
            (tmpb / name).mkdir()
            (tmpb / name / 'ui_summary.json').write_text('{}', encoding='utf-8')
            if sym:
                (tmpb / name / 'manifest.json').write_text(_json.dumps({'symbol': sym}),
                                                           encoding='utf-8')
        fpkg.BATCH_DIR = tmpb
        check("each symbol's newest batch run is its own - a run with no symbol is gold's",
              fpkg.latest_run('USDJPY.a').name == '20990101-000000'
              and fpkg.latest_run('XAUUSD.a').name == '20980101-000000'
              and fpkg.latest_run('EURUSD.a') is None)
    finally:
        fpkg.BATCH_DIR = real_batch
        _shutil.rmtree(tmpb, ignore_errors=True)

    # ----------------------------------------------------------------- 11 --- #
    section('11. Phase C: lift tables, confidence, analogs')
    from server.forecast import cond_model as cmod
    from server.forecast.tables import lift_table
    from server.lab.forecast import confidence
    lin2 = np.array([[0, 0], [0, 1]])
    fc2 = np.r_[np.zeros(3000, dtype=np.int64), np.ones(3000, dtype=np.int64)]
    res2 = np.r_[np.full(3000, 0.05), np.full(3000, -0.05)]
    kn = mo.known_from(np.full(6000, ms('2025-01-15 00:00')))
    lt = lift_table(mo, kn, lin2, fc2, res2, 10.0, 1)
    check('a lift table knows nothing in the month its outcomes resolve',
          bool(np.all(lt['lift'][0] == 0)) and bool(np.all(lt['w'][0] == 0)))
    check('...then each well-sampled cell carries its own lift',
          abs(float(lt['lift'][1, 0, 0]) - 0.05) < 1e-3 and abs(float(lt['lift'][1, 1, 0]) + 0.05) < 1e-3,
          f"{float(lt['lift'][1, 0, 0]):+.3f} / {float(lt['lift'][1, 1, 0]):+.3f}")
    fc3 = np.r_[np.zeros(3000, dtype=np.int64), np.ones(10, dtype=np.int64)]
    lt3 = lift_table(mo, mo.known_from(np.full(3010, ms('2025-01-15 00:00'))), lin2, fc3,
                     np.r_[np.full(3000, 0.0), np.full(10, 0.2)], 10.0, 1)
    check('a thin cell keeps only n_eff / (n_eff + k) of its own lift',
          abs(float(lt3['lift'][1, 1, 0]) - 0.5 * 0.2 - 0.5 * float(lt3['lift'][1, 0, 0])) < 1e-3,
          f"{float(lt3['lift'][1, 1, 0]):+.3f} (its own +0.200, weight 0.50)")
    pm = cmod._apply(np.array([[0.2, 0.3, 0.5]]), np.array([[0.5, -0.4, 0.0]]))
    check('baseline + lift stays a probability', abs(float(pm.sum()) - 1) < 1e-9 and float(pm.min()) > 0)
    P0 = {'regime': np.array([1]), 'mom': np.array([3]), 'legpull': np.array([4]),
          'vol': np.array([2]), 'session': np.array([5])}
    ok = True
    for name in ('direction', 'state', 'trade'):
        c = int(cmod.cells(name, P0)[0])
        lin_ = cmod.lineage(name)
        ok &= bool(lin_[-1, c] == c and lin_[0, c] == 0 and len(cmod.describe(name, c)) == 3)
    sc_ = int(cmod.cells('state', P0)[0])
    check('cells, lineage and descriptions agree',
          ok and cmod.describe('state', sc_) == ['downtrend', 'rising', 'normal vol'],
          ', '.join(cmod.describe('state', sc_)))
    check('confidence: interval over the baseline -> baseline',
          confidence(0.52, 0.49, 0.55, 0.51, 900, True) == 'baseline')
    check('confidence: clear of it, big cell, promoted -> high',
          confidence(0.60, 0.56, 0.64, 0.51, 900, True) == 'high')
    check('confidence: clear of it but a small cell or not promoted -> moderate',
          confidence(0.60, 0.56, 0.64, 0.51, 90, True) == 'moderate'
          and confidence(0.60, 0.56, 0.64, 0.51, 900, False) == 'moderate')
    i_ = st15.row_at(int(s_short.series.t[s_short.k]))
    an = st15.analogs(i_, limit=50)
    rows_ok = all(r['close_ms'] <= int(st15.close_ms[i_]) for r in an['rows'])
    res_ok = all(int(st15.a['resolve_ms'][st15.row_at(r['t'])]) <= int(st15.close_ms[i_]) for r in an['rows'])
    ts_ = sorted(st15.row_at(r['t']) for r in an['rows'])
    spaced = all(b - a >= st15.H for a, b in zip(ts_, ts_[1:]))
    check('similar moments: all resolved before this bar, thinned to one per horizon',
          bool(an['rows']) and rows_ok and res_ok and spaced,
          f"{an['n']} matches, {an['n_eff']} independent")
    rg = fa.get('regime') or {}
    check('the regime forecast travels with its gate and confidence',
          rg.get('confidence') in ('high', 'moderate', 'baseline') and 'promoted' in rg)

    # ----------------------------------------------------------------- 12 --- #
    section('12. Phase D: forecast filters')
    from server.lab.filters import verdict as fverdict
    a15 = st15.a
    rows_ = np.arange(a15['t'].size - 60000, a15['t'].size - 1000, 97)
    dn_row = next(i for i in rows_ if (st15.regime(int(i)) or {}).get('p', [0, 0])[1] >
                  (st15.regime(int(i)) or {}).get('p', [1, 0])[0])
    t_dn = int(a15['t'][dn_row])
    check('regime_agree blocks a buy when a downtrend is the likelier trend, lets a sell through',
          fverdict('regime_agree', SYM, '15m', t_dn, 'buy') is not None
          and fverdict('regime_agree', SYM, '15m', t_dn, 'sell') is None)
    quiet = next(i for i in rows_ if (st15.cone(int(i)) or {}).get('ratio', 1) < 1.0)
    check('range_active blocks a quieter-than-usual horizon, both sides',
          fverdict('range_active', SYM, '15m', int(a15['t'][quiet]), 'buy') is not None
          and fverdict('range_active', SYM, '15m', int(a15['t'][quiet]), 'sell') is not None)
    check('no filter, no veto', fverdict(None, SYM, '15m', t_dn, 'buy') is None)
    try:
        ReplaySession({'symbol': SYM, 'tf': '15m', 'start': '2025-05-05T00:00',
                       'end': '2025-05-07T00:00', 'record': False, 'forecast_filter': 'bogus'})
        bad_cfg = False
    except ValueError:
        bad_cfg = True
    check('an unknown filter name is refused', bad_cfg)
    sf = ReplaySession({'symbol': SYM, 'tf': '15m', 'start': '2025-03-03T00:00',
                        'end': '2025-03-20T00:00', 'record': False, 'mode': 'auto',
                        'forecast_filter': 'regime_agree'})
    sf.advance(sf.last - sf.k)
    vetoes = [e for e in sf.events if 'forecast filter' in str(e.get('text', ''))]
    check('a filtered replay runs and records every veto in its journal', bool(vetoes),
          f'{len(vetoes)} vetoes, {len(sf.trades)} trades')
    from server.lab import filtertest as FT
    from server.lab import settings as lab_settings
    fz = FT.frozen_settings()
    moved = {g: dict(v) for g, v in fz.items()}
    moved['risk']['max_daily_trades'] = int(fz['risk']['max_daily_trades']) + 7
    real_live = lab_settings.live_saved
    lab_settings.live_saved = lambda: moved         # a save in the live app mid-run
    try:
        sz = ReplaySession(FT._cfg(SYM, '15m', 2025, end_ms=1_736_467_200_000, settings=fz))
        held = lab_settings.jsonable(sz.eff) == fz
    finally:
        lab_settings.live_saved = real_live
    check('a filter test keeps its frozen settings when the live ones change mid-run', held,
          f'settings {FT.settings_hash(fz)}')

    # ----------------------------------------------------------------- 13 --- #
    section('13. Phase E: the news model and the challenge')
    from server.forecast import news_model as nmod
    s15 = nmod.state(F15, '15m')
    ahead = np.asarray(F15['news_in_h']) > 0
    check('a major release ahead is exactly news states 2-6 - the Phase B flag, refined',
          bool(np.array_equal(ahead, (s15 >= 2) & (s15 <= 6))),
          f'{int(ahead.sum())} rows with a release ahead, states {sorted(set(s15.tolist()))}')
    c_new = nmod.cells(F15['close_ms'], s15, F15['rr_w'], '15m')
    c_old = rmod.cells(F15['close_ms'], F15['news_in_h'], F15['rr_w'], '15m')
    check('every news cell shrinks first to the Phase B cell it refines',
          bool(np.array_equal(nmod.lineage()[2][c_new], rmod.lineage()[2][c_old])))
    check('...and its quiet twin is the same hour and volatility with no release',
          bool(np.all(nmod.quiet_twin(c_new) // 5 % nmod.N_STATES == 0)
               and np.all(nmod.quiet_twin(c_new) % 5 == c_new % 5)))
    from server.lab import challenge as chal
    hit = None
    for fi in range(len(sf.frames) - 1, -1, -1):
        if any(x.get('status') == 'qualified' or x.get('stage') in ('SENT', 'FILLED')
               for x in sf.frames[fi]['signals']):
            hit = fi
            break
    check('the filtered replay has a bar with a qualified or live signal to challenge',
          hit is not None)
    if hit is not None:
        sf.set_view(sf.i0 + hit)
        fr = sf.frames[hit]
        pack = chal.build(sf.fc.payload(sf, sf.v, fr), fr, 2)
        rules = chal.deterministic(pack)
        check('the fact sheet names the signal, its stop in ATR and the invalidation levels',
              pack['subject'] is not None
              and any(f.startswith('Signal under review') for f in pack['facts'])
              and any(f.startswith('Where it stops being true') for f in pack['facts']),
              f"{len(pack['facts'])} facts, {len(pack['checks'])} findings")
        check('the rule-written challenge uses no number the fact sheet lacks',
              not chal.untraced(rules, pack['facts'] + pack['checks']))
        check('a reply with an invented number is caught',
              chal.untraced(rules + '\n- a target of 1234.56 is likely', pack['facts']
                            + pack['checks']) == ['1234.56'])
        import asyncio
        from server.config import CONFIG as CFG
        import os
        keep = (CFG.anthropic_key, os.environ.pop('ANTHROPIC_API_KEY', None))
        CFG.anthropic_key = ''
        try:
            res = asyncio.run(chal.challenge(pack))
        finally:
            CFG.anthropic_key = keep[0]
            if keep[1] is not None:
                os.environ['ANTHROPIC_API_KEY'] = keep[1]
        check('without a model key the challenge is the engine\'s, and says so',
              res['source'] == 'engine' and res['text'] == rules and 'rules' in res['note'])

    # ----------------------------------------------------------------- 14 --- #
    section('14. Shadow live: regime_agree logged beside 5m sends, never used')
    import json as _json
    import tempfile
    import time as _time
    from server.forecast import HORIZON as HZ
    from server.forecast import rules as frules
    from server.forecast import shadow
    st5 = fst.load(SYM, '5m', allow_stale=True)
    s5 = ReplaySession({'symbol': SYM, 'tf': '5m', 'start': '2025-06-02T00:00',
                        'end': '2025-06-04T00:00', 'record': False})
    n_ok = n_all = 0
    for k in range(s5.i0, s5.last, 9):
        snap = s5._analyse(k, memo=False)
        i = st5.row_at(int(s5.series.t[k]))
        if not snap.get('ok') or i is None:
            continue
        mom = shadow.momentum(s5.series.c[k - HZ['5m']:k + 1], float(snap['atr']), '5m')
        fc = shadow.regime_forecast(snap, mom, '5m', SYM)
        ref = st5.regime(i)
        n_all += 1
        n_ok += int(fc['cell'] == int(st5.a['st_cell'][i])
                    and np.allclose(fc['p'], ref['p'], atol=1e-3))
    check('from a closed-bar analysis, the shadow reproduces the offline regime forecast',
          n_all > 20 and n_ok == n_all, f'{n_ok}/{n_all} bars, cell and probabilities')
    check('...and applies the very rule the filter test used',
          frules.regime_agree([0.2, 0.3, 0.3, 0.1, 0.1], 'buy') is not None
          and frules.regime_agree([0.2, 0.3, 0.3, 0.1, 0.1], 'sell') is None)
    k = s5.last - 1
    snap = s5._analyse(k, memo=False)
    real_log = shadow.LOG
    shadow.LOG = Path(tempfile.mkdtemp()) / 'shadow.jsonl'
    try:
        shadow.remember_closed(SYM, '5m', s5.series.slice(0, k + 1), snap)
        rec = {'id': 'test:5m:x', 'symbol': SYM, 'tf': '5m', 'side': 'buy', 'playbook': 'test',
               'final_bar_ms': int(s5.series.t[k])}
        shadow.note_send(rec, snap)
        shadow.note_send(dict(rec, tf='15m', id='test:15m:x'), snap)      # not shadowed
        shadow.note_send(None, None)                                      # garbage: no raise
        stale = dict(snap, bar_time_ms=int(snap['bar_time_ms']) - 300_000)
        shadow.note_send(dict(rec, id='test:5m:stale'), stale)
        for _ in range(100):
            if shadow.LOG.exists() and len(shadow.LOG.read_text(encoding='utf-8').splitlines()) >= 2:
                break
            _time.sleep(0.05)
        rows = [_json.loads(x) for x in shadow.LOG.read_text(encoding='utf-8').splitlines()] \
            if shadow.LOG.exists() else []
    finally:
        shadow.LOG = real_log
    ids = [r_.get('id') for r_ in rows]
    first = rows[0] if rows else {}
    check('a 5m send is logged with its verdict and the forecast behind it; 15m is not',
          ids[:1] == ['test:5m:x'] and 'test:15m:x' not in ids
          and first.get('verdict') in ('veto', 'pass') and 'forecast' in first,
          f"{first.get('verdict')} - {first.get('why') or 'no veto'}")
    check('a send judged on a different bar than the momentum abstains rather than guesses',
          any(r_.get('id') == 'test:5m:stale' and r_.get('verdict') == 'abstain' for r_ in rows))

    print(f'\n{PASS} passed, {FAIL} failed')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
