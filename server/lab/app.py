"""
server/lab/app.py - the lab's own server (port 8771).

REST for the things that are not time-critical - data coverage, the settings
schema, the saved-session list, snapshots - and ONE websocket for everything
that is: the transport (step, play, seek, run), manual trading, and the frames
the chart draws.

One session is active at a time. That is not a limitation worth engineering
around: the lab applies a session's settings to this process's CONFIG, and a
desk replays one market at a time anyway. Everything connected sees the same
session.

Long work (running to the end, resuming a saved session, fast-forwarding to a
date) runs in slices on a worker thread, so the socket keeps answering - Pause
and Cancel always get through.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from ..config import TF_SECONDS
from ..engine.signals import PLAYBOOKS
from . import data as lab_data
from . import session as S
from . import settings as lab_settings

ROOT = Path(__file__).resolve().parents[2]
TIMEFRAMES = [tf for tf in ('1m', '3m', '5m', '15m', '30m', '1h', '4h') if tf in TF_SECONDS]
SPEEDS = (1, 2, 4, 8, 16, 32, 64)
CHUNK_S = 0.6               # a long job reports progress this often
FRAMES_PER_S = 12           # play never pushes more frames than this

app = FastAPI(title='DiaNurFx Lab', version='1.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://127.0.0.1:5180', 'http://localhost:5180',
                   'http://127.0.0.1:8770', 'http://localhost:8770'],
    allow_credentials=False, allow_methods=['*'], allow_headers=['*'],
)


def _git_rev() -> str:
    try:
        rev = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT,
                             capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain', '--', 'server'], cwd=ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (rev or 'unknown') + ('+local' if dirty else '')
    except Exception:                                    # noqa: BLE001
        return 'unknown'


S.ENGINE_REV = _git_rev()


# --------------------------------------------------------------------------- #
# encoding                                                                    #
# --------------------------------------------------------------------------- #
def _clean(o):
    """NaN / inf -> null, numpy -> python: a frame the browser can always parse."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if hasattr(o, 'item') and not isinstance(o, (str, bytes)):
        try:
            return _clean(o.item())
        except Exception:                                # noqa: BLE001
            return str(o)
    return o


def dumps(obj) -> str:
    return json.dumps(_clean(obj), default=S._default, allow_nan=False)


# --------------------------------------------------------------------------- #
# state                                                                       #
# --------------------------------------------------------------------------- #
class Lab:
    def __init__(self):
        self.sess: S.ReplaySession | None = None
        self.clients: dict = {}          # websocket -> {'ev': n sent, 'tr': n sent}
        self.playing = False
        self.speed = 4
        self.task: asyncio.Task | None = None
        self.job: dict | None = None     # {'label', 'done', 'total'}
        self.stop = False
        self.last_save = 0.0


LAB = Lab()


async def _send(ws: WebSocket, msg: dict) -> bool:
    try:
        await ws.send_text(dumps(msg))
        return True
    except Exception:                                    # noqa: BLE001
        LAB.clients.pop(ws, None)
        return False


async def broadcast(msg: dict) -> None:
    for ws in list(LAB.clients):
        await _send(ws, msg)


async def send_state() -> None:
    await broadcast({'type': 'state', 'playing': LAB.playing, 'speed': LAB.speed,
                     'job': LAB.job, 'session': LAB.sess.sid if LAB.sess else None})


async def send_session() -> None:
    s = LAB.sess
    if s is None:
        await broadcast({'type': 'closed'})
        return
    payload = await asyncio.to_thread(s.session_payload)
    for ws in list(LAB.clients):
        LAB.clients[ws] = {'ev': len(s.events), 'tr': len(s.trades)}
        await _send(ws, payload)


async def push_frame() -> None:
    s = LAB.sess
    if s is None:
        return
    p = await asyncio.to_thread(s.view_payload)
    ev_n, tr_n = len(s.events), len(s.trades)
    stats = None
    for ws, sent in list(LAB.clients.items()):
        msg = dict(p)
        if sent.get('ev', 0) < ev_n:
            msg['events'] = s.events[sent.get('ev', 0):ev_n]
        if sent.get('tr', 0) < tr_n:
            msg['trades'] = s.trades[sent.get('tr', 0):tr_n]
            if stats is None:
                stats = await asyncio.to_thread(s.stats)
            msg['stats'] = stats
        LAB.clients[ws] = {'ev': ev_n, 'tr': tr_n}
        await _send(ws, msg)
    await _autosave()


async def _autosave(force: bool = False) -> None:
    s = LAB.sess
    if s is None or not s.cfg.get('record', True):
        return
    if force or time.time() - LAB.last_save > 8.0:
        LAB.last_save = time.time()
        await asyncio.to_thread(s.save)


