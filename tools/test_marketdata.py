#!/usr/bin/env python
"""
tools/test_marketdata.py - the history top-up, checked without MT5 or the bridge.

    1. tools/mt5_download.py merges into the year files: nothing duplicated,
       the forming bar left out, a corrected bar replaced, years split
    2. server/marketdata.py refuses what it should (no bridge, mock, open
       positions on a trading day without force), runs its steps in order and
       records the result
    3. the choice: chosen symbols and timeframes, the forecast alone, up to 30
       years for a new symbol; the forecast engine per symbol - switched on only
       for history that can carry it, built and rescored symbol by symbol
    4. the monthly routine: off does nothing; on, due and the weekend starts;
       a weekday or a recent update waits
    5. the readers see new bars: the disk cache and the lab's coverage follow
       the files' modification times

Run:  python tools/test_marketdata.py      (a few seconds; works on temp copies)
"""
from __future__ import annotations

import gzip
import shutil
import sys
import tempfile
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = '') -> None:
    global PASS, FAIL
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}]   {name}" + (f' - {detail}' if detail else ''))


def section(t: str) -> None:
    print(f'\n{t}\n' + '-' * len(t))


def fake_mt5(bars, tick_time):
    return types.SimpleNamespace(
        TIMEFRAME_M1=1, TIMEFRAME_M3=3, TIMEFRAME_M5=5, TIMEFRAME_M15=15, TIMEFRAME_M30=30,
        TIMEFRAME_H1=60, TIMEFRAME_H2=120, TIMEFRAME_H4=240, TIMEFRAME_D1=1440, TIMEFRAME_W1=10080,
        symbol_select=lambda s, on: True, symbol_info=lambda s: types.SimpleNamespace(digits=2),
        symbol_info_tick=lambda s: types.SimpleNamespace(time=tick_time),
        copy_rates_range=lambda s, tf, a, b: bars[(bars['time'] >= int(a.timestamp()))
                                                  & (bars['time'] < int(b.timestamp()))])


