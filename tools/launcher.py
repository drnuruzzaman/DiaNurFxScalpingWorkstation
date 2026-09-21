#!/usr/bin/env python
"""
tools/launcher.py - start the whole workstation from one window.

Double-click start_dianurfx.cmd (project root). One window, three services:

    Bridge   bridge/mt5_bridge.py   talks to the MetaTrader terminal   :8765
    API      server.main            the engine, signals, execution     :8770
    UI       vite dev server        the workstation in the browser     :5180

No console windows: each service writes to run/logs/<name>.log instead, and
the launcher shows a live light per service by polling its port.

ARMING stays here, not in the web page. "Arm trading" is a checkbox in THIS
window, unticked every time the launcher opens: the bridge is the only process
that can move money, and its on-switch is kept outside the browser so no bug,
stale tab or anyone else on the page can arm it. Everything else - auto mode,
lots, exits, playbooks, timeframes - is set in the web UI (Settings > Risk &
Gates) and saved there.

A service already running on its port (started some other way) is detected
and left alone - never started twice.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / 'run' / 'logs'
PREFS = ROOT / 'configs' / 'launch.json'
UI_URL = 'http://127.0.0.1:5180'

# The launcher itself may run under pythonw (no console). Its children must run
# under python.exe, or their prints have nowhere to go and some libraries fail.
_exe = Path(sys.executable)
PYTHON = str(_exe.with_name('python.exe')) if _exe.name.lower() == 'pythonw.exe' else str(_exe)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(('127.0.0.1', port)) == 0


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return 200 <= r.status < 300
    except Exception:                                         # noqa: BLE001
        return False


def load_prefs() -> dict:
    try:
        return json.loads(PREFS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_prefs(p: dict) -> None:
    # Never persist the arm checkbox: arming is a decision per session.
    p = {k: v for k, v in p.items() if k != 'arm'}
    PREFS.parent.mkdir(parents=True, exist_ok=True)
    PREFS.write_text(json.dumps(p, indent=1), encoding='utf-8')


class Service:
    def __init__(self, name, port, health_url, cmd_fn, cwd):
        self.name, self.port, self.health_url = name, port, health_url
        self.cmd_fn, self.cwd = cmd_fn, cwd
        self.proc = None          # only set when WE started it

    def running(self) -> bool:
        return port_open(self.port)

    def healthy(self) -> bool:
        return http_ok(self.health_url) if self.health_url else self.running()

    def start(self, opts: dict) -> str:
        if self.running():
            return f'{self.name}: already running on :{self.port} - left as is'
        LOGS.mkdir(parents=True, exist_ok=True)
        log = open(LOGS / f'{self.name.lower()}.log', 'a', encoding='utf-8')
        log.write(f'\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} started by launcher =====\n')
        log.flush()
        cmd = self.cmd_fn(opts)
        self.proc = subprocess.Popen(cmd, cwd=self.cwd, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                                     shell=False)
        return f'{self.name}: starting  ({" ".join(cmd[1:4])} ...)'

    def stop(self) -> str:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = None
            return f'{self.name}: not started by this launcher - left as is'
        # /T: take the whole tree. npm starts node as a child; killing npm
        # alone would leave the dev server holding its port.
        subprocess.run(['taskkill', '/PID', str(self.proc.pid), '/T', '/F'],
                       capture_output=True, creationflags=NO_WINDOW)
        self.proc = None
        return f'{self.name}: stopped'


def bridge_cmd(o):
    cmd = [PYTHON, '-u', 'bridge/mt5_bridge.py', '--port', '8765', '--attach-only',
           '--max-lots', str(o.get('max_lots', 0.03))]
    if o.get('arm'):
        cmd.append('--enable-trading')
    return cmd


SERVICES = [
    Service('Bridge', 8765, 'http://127.0.0.1:8765/health', bridge_cmd, ROOT),
    Service('API', 8770, 'http://127.0.0.1:8770/api/health',
            lambda o: [PYTHON, '-u', '-m', 'server.main', '--port', '8770'], ROOT),
    Service('UI', 5180, None,
            lambda o: ['cmd', '/c', 'npm', 'run', 'dev'], ROOT / 'web'),
]


def main() -> int:
    import tkinter as tk
    from tkinter import messagebox

    prefs = load_prefs()
    root = tk.Tk()
    root.title('DiaNurFx launcher')
    root.configure(bg='#0a0f1c')
    root.resizable(False, False)

    FG, DIM, BG, PANEL = '#e8eef6', '#8a94a6', '#0a0f1c', '#101a2e'
    UP, DOWN, WARN, BRAND = '#9fe547', '#ff2d78', '#ffb02e', '#e31c79'

    head = tk.Frame(root, bg=BG)
    head.pack(fill='x', padx=16, pady=(14, 6))
    tk.Label(head, text='DIANUR', fg=FG, bg=BG, font=('Segoe UI', 16, 'bold')).pack(side='left')
    tk.Label(head, text='FX', fg=BRAND, bg=BG, font=('Segoe UI', 16, 'bold')).pack(side='left')
    tk.Label(head, text='  workstation launcher', fg=DIM, bg=BG,
             font=('Segoe UI', 10)).pack(side='left', pady=(6, 0))

    rows = tk.Frame(root, bg=PANEL, padx=12, pady=10)
    rows.pack(fill='x', padx=16, pady=6)
    lights = {}
    for i, svc in enumerate(SERVICES):
        dot = tk.Label(rows, text='●', fg=DIM, bg=PANEL, font=('Segoe UI', 13))
        dot.grid(row=i, column=0, padx=(0, 8))
        tk.Label(rows, text=svc.name, fg=FG, bg=PANEL, width=8, anchor='w',
                 font=('Segoe UI', 10, 'bold')).grid(row=i, column=1, sticky='w')
        state = tk.Label(rows, text='-', fg=DIM, bg=PANEL, width=26, anchor='w',
                         font=('Consolas', 9))
        state.grid(row=i, column=2, sticky='w')
        tk.Label(rows, text=f':{svc.port}', fg=DIM, bg=PANEL,
                 font=('Consolas', 9)).grid(row=i, column=3, padx=(8, 0))
        lights[svc.name] = (dot, state)

    opts = tk.Frame(root, bg=BG)
    opts.pack(fill='x', padx=16, pady=(6, 0))
    arm = tk.BooleanVar(value=False)                  # never remembered
    tk.Checkbutton(opts, text='Arm trading (bridge --enable-trading)', variable=arm,
                   fg=WARN, bg=BG, selectcolor=PANEL, activebackground=BG,
                   activeforeground=WARN, font=('Segoe UI', 9, 'bold')).pack(anchor='w')
    lots_row = tk.Frame(opts, bg=BG)
    lots_row.pack(anchor='w', pady=(4, 0))
    tk.Label(lots_row, text='Bridge lot ceiling', fg=DIM, bg=BG,
             font=('Segoe UI', 9)).pack(side='left')
    max_lots = tk.StringVar(value=str(prefs.get('max_lots', 0.03)))
    tk.Entry(lots_row, textvariable=max_lots, width=6, bg=PANEL, fg=FG,
             insertbackground=FG, relief='flat').pack(side='left', padx=6)
    tk.Label(opts, text='Demo accounts only - the demo guard stays on. Auto mode, lots, exits '
                        'and timeframes are set in the web UI.', fg=DIM, bg=BG, wraplength=360,
             justify='left', font=('Segoe UI', 8)).pack(anchor='w', pady=(4, 0))

    msg = tk.StringVar(value='')
    tk.Label(root, textvariable=msg, fg=DIM, bg=BG, wraplength=380, justify='left',
             font=('Consolas', 8)).pack(fill='x', padx=16, pady=(8, 0))

    def current_opts() -> dict:
        try:
            ml = float(max_lots.get())
        except ValueError:
            ml = 0.03
        ml = min(5.0, max(0.01, ml))
        return {'arm': bool(arm.get()), 'max_lots': ml}

    def run_bg(fn):
        threading.Thread(target=fn, daemon=True).start()

    def start_all():
        o = current_opts()
        save_prefs({'max_lots': o['max_lots']})
        if o['arm'] and not messagebox.askyesno(
                'Arm trading?',
                'Start the bridge ARMED?\n\nWith auto mode on in the workstation, FINAL '
                'qualified signals will be sent to MT5 (demo accounts only).'):
            return

        def go():
            out = []
            for svc in SERVICES:
                out.append(svc.start(o))
                if svc.name == 'Bridge':
                    time.sleep(3)         # let it attach before the API asks it things
            msg.set('\n'.join(out))
            for _ in range(60):           # open the page once the UI answers
                if SERVICES[2].running():
                    time.sleep(1.5)
                    webbrowser.open(UI_URL)
                    break
                time.sleep(1)
        run_bg(go)

    def stop_all():
        def go():
            msg.set('\n'.join(svc.stop() for svc in reversed(SERVICES)))
        run_bg(go)

    btns = tk.Frame(root, bg=BG)
    btns.pack(fill='x', padx=16, pady=12)
    for text, cmd, color in (('Start all', start_all, UP),
                             ('Stop all', stop_all, DOWN),
                             ('Open workstation', lambda: webbrowser.open(UI_URL), '#35d6ef')):
        tk.Button(btns, text=text, command=cmd, bg=PANEL, fg=color, activebackground=PANEL,
                  activeforeground=color, relief='flat', padx=14, pady=6,
                  font=('Segoe UI', 9, 'bold')).pack(side='left', padx=(0, 8))
    tk.Label(root, text=f'logs: {LOGS}', fg=DIM, bg=BG,
             font=('Consolas', 7)).pack(anchor='w', padx=16, pady=(0, 10))

    def poll():
        def check():
            res = {}
            for svc in SERVICES:
                if not svc.running():
                    res[svc.name] = (DIM, 'stopped')
                elif svc.healthy():
                    detail = 'running'
                    if svc.name == 'Bridge':
                        try:
                            with urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=1.5) as r:
                                h = json.loads(r.read().decode('utf-8'))
                            armed = (h.get('execution') or {}).get('trading_enabled')
                            detail = (f'{h.get("account_type") or "?"} '
                                      f'{h.get("login") or ""}  '
                                      f'{"ARMED" if armed else "read-only"}')
                        except Exception:                        # noqa: BLE001
                            pass
                    res[svc.name] = (UP, detail)
                else:
                    res[svc.name] = (WARN, 'starting / not healthy')
            root.after(0, lambda: [(lights[n][0].config(fg=c), lights[n][1].config(text=t))
                                   for n, (c, t) in res.items()])
        run_bg(check)
        root.after(2000, poll)

    def on_close():
        mine = [s for s in SERVICES if s.proc is not None and s.proc.poll() is None]
        if mine:
            ans = messagebox.askyesnocancel(
                'Close launcher', 'Stop the services this launcher started?\n\n'
                'Yes: stop them.  No: leave them running.')
            if ans is None:
                return
            if ans:
                for s in reversed(mine):
                    s.stop()
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', on_close)
    poll()
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