async def _halt() -> None:
    """Stop whatever is running - play or a long job - and wait for it."""
    LAB.playing = False
    LAB.stop = True
    if LAB.sess is not None:
        LAB.sess.cancel = True
    t = LAB.task
    if t is not None and not t.done():
        try:
            await asyncio.wait_for(t, timeout=15)
        except Exception:                                # noqa: BLE001
            t.cancel()
    LAB.task = None
    LAB.job = None
    LAB.stop = False
    if LAB.sess is not None:
        LAB.sess.cancel = False


# --------------------------------------------------------------------------- #
# long work                                                                   #
# --------------------------------------------------------------------------- #
async def _run_to(target: int, label: str, after=None) -> None:
    """Simulate the frontier forward to bar `target`, reporting progress."""
    s = LAB.sess
    start_k = s.k
    total = max(1, target - start_k)
    LAB.job = {'label': label, 'done': 0, 'total': total}
    await send_state()
    per = 10
    try:
        while s.k < target and not LAB.stop and LAB.sess is s:
            t0 = time.perf_counter()
            ran = await asyncio.to_thread(s.advance, min(per, target - s.k))
            if ran == 0:
                break
            dt = time.perf_counter() - t0
            per = max(5, min(400, int(per * CHUNK_S / max(dt, 1e-3))))
            LAB.job = {'label': label, 'done': s.k - start_k, 'total': total}
            await broadcast({'type': 'progress', **LAB.job})
        if LAB.sess is s:
            s.set_view(min(target, s.k))
            if after:
                await after()
            await push_frame()
    finally:
        LAB.job = None
        await _autosave(force=True)
        await send_state()


async def _run_until_event(kinds: tuple, label: str, cap: int = 20_000) -> None:
    s = LAB.sess
    start_k = s.k
    LAB.job = {'label': label, 'done': 0, 'total': cap}
    await send_state()
    try:
        # One bar at a time, so the search stops ON the bar the event happened
        # and the frontier does not run past it - the account on screen is
        # then the live one and you can trade from there.
        found = s.next_event_bar(kinds)
        last_report = time.perf_counter()
        while found is None and not s.at_end() and not LAB.stop and s.k - start_k < cap:
            await asyncio.to_thread(s.advance, 1)
            found = s.next_event_bar(kinds)
            if time.perf_counter() - last_report > CHUNK_S:
                last_report = time.perf_counter()
                LAB.job = {'label': label, 'done': s.k - start_k, 'total': cap}
                await broadcast({'type': 'progress', **LAB.job})
        if LAB.sess is s:
            s.set_view(found if found is not None else s.k)
            await push_frame()
    finally:
        LAB.job = None
        await _autosave(force=True)
        await send_state()


async def _play() -> None:
    s = LAB.sess
    try:
        while LAB.playing and LAB.sess is s and not LAB.stop:
            t0 = time.perf_counter()
            per = max(1, round(LAB.speed / FRAMES_PER_S))
            moved = await asyncio.to_thread(s.step, per)
            await push_frame()
            if not moved:
                LAB.playing = False
                break
            wait = per / float(LAB.speed) - (time.perf_counter() - t0)
            if wait > 0:
                await asyncio.sleep(wait)
    except Exception as exc:                             # noqa: BLE001
        await broadcast({'type': 'error', 'message': f'play stopped: {exc}'})
    finally:
        LAB.playing = False
        await send_state()


def _spawn(coro) -> None:
    LAB.task = asyncio.create_task(coro)


# --------------------------------------------------------------------------- #
# the websocket                                                               #
# --------------------------------------------------------------------------- #
NEXT_KINDS = {
    'signal': ('signal',),
    'order': ('order', 'manual'),
    'fill': ('fill',),
    'exit': ('exit',),
    'trade': ('fill', 'exit'),
}