def main() -> int:
    import tools.mt5_download as dl
    tmp = Path(tempfile.mkdtemp())
    try:
        # ---------------------------------------------------------------- 1
        section('1. the download merges into the year files')
        src = ROOT / 'data' / 'XAUUSD.a' / '5m' / '2026.csv.gz'
        (tmp / 'XAUUSD.a' / '5m').mkdir(parents=True)
        shutil.copy(src, tmp / 'XAUUSD.a' / '5m' / '2026.csv.gz')
        dl.DATA = tmp
        path = tmp / 'XAUUSD.a' / '5m' / '2026.csv.gz'
        old = dl._read_rows(path)
        last = max(old)
        dt = np.dtype([('time', 'i8'), ('open', 'f8'), ('high', 'f8'), ('low', 'f8'),
                       ('close', 'f8'), ('tick_volume', 'i8'), ('spread', 'i4'),
                       ('real_volume', 'i8')])
        # MT5's answer: the disk's own last two days, then 30 new bars, the last forming
        ts = sorted([t for t in old if t > last - 2 * 86400]) + [last + 300 * k for k in range(1, 31)]
        bars = np.zeros(len(ts), dt)
        for i, t in enumerate(ts):
            if t in old:
                p = old[t].split(',')
                bars[i] = (t, float(p[1]), float(p[2]), float(p[3]), float(p[4]), int(p[5]),
                           int(p[7]), int(p[6]))
            else:
                bars[i] = (t, 4600.5, 4601.25, 4599.75, 4600.8, 120, 16, 0)
        sys.modules['MetaTrader5'] = fake_mt5(bars, ts[-1] + 60)
        n = dl.fetch_bars(['XAUUSD.a'], ['5m'], 20)
        rows = dl._read_rows(path)
        check('only the new closed bars are written, the overlap is not duplicated',
              n == 29 and len(rows) == len(old) + 29, f'{n} written')
        check('the bar still forming is left for the next update',
              ts[-1] not in rows and max(rows) == ts[-2])
        check('every existing row is untouched', all(rows[k] == v for k, v in old.items()))
        bars[-2]['close'] = 4611.0
        check('a bar MT5 corrected is replaced, once', dl.fetch_bars(['XAUUSD.a'], ['5m'], 20) == 1
              and dl._read_rows(path)[ts[-2]].split(',')[4] == '4611.0')
        (tmp / 'XAUUSD.a' / '15m').mkdir()
        dl.FLUSH_ROWS, real_flush = 7, dl.FLUSH_ROWS          # a first download, flushed often
        dl.fetch_bars(['XAUUSD.a'], ['15m'], 1)
        dl.FLUSH_ROWS = real_flush
        flushed = dl._read_rows(tmp / 'XAUUSD.a' / '15m' / '2026.csv.gz')
        check('a first download written in pieces holds every closed bar once',
              sorted(flushed) == [t for t in ts if t + 900 <= ts[-1] + 60], f'{len(flushed)} bars')
        shutil.rmtree(tmp / 'XAUUSD.a' / '15m')
        jan = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())
        dl.merge('XAUUSD.a', '5m', {jan: f'{jan},1,1,1,1,1,0,1'})
        check('a bar in a new year opens a new year file',
              (tmp / 'XAUUSD.a' / '5m' / '2027.csv.gz').exists() and jan not in dl._read_rows(path))
        (tmp / 'XAUUSD.a' / '5m' / '2027.csv.gz').unlink()

        # ---------------------------------------------------------------- 2
        section('2. the update job')
        import server.marketdata as md
        md.STATE_FILE = tmp / 'marketdata.json'
        md.LOG_DIR = tmp / 'logs'
        md.DATA = tmp
        (tmp / 'XAUUSD.a' / '1m').mkdir()

        class Bridge:
            def __init__(self, connected=True, mock=False, positions=(), orders=()):
                self.h = {'connected': connected, 'mock': mock}
                self.p, self.o = list(positions), list(orders)
                self.calls, self.polls, self.started = [], 0, False

            def health(self):
                return self.h

            def positions(self, strict=False):
                return self.p

            def orders(self, strict=False):
                return self.o

            def _get(self, path, **kw):
                self.calls.append((path, kw))
                if path == '/download/start':
                    self.started, self.polls = True, 0      # a fresh job per symbol
                    return {'ok': True}
                if path == '/download/status':
                    if not self.started:           # nothing running before a start
                        return {'running': False, 'phase': None, 'finished_ms': None}
                    self.polls += 1
                    done = self.polls > 2
                    return {'running': not done, 'phase': 'done' if done else 'bars',
                            'finished_ms': 1 if done else None, 'rows': 29, 'error': None,
                            'done': min(self.polls, 2), 'total': 2, 'tf': '5m', 'item': 'x',
                            'log': ['bars XAUUSD.a 5m 29 new or corrected bars (1/1)']}
                return {}

        check('refused while the bridge is not connected',
              not md.start(Bridge(connected=False))['ok'])
        check('refused on a mock bridge', not md.start(Bridge(mock=True))['ok'])
        sat = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
        mon = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
        real_dt = md.datetime
        md.datetime = types.SimpleNamespace(now=lambda tz=None: mon)
        r = md.start(Bridge(positions=[{'ticket': 1}]))
        md.datetime = real_dt
        check('on a trading day, with a position open, it asks to be forced rather than '
              'blocking it', not r['ok'] and r.get('needs_force'), r.get('error', '')[:60])
        steps, runs = [], []

        def fake_tool(name, argv, parse=None, final=True):
            steps.append(name)
            runs.append((name, list(argv)))
            if final:
                md._step(name, 'done')
            return not any(a in FAIL_FOR for a in argv)
        FAIL_FOR = set()
        md._subprocess = fake_tool
        # the job's own 2 s polls, skipped - on a copy, not the shared time module
        md.time = types.SimpleNamespace(**{k: getattr(time, k) for k in ('time', 'strftime')},
                                        sleep=lambda s: None)
        b = Bridge()
        r = md.start(b, rebuild=True, rescore=False, reason='manual')
        for _ in range(200):
            if not md._JOB['running']:
                break
            time.sleep(0.01)
        st = md.status()
        start_call = next(kw for p, kw in b.calls if p == '/download/start')
        check('the bridge is asked for every timeframe on disk by name - never an empty list',
              r['ok'] and set(start_call['tfs'].split(',')) == {'1m', '5m'}, start_call['tfs'])
        check('the steps run in order and the rescore stays off unless asked',
              steps == ['releases', 'forecast']
              and [s['state'] for s in st['job']['steps']] == ['done', 'done', 'done', 'skipped'],
              ' -> '.join(steps))
        check('a good run is recorded as the last update',
              st['routine']['last_ok_ms'] and st['history'][-1]['ok'] and not st['routine']['due'])
        check('the progress bar ends at 100%', st['job']['pct'] == 100.0, f"{st['job']['pct']}%")
        jl = md.job_status()
        check("the footer's view of the job: the same progress, without the log",
              jl['pct'] == st['job']['pct'] and jl['phase'] == st['job']['phase']
              and 'log' not in jl and st['job']['log'], f"{jl['pct']}% {jl['phase']}")

        def run_and_wait(bridge, **kw):
            r = md.start(bridge, **kw)
            for _ in range(300):
                if not md._JOB['running']:
                    break
                time.sleep(0.01)
            return r, [kw2 for p2, kw2 in bridge.calls if p2 == '/download/start']

        last_ok = md._load()['last_ok_ms']
        steps.clear()
        r, calls = run_and_wait(Bridge(), symbols=['EURUSD.a'], tfs=['1m', '5m', '1h'], years=3,
                                reason='add symbol')
        check('a new symbol gets the timeframes and years asked for',
              r['ok'] and r['new'] == ['EURUSD.a'] and calls[0]['tfs'] == '1m,5m,1h'
              and calls[0]['years'] == 3, str(calls[0]))
        check('...the forecast rebuild is skipped - its forecast engine is off',
              steps == ['releases'] and md.status()['job']['steps'][2]['state'] == 'skipped')
        check('...and it does not count as the monthly update', md._load()['last_ok_ms'] == last_ok)
        r, calls = run_and_wait(Bridge(), symbols=['XAUUSD.a', 'EURUSD.a'])
        check('one bridge job per symbol, each with its own timeframes',
              [c['symbols'] for c in calls] == ['XAUUSD.a', 'EURUSD.a']
              and set(calls[0]['tfs'].split(',')) == {'1m', '5m'}
              and len(calls[1]['tfs'].split(',')) == 10, f"{calls[0]['tfs']} | {calls[1]['tfs']}")
        check('a name that is not a symbol is refused (it becomes a folder)',
              not md.start(Bridge(), symbols=['../evil'])['ok'])
        bp = md._BuildProgress()
        fr = [x[0] for x in map(bp, ['reads: 4 timeframe-years to build on 2 workers',
                                    '  [ 1/4]  5m 2026  1 bars', '  [4/4] 4h 2026 1 bars',
                                    'labels: market 5m 1 rows', 'store:   5m 1 rows',
                                    'store:  15m cached']) if x]
        check("the rebuild's progress is read from its own output, and only rises",
              fr == sorted(fr) and 0 < fr[0] < 1 and len(fr) == 5, ' '.join(f'{x:.2f}' for x in fr))
        check('the overall percentage weighs each step and drops the skipped ones',
              md.overall([{'name': 'download', 'state': 'done', 'frac': 1.0},
                          {'name': 'releases', 'state': 'done', 'frac': 1.0},
                          {'name': 'forecast', 'state': 'skipped', 'frac': 0.0},
                          {'name': 'rescore', 'state': 'skipped', 'frac': 0.0}]) == 100.0
              and md.overall([{'name': 'download', 'state': 'done', 'frac': 1.0},
                              {'name': 'forecast', 'state': 'running', 'frac': 0.5}])
              == round(100 * (md.WEIGHTS['download'] + md.WEIGHTS['forecast'] / 2)
                       / (md.WEIGHTS['download'] + md.WEIGHTS['forecast']), 1))

        # ---------------------------------------------------------------- 3
        section('3. what is updated is a choice')
        md.datetime = types.SimpleNamespace(now=lambda tz=None: sat)
        r, calls = run_and_wait(Bridge(positions=[{'ticket': 1}]), symbols=['XAUUSD.a'])
        md.datetime = real_dt
        check('at the weekend open positions do not hold the download back - nothing is quoted',
              r['ok'] and len(calls) == 1, r.get('error', ''))
        last_ok = md._load()['last_ok_ms']
        r, calls = run_and_wait(Bridge(), symbols=['XAUUSD.a'], tfs=['5m'])
        check('a chosen symbol on chosen timeframes: only those are asked for',
              r['ok'] and [(c['symbols'], c['tfs']) for c in calls] == [('XAUUSD.a', '5m')],
              str(calls))
        check("...it is a selection, and not the monthly routine's full update",
              r['reason'] == 'selected' and md._load()['last_ok_ms'] == last_ok)
        everything = sorted(p.name for p in tmp.iterdir() if p.is_dir() and dl.disk_tfs(p.name))
        r, calls = run_and_wait(Bridge(), symbols=everything, tfs=list(dl.TF_SECONDS))
        check('every symbol on every timeframe, named explicitly, is still the full update',
              r['reason'] == 'manual' and md._load()['last_ok_ms'] > last_ok, r['reason'])
        check('...and once it stops, the last thing it was doing is still said',
              bool(md.status()['job']['detail']), md.status()['job']['detail'])
        last_ok = md._load()['last_ok_ms']
        r, calls = run_and_wait(Bridge(), symbols=everything, tfs=['5m'])
        check('...but not on some timeframes only',
              r['reason'] == 'selected' and md._load()['last_ok_ms'] == last_ok, r['reason'])
        r, calls = run_and_wait(Bridge(), symbols=['NEW.a'], years=30)
        r2, calls2 = run_and_wait(Bridge(), symbols=['NEW.a'], years=99)
        check('a new symbol can have 30 years of history, and no more',
              calls[0]['years'] == 30 and calls2[0]['years'] == 30 and md.MAX_YEARS == 30,
              f"{calls[0]['years']} / {calls2[0]['years']}")

        # the forecast engine per symbol
        def put(sym, tf, year, step, n=300):
            d = tmp / sym / tf
            d.mkdir(parents=True, exist_ok=True)
            t0 = int(datetime(year, 1, 2, tzinfo=timezone.utc).timestamp())
            with gzip.open(d / f'{year}.csv.gz', 'wt', encoding='utf-8') as fh:
                fh.write('ts,open,high,low,close,tick_volume,real_volume,spread\n')
                for k in range(n):
                    fh.write(f'{t0 + k * step},1,1,1,1,1,0,1\n')
        need, first = md.forecast_needs()
        check('the engine needs its timeframes, their MTF rungs and the 1m path, from 2018',
              {'1m', '5m', '15m', '1h', '4h'} <= set(need) and first == 2018, f'{need} {first}')
        check('it runs for gold alone until another symbol is switched on',
              md.forecast_symbols() == ['XAUUSD.a'])
        for tf in need:
            put('FX.a', tf, 2026, md.TF_SECONDS[tf])
        r = md.set_forecast('FX.a', True)
        check('switching it on is refused while the history is too short, and says why',
              not r['ok'] and 'from 2018' in r['error'] and 'FX.a' not in md.forecast_symbols(),
              r.get('error', '')[:90])
        for tf in need:
            put('FX.a', tf, 2018, 86400 if tf == '1m' else md.TF_SECONDS[tf])
        r = md.set_forecast('FX.a', True)
        check("...and while the first year's 1m file is MT5's coarser padding, not minutes",
              not r['ok'] and 'not minute bars' in r['error'], r.get('error', '')[:90])
        put('FX.a', '1m', 2018, 60)
        r = md.set_forecast('FX.a', True)
        st = md.status()
        check('with history from 2018 and real minutes it switches on',
              r.get('ok') and md.forecast_symbols() == ['XAUUSD.a', 'FX.a']
              and st['forecast']['per_symbol']['FX.a']['on']
              and st['forecast']['per_symbol']['FX.a']['why'] is None, r.get('error', ''))
        runs.clear()
        r, calls = run_and_wait(Bridge(), symbols=['XAUUSD.a', 'FX.a'], rescore=True)
        built = [a[a.index('--symbol') + 1] for n, a in runs if n == 'forecast']
        scored = [a[a.index('--symbol') + 1] for n, a in runs if n == 'rescore']
        check('each chosen symbol with its engine on is rebuilt and rescored on its own',
              r['ok'] and built == ['XAUUSD.a', 'FX.a'] and scored == ['XAUUSD.a', 'FX.a']
              and md.status()['job']['steps'][3]['state'] == 'done', f'{built} {scored}')
        runs.clear()
        FAIL_FOR.add('FX.a')
        r, calls = run_and_wait(Bridge(), symbols=['XAUUSD.a', 'FX.a'], rescore=True)
        FAIL_FOR.clear()
        st = md.status()
        scored = [a[a.index('--symbol') + 1] for n, a in runs if n == 'rescore']
        check("one symbol's failed rebuild does not stop the other's, and the run says which",
              scored == ['XAUUSD.a'] and 'FX.a' in (st['job']['error'] or '')
              and not st['history'][-1]['ok'], st['job']['error'] or '')
        runs.clear()
        b = Bridge()
        r, calls = run_and_wait(b, symbols=['FX.a'], download=False, rescore=True)
        st = md.status()
        check('the forecast alone: MT5 is left alone and only FX.a is rebuilt and rescored',
              r['ok'] and not calls and not b.calls and r['reason'] == 'forecast only'
              and [n for n, _ in runs] == ['forecast', 'rescore']
              and [s['state'] for s in st['job']['steps'][:2]] == ['skipped', 'skipped'],
              f"{[n for n, _ in runs]} {r.get('error', '')}")
        r = md.start(Bridge(), symbols=['NEW.a'], download=False)
        check('...refused for a symbol not on disk', not r['ok'] and 'not on disk' in r['error'],
              r.get('error', ''))
        md.set_forecast('FX.a', False)
        check('switching it off keeps gold on', md.forecast_symbols() == ['XAUUSD.a'])
        r = md.start(Bridge(), symbols=['FX.a'], download=False)
        check('...and the forecast alone is refused for a symbol with its engine off',
              not r['ok'] and 'engine is off' in r['error'], r.get('error', ''))
        runs.clear()
        r, calls = run_and_wait(Bridge(), symbols=['NEW.a'], enable_forecast=True)
        st = md.status()
        check('a new download asked to switch the engine on: not for history that cannot '
              'carry it - it says why', 'NEW.a' not in md.forecast_symbols()
              and st['job']['steps'][2]['state'] == 'skipped'
              and 'NEW.a' in st['job']['steps'][2]['note'], st['job']['steps'][2]['note'][:80])
        runs.clear()
        r, calls = run_and_wait(Bridge(), symbols=['XAUUSD.a', 'FX.a'], enable_forecast=['NEW.a'])
        check('switching on is only for the symbols named', 'FX.a' not in md.forecast_symbols()
              and [a[a.index('--symbol') + 1] for n, a in runs if n == 'forecast'] == ['XAUUSD.a'])
        runs.clear()
        r, calls = run_and_wait(Bridge(), symbols=['FX.a'], enable_forecast=['FX.a'])
        check('...and for history that can, it is switched on and built in the same run',
              md.forecast_symbols() == ['XAUUSD.a', 'FX.a']
              and [a[a.index('--symbol') + 1] for n, a in runs if n == 'forecast'] == ['FX.a'])
        md.set_forecast('FX.a', False)

        # ---------------------------------------------------------------- 4
        section('4. the monthly routine')
        md.set_auto(False)
        check('off: it never starts', md.routine_tick(Bridge()) is None)
        md.set_auto(True)
        check('on but updated today: it waits', md.routine_tick(Bridge()) is None)
        doc = md._load()
        doc['last_ok_ms'] -= 30 * md.DAY_MS
        doc['history'] = []
        md._save(doc)
        check('the market is shut on Saturday, open on Monday',
              md._market_shut(sat) and not md._market_shut(mon))
        real = md.datetime
        md.datetime = types.SimpleNamespace(now=lambda tz=None: mon)
        check('due, but a weekday: it waits for the weekend', md.routine_tick(Bridge()) is None)
        md.datetime = types.SimpleNamespace(now=lambda tz=None: sat)
        b.polls = 0
        res = md.routine_tick(Bridge())
        for _ in range(200):
            if not md._JOB['running']:
                break
            time.sleep(0.01)
        md.datetime = real
        check('due and the weekend: the monthly update runs',
              res == 'started' and md._load()['history'][-1]['reason'] == 'monthly routine')

        # ---------------------------------------------------------------- 5
        section('5. readers see the new bars')
        from server import datafeed
        from server.lab import data as lab_data
        real_dir = datafeed.DATA_DIR
        datafeed.DATA_DIR = lab_data.DATA_DIR = tmp
        try:
            before = datafeed.load_disk('XAUUSD.a', '5m', [2026])
            cov0 = lab_data.coverage('XAUUSD.a')['5m']['last_ms']
            t_new = int(before.t[-1] // 1000) + 300
            time.sleep(0.05)
            dl.merge('XAUUSD.a', '5m', {t_new: f'{t_new},4600,4601,4599,4600.5,10,0,15'})
            after = datafeed.load_disk('XAUUSD.a', '5m', [2026])
            check('the disk cache reloads a year file that changed',
                  len(after) == len(before) + 1 and int(after.t[-1]) == t_new * 1000)
            check("the lab's coverage follows it",
                  lab_data.coverage('XAUUSD.a')['5m']['last_ms'] == t_new * 1000 > cov0)
        finally:
            datafeed.DATA_DIR = lab_data.DATA_DIR = real_dir
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f'\n{PASS} passed, {FAIL} failed')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
