"""
python -m server.lab [--port 8771] - run the backtest lab's server.

Normally started for you: the live API's POST /api/lab/start launches it the
first time the BACKTEST tab is opened. It is a separate process on purpose -
see server/lab/__init__.py.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description='DiaNurFx backtest lab')
    ap.add_argument('--port', type=int, default=8771)
    a = ap.parse_args()
    import uvicorn

    from .app import app
    pid = Path(__file__).resolve().parents[2] / 'runs' / 'lab' / 'lab.pid'
    pid.parent.mkdir(parents=True, exist_ok=True)
    pid.write_text(str(os.getpid()), encoding='utf-8')
    print(f'DiaNurFx lab on http://127.0.0.1:{a.port}  (pid {os.getpid()})', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=a.port, log_level='warning')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