async def handle(ws: WebSocket, msg: dict) -> None:
    op = msg.get('op')
    s = LAB.sess

    if op == 'hello':
        await _send(ws, {'type': 'state', 'playing': LAB.playing, 'speed': LAB.speed,
                         'job': LAB.job, 'session': s.sid if s else None})
        if s is not None:
            LAB.clients[ws] = {'ev': len(s.events), 'tr': len(s.trades)}
            await _send(ws, await asyncio.to_thread(s.session_payload))
            p = await asyncio.to_thread(s.view_payload)
            await _send(ws, p)
        return

    if op == 'create':
        await _halt()
        if LAB.sess is not None:
            await _autosave(force=True)
        LAB.job = {'label': 'loading history', 'done': 0, 'total': 1}
        await send_state()
        try:
            LAB.sess = await asyncio.to_thread(S.ReplaySession, msg.get('cfg') or {})
        finally:
            LAB.job = None
        await _autosave(force=True)
        await send_session()
        await push_frame()
        await send_state()
        return

    if op == 'open':
        await _halt()
        if LAB.sess is not None and LAB.sess.sid != msg.get('sid'):
            await _autosave(force=True)
        LAB.job = {'label': 'loading session', 'done': 0, 'total': 1}
        await send_state()
        try:
            LAB.sess = await asyncio.to_thread(S.ReplaySession.open, str(msg.get('sid')))
        finally:
            LAB.job = None
        s = LAB.sess
        await send_session()
        target = s.resume_target()

        async def compare():
            await broadcast({'type': 'compare', **s.compare_saved()})
            await send_session()

        if target > s.k:
            _spawn(_run_to(target, 're-running the recorded session', after=compare))
        else:
            await push_frame()
            await send_state()
        return

    if s is None:
        await _send(ws, {'type': 'error', 'message': 'No session open - start one first.'})
        return

    if op in ('step', 'seek', 'next', 'end', 'frontier', 'first'):
        await _halt()
        if op == 'step':
            n = int(msg.get('n') or 1)
            if s.v + n > s.k and (s.v + n) - s.k > 60:
                _spawn(_run_to(min(s.last, s.v + n), f'simulating {n} bars'))
                return
            await asyncio.to_thread(s.step, n)
            await push_frame()
        elif op == 'seek':
            target = s.index_at(int(msg['t'])) if msg.get('t') is not None \
                else max(s.i0, min(s.last, int(msg.get('i') or s.v)))
            if target <= s.k:
                s.set_view(target)
                await push_frame()
            else:
                _spawn(_run_to(target, 'simulating to that bar'))
        elif op == 'next':
            kinds = NEXT_KINDS.get(msg.get('what') or 'trade', ('fill', 'exit'))
            found = s.next_event_bar(kinds)
            if found is not None:
                s.set_view(found)
                await push_frame()
            else:
                _spawn(_run_until_event(kinds, f"looking for the next {msg.get('what') or 'trade'}"))
        elif op == 'end':
            _spawn(_run_to(s.last, 'running to the end'))
        elif op == 'frontier':
            s.set_view(s.k)
            await push_frame()
        elif op == 'first':
            s.set_view(s.i0)
            await push_frame()
        return

    if op == 'play':
        if msg.get('speed') in SPEEDS:
            LAB.speed = int(msg['speed'])
        if not LAB.playing:
            await _halt()
            LAB.playing = True
            _spawn(_play())
        await send_state()
        return
    if op == 'speed':
        if msg.get('speed') in SPEEDS:
            LAB.speed = int(msg['speed'])
        await send_state()
        return
    if op in ('pause', 'cancel'):
        await _halt()
        await push_frame()
        await send_state()
        return

    if op == 'restart':
        await _halt()
        await asyncio.to_thread(s.reset)
        await _autosave(force=True)
        await send_session()
        await push_frame()
        return
    if op == 'configure':
        await _halt()
        await asyncio.to_thread(s.configure, msg.get('patch') or {})
        await _autosave(force=True)
        await send_session()
        await push_frame()
        return
    if op in ('rename', 'annotate'):
        # Name, notes and tags describe the session; they do not change what
        # it traded, so they never restart the run.
        if msg.get('name') is not None:
            s.cfg['name'] = str(msg.get('name') or s.cfg['name'])[:80]
        if msg.get('notes') is not None:
            s.cfg['notes'] = str(msg.get('notes'))[:2000]
        if isinstance(msg.get('tags'), list):
            s.cfg['tags'] = [str(x)[:30] for x in msg['tags']][:20]
        await _autosave(force=True)
        await broadcast({'type': 'meta', 'meta': s.meta()})
        return
    if op == 'command':
        await _halt()
        res = await asyncio.to_thread(s.command, msg.get('cmd') or {})
        await _send(ws, {'type': 'result', 'op': (msg.get('cmd') or {}).get('op'), **res})
        await push_frame()
        await _autosave(force=True)
        return
    if op == 'note':
        n = s.note(str(msg.get('text') or ''))
        await broadcast({'type': 'notes', 'notes': s.bar_notes, 'added': n})
        await _autosave(force=True)
        return
    if op == 'tag':
        s.tag_trade(str(msg.get('id')), msg.get('tag'), msg.get('note'))
        await broadcast({'type': 'tags', 'tags': s.trade_tags})
        await _autosave(force=True)
        return
    if op == 'save':
        await _autosave(force=True)
        await broadcast({'type': 'meta', 'meta': s.meta(), 'saved': True})
        return
    if op == 'close':
        await _halt()
        await _autosave(force=True)
        LAB.sess = None
        await broadcast({'type': 'closed'})
        await send_state()
        return

    await _send(ws, {'type': 'error', 'message': f'unknown operation {op}'})


