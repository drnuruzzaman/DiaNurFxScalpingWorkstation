"""
server/marketdata.py - keep the history on disk current, by hand or once a month.

The lab replays, the forecast tables, the shadow log and every research tool
read data/<symbol>/<tf>/<year>.csv.gz. Nothing appended to those files on its
own, so they quietly stopped at the last manual download. This runs the update:

  1. DOWNLOAD   the bridge's own /download/start, which loads
                tools/mt5_download.py inside the bridge on its MT5 session and
                tops up every timeframe from the newest bar on disk. While it
                runs the bridge holds its MT5 lock, so live quotes and orders
                wait - seconds for a month's top-up. That is why a run on a
                trading day refuses while positions or pending orders are open
                (unless forced). At the weekend nothing is quoted or filled, so
                nothing can wait on the lock and the check is not made; the
                monthly routine only runs then.
  2. RELEASES   tools/news_backfill.py - the release history the news gate and
                the news layer read (needs FRED_API_KEY; a failure here is noted,
                not fatal).
  3. FORECAST   tools/forecast_build.py --symbol S, for each updated symbol whose
                forecast engine is on: the caches and stores for the new bars
                (below normal priority; two workers on a trading day, four at
                the weekend). A symbol's first build reads every bar since the
                training start - an hour or more; later ones only the new bars.
  4. RESCORE    optional: tools/forecast_batch.py --symbol S - the promotion
                gates on the longer history. Off by default: a gate can change
                what the lab shows, and that should be a decision, not a side
                effect.

What is updated is a choice: every symbol on disk, or chosen symbols and
timeframes. The download can be left out to rebuild or rescore the forecast
alone.

The forecast engine runs for the symbols switched on here - gold's alone until
another is added. Each is built and scored on its own history, which has to
reach back to the training start (2018) on every timeframe the engine reads,
with real 1m bars from then: the trade odds walk the 1m path.

The monthly routine (off until switched on): a run starts at the weekend when
the last successful update is 28 or more days old. Its state is kept in
configs/marketdata.json. Measurements and data only - nothing here trades.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG, TF_SECONDS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
STATE_FILE = ROOT / 'configs' / 'marketdata.json'
LOG_DIR = ROOT / 'runs' / 'marketdata'
DUE_DAYS = 28
DAY_MS = 86_400_000
MAX_YEARS = 30                  # history for a new timeframe - the bridge's own limit
_LOCK = threading.Lock()
_JOB: dict = {'running': False, 'phase': 'idle', 'steps': [], 'log': [], 'error': None,
              'started_ms': None, 'finished_ms': None, 'reason': None, 'cancel': False}


# --------------------------------------------------------------------------- #
# state                                                                       #
# --------------------------------------------------------------------------- #
def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'auto': False, 'last_ok_ms': None, 'history': []}


def _save(doc: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(doc, indent=1), encoding='utf-8')
    tmp.replace(STATE_FILE)


def set_auto(on: bool) -> dict:
    with _LOCK:
        doc = _load()
        doc['auto'] = bool(on)
        _save(doc)
    return status()


def forecast_symbols(doc: dict | None = None) -> list:
    """The symbols the forecast engine runs for - gold's alone until changed."""
    from .forecast import SYMBOL
    doc = _load() if doc is None else doc
    got = doc.get('forecast_symbols')
    return [str(s) for s in got] if isinstance(got, list) else [SYMBOL]


def set_forecast(symbol: str, on: bool) -> dict:
    """
    Switch the forecast engine on or off for one symbol. On needs history that
    can carry it (forecast_ready); its tables are built by the next update that
    includes it. Off keeps whatever tables exist - they just stop being rebuilt.
    """
    symbol = str(symbol or '')
    if not SYMBOL_RE.match(symbol):
        return {'ok': False, 'error': f'not a symbol name: {symbol}'}
    if on:
        cov = coverage()                    # fresh: the files may have changed a moment ago
        _COVER.update(at=time.time(), doc=cov)
        why = forecast_ready(symbol, cov, deep=True)
        if why:
            return {'ok': False, 'error': f'{symbol}: {why}'}
    with _LOCK:
        doc = _load()
        cur = forecast_symbols(doc)
        doc['forecast_symbols'] = ((cur + [symbol]) if symbol not in cur else cur) if on \
            else [s for s in cur if s != symbol]
        _save(doc)
    _FCINFO['at'] = 0.0
    return dict(status(), ok=True)