@app.websocket('/lab/ws')
async def lab_ws(ws: WebSocket):
    await ws.accept()
    LAB.clients[ws] = {'ev': 0, 'tr': 0}
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            try:
                await handle(ws, msg)
            except Exception as exc:                     # noqa: BLE001
                await _send(ws, {'type': 'error', 'message': f'{type(exc).__name__}: {exc}'})
                LAB.job = None
                await send_state()
    except WebSocketDisconnect:
        pass
    finally:
        LAB.clients.pop(ws, None)


# --------------------------------------------------------------------------- #
# REST                                                                        #
# --------------------------------------------------------------------------- #
@app.get('/lab/health')
def health():
    return {'ok': True, 'pid': os.getpid(), 'engine_rev': S.ENGINE_REV,
            'session': LAB.sess.sid if LAB.sess else None}


@app.get('/lab/data')
def data_info():
    out = []
    for sym in lab_data.symbols():
        cover = lab_data.coverage(sym)
        out.append({'symbol': sym,
                    'coverage': {tf: v for tf, v in cover.items() if tf in TIMEFRAMES or tf == '1m'},
                    'spec_source': lab_data.spec(sym).get('source')})
    return {'symbols': out, 'timeframes': TIMEFRAMES, 'playbooks': list(PLAYBOOKS),
            'speeds': list(SPEEDS), 'max_bars': S.MAX_BARS}


@app.get('/lab/schema')
def schema():
    return {'schema': lab_settings.SCHEMA,
            'live': lab_settings.jsonable(lab_settings.effective(None))}


@app.get('/lab/sessions')
def sessions():
    rows = S.list_sessions()
    active = LAB.sess.sid if LAB.sess else None
    for r in rows:
        r['active'] = r.get('id') == active
    return {'sessions': rows}


@app.delete('/lab/sessions/{sid}')
async def delete(sid: str):
    if not S.valid_sid(sid):
        raise HTTPException(400, 'bad session id')
    if LAB.sess is not None and LAB.sess.sid == sid:
        await _halt()
        LAB.sess = None
        await broadcast({'type': 'closed'})
    return {'ok': S.delete_session(sid)}


_DATA_URL = re.compile(r'^data:image/png;base64,')


@app.post('/lab/sessions/{sid}/snapshot')
async def snapshot(sid: str, body: dict = Body(...)):
    s = LAB.sess
    if s is None or s.sid != sid:
        raise HTTPException(409, 'that session is not the one open')
    png = str(body.get('png') or '')
    if not _DATA_URL.match(png):
        raise HTTPException(400, 'expected a PNG data URL')
    raw = base64.b64decode(_DATA_URL.sub('', png))
    if len(raw) > 12_000_000:
        raise HTTPException(413, 'snapshot too large')
    meta = await asyncio.to_thread(s.save_snapshot, raw, str(body.get('note') or ''))
    s._emit('snapshot', f"snapshot {meta['file']}" + (f" - {meta['note']}" if meta['note'] else ''))
    await _autosave(force=True)
    await broadcast({'type': 'snaps', 'snaps': s.snaps})
    return meta


_FILE = re.compile(r'^snap-[0-9]{3}-[0-9]{8}-[0-9]{4}\.png$')


@app.get('/lab/sessions/{sid}/snapshots/{name}')
def snapshot_file(sid: str, name: str):
    if not (S.valid_sid(sid) and _FILE.match(name)):
        raise HTTPException(400, 'bad name')
    path = S.RUNS / sid / 'snapshots' / name
    if not path.exists():
        raise HTTPException(404, 'no such snapshot')
    return FileResponse(path, media_type='image/png')


@app.get('/lab/sessions/{sid}/export')
def export(sid: str):
    if not S.valid_sid(sid):
        raise HTTPException(400, 'bad session id')
    base = S.RUNS / sid
    try:
        doc = {name: json.loads((base / f'{name}.json').read_text(encoding='utf-8'))
               for name in ('session', 'trades', 'events')}
    except (OSError, ValueError):
        raise HTTPException(404, 'no such session')
    return Response(content=json.dumps(doc, indent=1), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="lab-{sid}.json"'})


__all__ = ['app']