def forecast_needs() -> tuple:
    """
    What the forecast engine reads from disk: its timeframes, the MTF rungs
    above them and the 1m path its trade odds walk - (timeframes, first year).
    """
    from .forecast import QT_TFS, TFS, TRADE_TFS, TRAIN_START
    need = set(TFS) | set(QT_TFS) | ({'1m'} if TRADE_TFS else set())
    return sorted(need, key=lambda t: TF_SECONDS[t]), int(TRAIN_START[:4])


def forecast_ready(symbol: str, cov: dict | None = None, deep: bool = False) -> str | None:
    """
    None when the history on disk can carry the forecast engine for `symbol`,
    else why not. Every table trains from the first year and chooses its
    settings on 2020-2023, so each timeframe has to reach back that far. `deep`
    also opens the first year's 1m file: MT5 pads the years before a broker's
    1m history with coarser bars (gold's 1998 "1m" file is daily bars), and a
    trade walked on those would be scored on a path that never existed.
    """
    tfs, year = forecast_needs()
    have = (coverage() if cov is None else cov).get(symbol) or {}
    if not have:
        return 'not on disk - download it first'
    missing = [t for t in tfs if t not in have]
    if missing:
        return f"needs {', '.join(missing)} on disk as well - the engine reads {', '.join(tfs)}"
    late = [f"{t} from {have[t]['first_year']}" for t in tfs
            if int(have[t]['first_year'] or 9999) > year]
    if late:
        n = datetime.now(timezone.utc).year - year + 1
        return (f"needs history from {year}, where its tables start - on disk: {', '.join(late)}. "
                f'A new download needs {n}+ years; the update only tops up forward')
    if deep and not _minute_bars(symbol, year):
        return (f'its {year} 1m file is not minute bars - MT5 filled the years before the '
                "broker's 1m history with coarser bars, and the trade odds walk the 1m path")
    return None


def _minute_bars(symbol: str, year: int) -> bool:
    """Whether the year's 1m file opens with minute bars (the first 200 rows' median step)."""
    ts = []
    try:
        with gzip.open(DATA / symbol / '1m' / f'{year}.csv.gz', 'rt', encoding='utf-8') as fh:
            next(fh, None)                                             # header
            for line in fh:
                ts.append(int(line.split(',', 1)[0]))
                if len(ts) >= 200:
                    break
    except (OSError, ValueError, EOFError):
        return False
    if len(ts) < 50:
        return False
    steps = sorted(b - a for a, b in zip(ts, ts[1:]))
    return steps[len(steps) // 2] <= 60


_FCINFO = {'at': 0.0, 'doc': {}}


def _forecast_info(symbols: list) -> dict:
    """Per symbol: when its forecast tables were built and its gates last scored, and what passed."""
    if time.time() - _FCINFO['at'] < 30 and not _JOB['running'] \
            and set(symbols) <= set(_FCINFO['doc']):
        return _FCINFO['doc']
    from .forecast import CACHE_DIR, TFS, latest_run
    doc = {}
    for s in symbols:
        built = []
        for tf in TFS:
            try:
                built.append((CACHE_DIR / s / tf / 'forecast.npz').stat().st_mtime)
            except OSError:
                built = []
                break
        run, promoted, scored = latest_run(s), None, None
        if run is not None:
            try:
                u = json.loads((run / 'ui_summary.json').read_text(encoding='utf-8'))
                scored = (run / 'ui_summary.json').stat().st_mtime
                cond = u.get('cond') or {}
                promoted = {
                    'range': [tf for tf, g in (u.get('range') or {}).items() if g.get('promoted')],
                    'regime': [tf for tf, g in cond.items() if (g.get('state') or {}).get('promoted')],
                    'direction': [tf for tf, g in cond.items()
                                  if (g.get('direction') or {}).get('promoted')],
                    'trade': sum(1 for g in cond.values()
                                 for t in ((g.get('trade') or {}).get('templates') or {}).values()
                                 if t.get('promoted'))}
            except (OSError, ValueError, AttributeError):
                pass
        doc[s] = {'built_ms': int(min(built) * 1000) if built else None,
                  'run_id': run.name if run is not None else None,
                  'scored_ms': int(scored * 1000) if scored else None, 'promoted': promoted}
    _FCINFO.update(at=time.time(), doc=doc)
    return doc


# --------------------------------------------------------------------------- #
# what is on disk                                                             #
# --------------------------------------------------------------------------- #
def coverage() -> dict:
    """{symbol: {tf: {'first_year', 'last_ms'}}} - the newest bar per timeframe, from the files."""
    sys.path.insert(0, str(ROOT))
    from tools import mt5_download as dl
    out = {}
    for s in sorted(p.name for p in DATA.iterdir() if p.is_dir() and not p.name.startswith('_')):
        cov = dl.coverage(s)
        if cov:
            out[s] = {tf: {'first_year': v['first_year'],
                           'last_ms': int(v['last_ts']) * 1000 if v['last_ts'] else None}
                      for tf, v in cov.items()}
    return out


_COVER = {'at': 0.0, 'doc': {}}


def _coverage_cached(max_age: float = 60.0) -> dict:
    # Fresher while an update writes new bars - but not on every poll: reading
    # every symbol's files takes about half a second, and the API has a live
    # feed to serve in the same process.
    if time.time() - _COVER['at'] > (10.0 if _JOB['running'] else max_age):
        _COVER.update(at=time.time(), doc=coverage())
    return _COVER['doc']


def job_status(log: int = 0) -> dict:
    """
    The running update - or the last one: its steps, the overall percentage and
    what it is doing. Nothing read from disk, so the footer can poll it; `log`
    adds that many of its newest log lines.
    """
    with _LOCK:
        job = {k: v for k, v in _JOB.items() if k not in ('cancel', 'log')}
        job['steps'] = [dict(s) for s in _JOB['steps']]
        if log:
            job['log'] = list(_JOB['log'][-log:])
    job['pct'] = overall(job['steps']) if job['steps'] else 0.0
    # What it is doing now - or, once stopped, the last thing it was doing.
    cur = next((x for x in job['steps'] if x['state'] == 'running'), None) or next(
        (x for x in reversed(job['steps'])
         if x.get('detail') and x['state'] not in ('waiting', 'skipped')), None)
    job['detail'] = (cur or {}).get('detail') or ''
    return job


def status() -> dict:
    doc = _load()
    cov = _coverage_cached()
    last_ok = doc.get('last_ok_ms')
    now = int(time.time() * 1000)
    newest = max((v['last_ms'] or 0 for tfs in cov.values() for v in tfs.values()), default=0)
    job = job_status(log=60)
    enabled = forecast_symbols(doc)
    tfs_need, year = forecast_needs()
    names = sorted(set(cov) | set(enabled))
    info = _forecast_info(names)
    fc = {s: dict(info.get(s) or {}, on=s in enabled, why=forecast_ready(s, cov)) for s in names}
    return {'coverage': cov, 'newest_ms': newest or None,
            'forecast': {'symbols': enabled, 'per_symbol': fc,
                         'needs': {'tfs': tfs_need, 'from_year': year,
                                   'years': datetime.now(timezone.utc).year - year + 1}},
            'max_years': MAX_YEARS,
            'gap_days': round((now - newest) / DAY_MS, 1) if newest else None,
            'routine': {'auto': bool(doc.get('auto')), 'last_ok_ms': last_ok,
                        'next_due_ms': (last_ok + DUE_DAYS * DAY_MS) if last_ok else None,
                        'due': (not last_ok) or now - last_ok >= DUE_DAYS * DAY_MS,
                        'rule': f'at the weekend, once {DUE_DAYS}+ days after the last update'},
            'history': (doc.get('history') or [])[-12:], 'job': job}


# --------------------------------------------------------------------------- #
# the job                                                                     #
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    line = time.strftime('%H:%M:%S ') + str(msg)
    with _LOCK:
        _JOB['log'] = (_JOB['log'] + [line])[-400:]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f"{time.strftime('%Y%m')}.log", 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def _step(name: str, state: str, note: str = '') -> None:
    with _LOCK:
        for s in _JOB['steps']:
            if s['name'] == name:
                s.update(state=state, note=note, at_ms=int(time.time() * 1000))
                if state == 'done':
                    s['frac'] = 1.0
        _JOB['phase'] = name if state == 'running' else _JOB['phase']


def _progress(name: str, frac: float, detail: str = '') -> None:
    """How far one step has got (0..1) and what it is doing - for the progress bar."""
    with _LOCK:
        for s in _JOB['steps']:
            if s['name'] == name:
                s['frac'] = max(0.0, min(1.0, float(frac)))
                if detail:
                    s['detail'] = detail


# Share of the time each step takes, for the overall percentage - from the first
# real update (2026-09-26): download 8 s, releases 5 s, forecast rebuild 13.5 min,
# rescore 70 s. A new symbol's first download is longer; its forecast step is
# skipped, so the download then carries the bar. Skipped steps drop out.
WEIGHTS = {'download': 10, 'releases': 3, 'forecast': 80, 'rescore': 7}


def overall(steps: list) -> float:
    live = [s for s in steps if s['state'] != 'skipped']
    tot = sum(WEIGHTS.get(s['name'], 10) for s in live) or 1
    return round(100.0 * sum(WEIGHTS.get(s['name'], 10) * (s.get('frac') or 0) for s in live) / tot,
                 1)


SYMBOL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._#\-]{0,31}$')


def start(bridge, rebuild: bool = True, rescore: bool = False, force: bool = False,
          reason: str = 'manual', symbols: list = None, tfs: list = None, years: int = 5,
          spec=None, download: bool = True, enable_forecast=False) -> dict:
    """
    Begin an update. With no symbols: every instrument on disk, topped up. With
    symbols: just those - one not on disk yet gets `years` of history on `tfs`
    (all timeframes by default); one on disk is topped up on `tfs` (its own
    timeframes by default). The forecast tables are rebuilt - and with
    `rescore` the gates rescored - for the chosen symbols whose forecast engine
    is on; `enable_forecast` (True for all of them, or a list of symbols)
    switches it on once their history is down, if that history can carry it.
    download=False leaves MT5 alone and only rebuilds or rescores. Returns
    {'ok': True, ...} or the reason not.
    """
    with _LOCK:
        if _JOB['running']:
            return {'ok': False, 'error': 'an update is already running'}
    sys.path.insert(0, str(ROOT))
    from tools import mt5_download as dl
    # Any timeframe on disk counts: a symbol kept without 1m is still topped up
    # on its own timeframes, never handed all ten as if it were new.
    on_disk = sorted(p.name for p in DATA.iterdir() if p.is_dir() and not p.name.startswith('_')
                     and dl.disk_tfs(p.name))
    if symbols:
        bad = [s for s in symbols if not SYMBOL_RE.match(str(s))]
        if bad:
            return {'ok': False, 'error': f'not a symbol name: {", ".join(map(str, bad))}'}
        symbols = list(dict.fromkeys(str(s) for s in symbols))
    else:
        symbols = on_disk or [CONFIG.symbol]
    want = [t for t in (tfs or list(dl.TF_SECONDS)) if t in dl.TF_SECONDS]
    if not want:
        return {'ok': False, 'error': 'choose at least one timeframe'}
    years = max(1, min(MAX_YEARS, int(years or 5)))
    new = [s for s in symbols if s not in on_disk]
    rebuild = bool(rebuild or rescore)             # the gates are scored on fresh tables
    enabled = forecast_symbols()
    fc = [s for s in symbols if s in enabled] if rebuild else []
    wanted = symbols if enable_forecast is True else [str(s) for s in (enable_forecast or [])]
    enable = [s for s in symbols if s in wanted and s not in enabled]
    if not download:
        if new:
            return {'ok': False, 'error': f"not on disk yet: {', '.join(new)} - download it first"}
        if not fc:
            return {'ok': False, 'error': 'nothing to do - ' + (
                'choose rebuild or rescore' if not rebuild else
                f"the forecast engine is off for {', '.join(symbols)}")}
    else:
        h = bridge.health() or {}
        if not h.get('connected'):
            return {'ok': False, 'error': 'the MT5 bridge is not connected - the download runs '
                                           'on its MetaTrader session'}
        if h.get('mock'):
            return {'ok': False, 'error': 'the bridge is in mock mode - there is no real '
                                           'history to download'}
        if (bridge._get('/download/status') or {}).get('running'):
            return {'ok': False, 'error': 'the bridge is already downloading'}
        # At the weekend nothing is quoted or filled, so nothing waits on MT5's
        # lock and open positions are no reason to hold the download back.
        if not force and not _market_shut(datetime.now(timezone.utc)):
            pos = bridge.positions(strict=True)
            orders = bridge.orders(strict=True)
            if pos is None or orders is None:
                return {'ok': False, 'needs_force': True,
                        'error': 'the bridge did not say whether positions are open - run '
                                 'anyway only if you know none are'}
            if pos or orders:
                wait = ('minutes, for years of new history' if new else 'usually seconds')
                return {'ok': False, 'needs_force': True,
                        'error': f'{len(pos)} open position(s) and {len(orders)} pending '
                                 'order(s): while MT5 downloads, their quotes, trailing stops '
                                 f'and any new order wait for it ({wait}). Run anyway, or '
                                 'wait for the weekend, when the market is shut.'}
    # Each symbol with its own timeframes: the bridge applies one list to a whole
    # job, and a symbol kept on two timeframes must not suddenly get all ten.
    plan = [(s, (dl.disk_tfs(s) if s in on_disk and not tfs else want)) for s in symbols] \
        if download else []
    # Only a top-up of everything on disk, on every timeframe it has, is "the
    # update" the monthly routine counts from; a selection leaves the rest as old
    # as it was.
    full = bool(download and set(on_disk) <= set(symbols)
                and all(set(dl.disk_tfs(s)) <= set(want) for s in on_disk))
    if reason == 'manual':
        reason = ('forecast only' if not download else 'add symbol' if new
                  else 'manual' if full else 'selected')
    build = rebuild and bool(fc or enable)
    steps = [{'name': 'download', 'state': 'waiting' if download else 'skipped', 'note': '',
              'frac': 0.0},
             {'name': 'releases', 'state': 'waiting' if download else 'skipped', 'note': '',
              'frac': 0.0},
             {'name': 'forecast', 'state': 'waiting' if build else 'skipped',
              'note': '' if build or not rebuild else 'forecast engine off', 'frac': 0.0},
             {'name': 'rescore', 'state': 'waiting' if (rescore and build) else 'skipped',
              'note': '', 'frac': 0.0}]
    with _LOCK:
        _JOB.update(running=True, phase='download' if download else 'forecast', steps=steps,
                    log=[], error=None, started_ms=int(time.time() * 1000), finished_ms=None,
                    reason=reason, cancel=False, symbols=symbols, new_symbols=new, forecast=fc,
                    full=full)
    threading.Thread(target=_run, args=(bridge, plan, years, rebuild, rescore, spec, fc, enable),
                     daemon=True, name='marketdata').start()
    return {'ok': True, 'symbols': symbols, 'new': new, 'forecast': fc, 'reason': reason}


def cancel(bridge) -> dict:
    with _LOCK:
        _JOB['cancel'] = True
    bridge._get('/download/cancel')
    return {'ok': True}


def _cancelled() -> bool:
    with _LOCK:
        return bool(_JOB['cancel'])


class _BuildProgress:
    """Reads tools/forecast_build.py's output: reads [k/N], then labels, then stores."""
    LABELS, STORES = 6, 4                     # 4 market + 2 trade label sets; 4 stores

    def __init__(self):
        self.reads, self.labels, self.stores = 0.0, 0, 0

    def __call__(self, line: str):
        m = re.search(r'\[\s*(\d+)/(\d+)\]', line)
        if line.startswith('reads:') and 'fresh' in line:
            self.reads = 1.0
        elif m and not line.startswith('store'):
            self.reads = int(m.group(1)) / max(1, int(m.group(2)))
        elif line.startswith('labels:'):
            self.reads, self.labels = 1.0, self.labels + 1
        elif line.startswith('store:'):
            self.reads, self.labels, self.stores = 1.0, self.LABELS, self.stores + 1
        else:
            return None
        # reads ~9 min, labels ~2.5, stores ~2 of the first real rebuild
        frac = (0.65 * self.reads + 0.2 * min(1, self.labels / self.LABELS)
                + 0.15 * min(1, self.stores / self.STORES))
        what = ('stores' if self.stores else 'labels' if self.labels else 'reads')
        return frac, f'{what}: {line.strip()[:60]}'


class _BatchProgress:
    """Reads tools/forecast_batch.py's output: one line per market and trade timeframe."""
    TOTAL = 6

    def __init__(self):
        self.n = 0

    def __call__(self, line: str):
        s = line.strip()
        if s.startswith('market ') or s.startswith('trade '):
            self.n += 1
            return min(1.0, self.n / self.TOTAL), s[:60]
        return None


class _PerSymbol:
    """One symbol's progress parser, scaled into its share of a step that runs several."""

    def __init__(self, parse, i: int, n: int, symbol: str):
        self.parse, self.i, self.n, self.symbol = parse, i, n, symbol

    def __call__(self, line: str):
        got = self.parse(line)
        if not got:
            return None
        frac, detail = got
        return (self.i + frac) / self.n, f'{self.symbol} · {detail}'


def _subprocess(name: str, argv: list, parse=None, final: bool = True) -> bool:
    """
    Run one tool below normal priority, streaming its output into the log.
    final=False leaves the step running - one of several runs under one step.
    """
    flags = 0x00004000 if sys.platform == 'win32' else 0      # BELOW_NORMAL_PRIORITY_CLASS
    _step(name, 'running')
    _log(f'{name}: ' + ' '.join(argv))
    try:
        p = subprocess.Popen([sys.executable, '-u'] + argv, cwd=str(ROOT), text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=flags, encoding='utf-8', errors='replace')
        for line in p.stdout:
            if line.strip():
                _log(f'  {line.rstrip()[:300]}')
                got = parse(line.strip()) if parse else None
                if got:
                    _progress(name, *got)
            if _cancelled():
                p.terminate()
                _step(name, 'cancelled')
                return False
        code = p.wait()
    except OSError as exc:
        _log(f'{name}: {exc}')
        if final:
            _step(name, 'failed', str(exc))
        return False
    if final:
        _step(name, 'done' if code == 0 else 'failed', '' if code == 0 else f'exit {code}')
    return code == 0


def _build_workers() -> int:
    """Two on a trading day (the live API and the bridge keep the CPU); four when shut."""
    shut = _market_shut(datetime.now(timezone.utc))
    return 4 if shut and (os.cpu_count() or 2) >= 6 else 2


def _per_symbol(name: str, symbols: list, argv, parser) -> list:
    """One tool per symbol under one step; the symbols it succeeded for. A failure
    is noted and the others still run."""
    ok, failed = [], []
    for i, s in enumerate(symbols):
        if _cancelled():
            break
        if _subprocess(name, argv(s), _PerSymbol(parser(), i, len(symbols), s), final=False):
            ok.append(s)
            _progress(name, (i + 1) / len(symbols))
        else:
            failed.append(s)
    if _cancelled():
        _step(name, 'cancelled')
    elif failed:
        _step(name, 'failed', f"failed for {', '.join(failed)}")
    else:
        _step(name, 'done', ', '.join(ok) if len(symbols) > 1 else '')
    return ok


def _switch_on(symbols: list) -> tuple:
    """Switch the forecast engine on for the symbols whose history can now carry it."""
    cov = coverage()
    on, notes = [], []
    for s in symbols:
        why = forecast_ready(s, cov, deep=True)
        if why:
            notes.append(f'{s}: {why}')
            _log(f'forecast: not switched on for {s} - {why}')
        else:
            on.append(s)
    if on:
        with _LOCK:
            doc = _load()
            cur = forecast_symbols(doc)
            doc['forecast_symbols'] = cur + [s for s in on if s not in cur]
            _save(doc)
        _log(f"forecast: switched on for {', '.join(on)}")
    return on, notes


def _download(bridge, sym: str, tfs: list, years: int, index: int, count: int) -> int:
    """One symbol's download on the bridge; returns bars written. Raises on failure."""
    _log(f"download {sym}: {', '.join(tfs)}" + (f' ({years} years for anything new)'))
    # Named explicitly: to the bridge an empty tfs means "ticks only", which would
    # start a download that fetches nothing and reports success.
    r = bridge._get('/download/start', symbols=sym, tfs=','.join(tfs), years=years)
    if not (r or {}).get('ok'):
        raise RuntimeError(f"the bridge refused the download of {sym}: "
                           f"{(r or {}).get('error') or 'no answer'}")
    seen, silent_since = 0, None
    while True:
        time.sleep(2)
        st = bridge._get('/download/status')
        if st is None:                         # the bridge stopped answering
            silent_since = silent_since or time.time()
            if time.time() - silent_since > 300:
                raise RuntimeError('the bridge stopped answering during the download')
            continue
        silent_since = None
        for line in (st.get('log') or [])[seen:]:
            _log(f'  {line}')
        seen = len(st.get('log') or [])
        part = (float(st.get('done') or 0) / float(st['total'])) if st.get('total') else 0.0
        _progress('download', (index + min(1.0, part)) / count,
                  f"{sym} {st.get('tf') or ''} - {st.get('item') or st.get('phase') or ''}".strip())
        if not st.get('running') and st.get('finished_ms'):
            break
        if st.get('error') and not st.get('running'):
            break
    if st.get('error'):
        raise RuntimeError(f"download of {sym} failed on the bridge: {st['error']}")
    if st.get('phase') == 'cancelled' or _cancelled():
        _step('download', 'cancelled')
        raise RuntimeError('cancelled')
    return int(st.get('rows') or 0)


def _run(bridge, plan: list, years: int, rebuild: bool, rescore: bool, spec=None,
         fc: list = (), enable: list = ()) -> None:
    ok_download = False
    try:
        if plan:
            # ---- 1. download, on the bridge, one symbol at a time
            _step('download', 'running')
            rows = 0
            for i, (sym, tfs) in enumerate(plan):
                rows += _download(bridge, sym, tfs, years, i, len(plan))
                if spec:
                    try:
                        spec(sym)              # the live feed saves spec.json for the lab
                    except Exception:                         # noqa: BLE001
                        pass
            ok_download = True
            _step('download', 'done', f'{rows} bars written')
            _log(f'download: done, {rows} new or corrected bars')
            _refresh_readers()
            # ---- 2. release history (non-fatal)
            _progress('releases', 0.1, 'FRED release dates and the FOMC calendar')
            if not _subprocess('releases', ['tools/news_backfill.py']):
                _log('releases: not updated (see above) - the news gate keeps the history it has')
                _progress('releases', 1.0)
        # ---- 3. forecast caches and stores, per symbol whose engine is on
        fc, notes = list(fc), []
        if enable and not _cancelled():
            on, notes = _switch_on(list(enable))
            fc += [s for s in on if s not in fc] if rebuild else []
        built, errors = [], []
        if rebuild and fc and not _cancelled():
            w = _build_workers()
            built = _per_symbol('forecast', fc, lambda s: ['tools/forecast_build.py', '--symbol', s,
                                                           '--workers', str(w)], _BuildProgress)
            if len(built) < len(fc) and not _cancelled():
                errors.append('the forecast rebuild failed for '
                              + ', '.join(s for s in fc if s not in built) + ' - see the log')
        elif rebuild and not _cancelled():
            _step('forecast', 'skipped', '; '.join(notes) or 'forecast engine off')
        # ---- 4. rescore the gates of what was rebuilt
        if rescore and built and not _cancelled():
            scored = _per_symbol('rescore', built, lambda s: ['tools/forecast_batch.py', '--symbol',
                                                              s, '--no-rows'], _BatchProgress)
            if len(scored) < len(built) and not _cancelled():
                errors.append('rescoring failed for '
                              + ', '.join(s for s in built if s not in scored) + ' - see the log')
        elif rescore and not _cancelled():
            _step('rescore', 'skipped', 'nothing rebuilt')
        if errors:
            raise RuntimeError('; '.join(errors))
    except Exception as exc:                                  # noqa: BLE001
        with _LOCK:
            _JOB['error'] = str(exc)
        _log(f'stopped: {exc}')
    finally:
        now = int(time.time() * 1000)
        _COVER['at'] = _FCINFO['at'] = 0.0
        with _LOCK:
            err, reason = _JOB['error'], _JOB['reason']
            syms, full = list(_JOB.get('symbols') or []), bool(_JOB.get('full'))
            # Record first, then say finished: a status read in between must never
            # see "done" with no history entry for it. Read and written under the
            # lock, so a switch flipped meanwhile is not overwritten.
            doc = _load()
            if ok_download and not err and full:
                doc['last_ok_ms'] = now
            doc.setdefault('history', []).append({'at_ms': now, 'reason': reason, 'ok': not err,
                                                  'error': err, 'symbols': syms})
            doc['history'] = doc['history'][-24:]
            _save(doc)
            _JOB.update(running=False, finished_ms=now, phase='failed' if err else 'done')


def _refresh_readers() -> None:
    """Drop this process's cached year files so the new bars are read."""
    try:
        from . import datafeed
        with datafeed._DISK_LOCK:
            datafeed._DISK_CACHE.clear()
    except Exception:                                         # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# the monthly routine                                                         #
# --------------------------------------------------------------------------- #
def _market_shut(now: datetime) -> bool:
    """Saturday, or Sunday before the 21:00 UTC reopen - gold and FX do not trade."""
    return now.weekday() == 5 or (now.weekday() == 6 and now.hour < 20)


def routine_tick(bridge) -> str | None:
    """Called periodically. Starts the monthly update when it is on, due and the market shut."""
    doc = _load()
    if not doc.get('auto') or _JOB['running']:
        return None
    last = doc.get('last_ok_ms')
    now = int(time.time() * 1000)
    if last and now - last < DUE_DAYS * DAY_MS:
        return None
    if not _market_shut(datetime.now(timezone.utc)):
        return None
    # Do not retry a failed routine run every tick: once per 6 hours.
    tried = [h for h in doc.get('history') or [] if h.get('reason') == 'monthly routine']
    if tried and now - tried[-1]['at_ms'] < 6 * 3_600_000:
        return None
    r = start(bridge, rebuild=True, rescore=False, force=False, reason='monthly routine')
    return 'started' if r.get('ok') else r.get('error')


__all__ = ['status', 'job_status', 'start', 'cancel', 'set_auto', 'set_forecast', 'forecast_symbols',
           'forecast_needs', 'forecast_ready', 'coverage', 'routine_tick', 'MAX_YEARS']
