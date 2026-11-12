#!/usr/bin/env python
"""
tools/build_playbook_docs.py - docs/playbooks.html, the beginner's guide to every playbook.

    python tools/build_playbook_docs.py

Reads web/src/data/playbooks.json (the same text the Settings -> Playbooks
hover cards show) and writes one self-contained HTML page: a schematic chart
per playbook drawn as inline SVG from the JSON's 'diagram' spec, the rule in
plain words, the shared exit rules and a glossary. No dependencies.

Diagram spec (prices on a 0-100 scale, x in bars):
    path     [[bar, close], ...] waypoints; closes in between are interpolated
             with a little deterministic noise so it looks like a chart
    candles  [{i, o, h, l, c}] bars drawn exactly as given (pin bars, etc.)
    boxes    [{x1, x2, y1, y2, kind, label}]   kind: range|ob|fvg|squeeze|fib
    lines    [{y, label, style}]                style: level|dash|dot
    segs     [{x1, y1, x2, y2, label}]          trendlines, channels, EMAs
    vlines   [{x, label}]                       a moment (news release)
    marks    [{x, y, text}]                     small call-outs
    trade    {x, side, entry, stop, target}     the order
Rerun after editing the JSON.
"""
from __future__ import annotations

import hashlib
import html
import json
import random
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'web' / 'src' / 'data' / 'playbooks.json'
OUT = ROOT / 'docs' / 'playbooks.html'

W, H = 600, 270
PAD_L, PAD_R, PAD_T, PAD_B = 12, 112, 18, 18
BARS = 46                     # x scale: bars 0..BARS


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def candles_for(pid: str, d: dict) -> list:
    path = sorted(d.get('path') or [], key=lambda p: p[0])
    over = {int(c['i']): c for c in d.get('candles') or []}
    last = max([p[0] for p in path] + list(over) or [0])
    rnd = random.Random(int(hashlib.md5(pid.encode()).hexdigest()[:8], 16))
    closes = {}
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        for i in range(int(x0), int(x1) + 1):
            f = 0 if x1 == x0 else (i - x0) / (x1 - x0)
            closes[i] = y0 + (y1 - y0) * f
    out = []
    prev = path[0][1] if path else 50
    for i in range(0, int(last) + 1):
        if i in over:
            c = over[i]
            out.append((i, c['o'], c['h'], c['l'], c['c']))
            prev = c['c']
            continue
        base = closes.get(i, prev)
        cl = base + (rnd.uniform(-1.4, 1.4) if i not in [int(p[0]) for p in path] else 0)
        op = prev
        hi = max(op, cl) + abs(rnd.gauss(0, 1.1))
        lo = min(op, cl) - abs(rnd.gauss(0, 1.1))
        out.append((i, op, hi, lo, cl))
        prev = cl
    return out


def svg_for(p: dict) -> str:
    d = p.get('diagram') or {}
    cs = candles_for(p['id'], d)
    ys = [v for c in cs for v in (c[2], c[3])]
    for b in d.get('boxes') or []:
        ys += [b['y1'], b['y2']]
    for ln in d.get('lines') or []:
        ys.append(ln['y'])
    for s in d.get('segs') or []:
        ys += [s['y1'], s['y2']]
    t = d.get('trade') or {}
    ys += [v for k, v in t.items() if k in ('entry', 'stop', 'target')]
    for m in d.get('marks') or []:
        ys.append(m['y'])
    lo, hi = min(ys) - 3, max(ys) + 3

    def X(b):
        return PAD_L + (W - PAD_L - PAD_R) * b / BARS

    def Y(v):
        return PAD_T + (H - PAD_T - PAD_B) * (hi - v) / (hi - lo)

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(p["title"])} diagram" '
             f'class="dia" xmlns="http://www.w3.org/2000/svg">',
             f'<rect x="0" y="0" width="{W}" height="{H}" rx="8" class="dia-bg"/>']
    for b in d.get('boxes') or []:
        x1, x2 = X(b['x1']), X(b['x2'])
        y1, y2 = Y(max(b['y1'], b['y2'])), Y(min(b['y1'], b['y2']))
        k = b.get('kind', 'range')
        parts.append(f'<rect x="{x1:.1f}" y="{y1:.1f}" width="{x2 - x1:.1f}" height="{y2 - y1:.1f}" '
                     f'class="box box-{k}"/>')
        if b.get('label'):
            parts.append(f'<text x="{x1 + 4:.1f}" y="{y1 - 4:.1f}" class="lbl lbl-{k}">{esc(b["label"])}</text>')
    for ln in d.get('lines') or []:
        y = Y(ln['y'])
        st = ln.get('style', 'level')
        parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R + 6}" y1="{y:.1f}" y2="{y:.1f}" class="ln ln-{st}"/>')
        parts.append(f'<text x="{W - PAD_R + 9}" y="{y + 3.5:.1f}" class="lbl">{esc(ln["label"])}</text>')
    for s in d.get('segs') or []:
        parts.append(f'<line x1="{X(s["x1"]):.1f}" y1="{Y(s["y1"]):.1f}" x2="{X(s["x2"]):.1f}" '
                     f'y2="{Y(s["y2"]):.1f}" class="seg"/>')
        if s.get('label'):
            parts.append(f'<text x="{X(s["x2"]) + 4:.1f}" y="{Y(s["y2"]) + 3:.1f}" class="lbl">{esc(s["label"])}</text>')
    for v in d.get('vlines') or []:
        x = X(v['x'])
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{PAD_T}" y2="{H - PAD_B}" class="ln ln-news"/>')
        parts.append(f'<text x="{x + 4:.1f}" y="{PAD_T + 10}" class="lbl lbl-news">{esc(v["label"])}</text>')
    cw = (W - PAD_L - PAD_R) / BARS * 0.62
    for i, o, h_, l_, c in cs:
        x = X(i)
        up = c >= o
        cls = 'up' if up else 'dn'
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{Y(h_):.1f}" y2="{Y(l_):.1f}" class="wick {cls}"/>')
        top, bot = Y(max(o, c)), Y(min(o, c))
        parts.append(f'<rect x="{x - cw / 2:.1f}" y="{top:.1f}" width="{cw:.1f}" '
                     f'height="{max(bot - top, 1.2):.1f}" class="body {cls}"/>')
    for m in d.get('marks') or []:
        parts.append(f'<text x="{X(m["x"]):.1f}" y="{Y(m["y"]):.1f}" class="mark" text-anchor="middle">'
                     f'{esc(m["text"])}</text>')
    if t:
        x0, x1 = X(t['x']), W - PAD_R + 6
        for key, cls, name in (('target', 'tgt', 'Target'), ('entry', 'ent', 'Entry'), ('stop', 'stp', 'Stop')):
            y = Y(t[key])
            parts.append(f'<line x1="{x0:.1f}" x2="{x1:.1f}" y1="{y:.1f}" y2="{y:.1f}" class="rail {cls}"/>')
            parts.append(f'<text x="{x1 + 3:.1f}" y="{y + 3.5:.1f}" class="rl {cls}">{name}</text>')
        buy = t['side'] == 'buy'
        ye = Y(t['entry'])
        ax = X(t['x'])
        if buy:
            parts.append(f'<path d="M{ax:.1f} {ye + 9:.1f} l-5 8 h10 z" class="arrow ent"/>')
        else:
            parts.append(f'<path d="M{ax:.1f} {ye - 9:.1f} l-5 -8 h10 z" class="arrow ent"/>')
        parts.append(f'<text x="{ax:.1f}" y="{(ye + 30) if buy else (ye - 22):.1f}" class="side {cls}" '
                     f'text-anchor="middle">{"BUY" if buy else "SELL"}</text>')
    parts.append('</svg>')
    return ''.join(parts)


CSS = """
:root{--bg:#f5f6f8;--card:#fff;--ink:#16202e;--ink2:#4a5568;--ink3:#8592a6;--line:#dfe3ea;
--up:#2e9e5b;--dn:#d6336c;--amber:#c47d00;--cyan:#0b8fb3;--violet:#7c4dcc;--dia:#fbfcfd;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#070b14;--card:#0d1422;--ink:#dbe3f0;
--ink2:#a3b0c4;--ink3:#64728a;--line:#1d2638;--up:#9fe547;--dn:#ff2d78;--amber:#ffb02e;--cyan:#35d6ef;
--violet:#c264ff;--dia:#080c16;--chip:#141c2e}}
:root[data-theme="dark"]{--bg:#070b14;--card:#0d1422;--ink:#dbe3f0;--ink2:#a3b0c4;--ink3:#64728a;--line:#1d2638;
--up:#9fe547;--dn:#ff2d78;--amber:#ffb02e;--cyan:#35d6ef;--violet:#c264ff;--dia:#080c16;--chip:#141c2e}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:40px 16px 20px;max-width:1100px;margin:0 auto}
h1{font-size:30px;margin:0 0 6px;letter-spacing:-.01em}h1 span{color:var(--cyan)}
.sub{color:var(--ink2);max-width:760px;margin:0}
.warn{margin:16px 0 0;padding:10px 14px;border:1px solid var(--amber);border-radius:8px;color:var(--ink);max-width:760px;font-size:14px}
main{max-width:1100px;margin:0 auto;padding:0 16px 60px}
nav.toc{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 24px}
nav.toc a{font-size:12.5px;text-decoration:none;color:var(--ink2);background:var(--chip);border:1px solid var(--line);
border-radius:999px;padding:3px 10px}nav.toc a:hover{color:var(--ink);border-color:var(--ink3)}
.search{width:100%;max-width:420px;padding:9px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);
color:var(--ink);font:inherit;margin:0 0 10px}
h2{font-size:22px;margin:42px 0 4px;padding-top:6px}h2 small{font-size:14px;color:var(--ink3);font-weight:400;margin-left:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,500px),1fr));gap:16px;margin-top:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;scroll-margin-top:12px}
.card h3{margin:0 0 2px;font-size:17px}.card .id{font:12px ui-monospace,Consolas,monospace;color:var(--ink3)}
.card .idea{margin:8px 0 10px;color:var(--ink)}
.dia{width:100%;height:auto;display:block;margin:6px 0 10px}
.dia-bg{fill:var(--dia);stroke:var(--line)}
.wick{stroke-width:1}.wick.up{stroke:var(--up)}.wick.dn{stroke:var(--dn)}
.body.up{fill:var(--up)}.body.dn{fill:var(--dn)}
.box{stroke-width:1}.box-range{fill:var(--cyan);fill-opacity:.08;stroke:var(--cyan)}
.box-ob{fill:var(--violet);fill-opacity:.12;stroke:var(--violet)}.box-fvg{fill:var(--amber);fill-opacity:.12;stroke:var(--amber);stroke-dasharray:4 3}
.box-squeeze{fill:var(--ink3);fill-opacity:.12;stroke:var(--ink3);stroke-dasharray:3 3}.box-fib{fill:var(--cyan);fill-opacity:.1;stroke:none}
.ln{stroke:var(--ink3);stroke-width:1}.ln-level{stroke:var(--amber);stroke-width:1.4}.ln-dash{stroke-dasharray:5 4}
.ln-dot{stroke-dasharray:2 3}.ln-news{stroke:var(--violet);stroke-dasharray:4 3}
.seg{stroke:var(--cyan);stroke-width:1.4}
.lbl{font:10.5px system-ui,sans-serif;fill:var(--ink2)}.lbl-range{fill:var(--cyan)}.lbl-ob{fill:var(--violet)}
.lbl-fvg{fill:var(--amber)}.lbl-news{fill:var(--violet)}
.mark{font:600 10.5px system-ui,sans-serif;fill:var(--ink)}
.rail{stroke-width:1.5}.rail.ent{stroke:var(--amber)}.rail.stp{stroke:var(--dn);stroke-dasharray:5 3}.rail.tgt{stroke:var(--up);stroke-dasharray:5 3}
.rl{font:600 10.5px system-ui,sans-serif}.rl.ent{fill:var(--amber)}.rl.stp{fill:var(--dn)}.rl.tgt{fill:var(--up)}
.arrow{fill:var(--amber)}.side{font:700 10px system-ui,sans-serif;fill:var(--amber)}
dl.rule{display:grid;grid-template-columns:62px 1fr;gap:4px 10px;margin:0;font-size:14px}
dl.rule dt{font-weight:600;color:var(--ink2)}dl.rule dd{margin:0}
.tip{margin:10px 0 0;font-size:13.5px;color:var(--ink2);border-left:3px solid var(--cyan);padding:2px 0 2px 10px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-top:14px}
.panel ul{margin:6px 0;padding-left:20px}.gloss{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,320px),1fr));gap:10px 22px}
.gloss b{display:block}.gloss p{margin:2px 0 0;color:var(--ink2);font-size:14px}
footer{color:var(--ink3);font-size:12.5px;text-align:center;padding:20px}
.hide{display:none}
"""

GLOSSARY = [
    ('ATR (Average True Range)', 'The average size of a candle over the last 14 bars. Stops, pads and distances are measured in ATR so the same rule works in quiet and busy markets.'),
    ('R (risk unit)', 'The distance from entry to stop. A trade that makes 2R made twice what it risked.'),
    ('Swing high / swing low', 'A peak (or trough) with lower highs (or higher lows) on both sides. The building block of market structure.'),
    ('HH, HL, LH, LL', 'Higher high, higher low (an uptrend); lower high, lower low (a downtrend).'),
    ('BOS - break of structure', 'A close through the latest swing in the trend\'s direction: the trend continues.'),
    ('CHoCH - change of character', 'A close through the protected level AGAINST the trend: the first sign the trend may be turning.'),
    ('Protected level', 'The swing a trend must not break: in an uptrend, the low the last rally started from.'),
    ('Liquidity', 'Clusters of stop orders, usually just beyond equal highs / lows and obvious swings.'),
    ('Sweep', 'A wick runs through liquidity and the candle closes back - the stops were taken but price did not stay.'),
    ('Order block (OB)', 'The last opposite candle before a move that broke structure - where large orders are thought to have been placed.'),
    ('Fair value gap (FVG)', 'Three candles where the first and third do not overlap: a gap only one side traded through. Price often returns to it.'),
    ('Two-close breakout', 'The rule every breakout here uses: two closes in a row more than 0.3 ATR beyond the level. One close, or a wick, is not enough.'),
    ('Retest', 'After a breakout, price comes back to the level it broke. Broken resistance often acts as support.'),
    ('Range types', 'Horizontal (flat), ascending (both edges rising), descending (both falling), contracting (narrowing) and expanding (widening).'),
    ('EMA', 'Exponential moving average. Here the fast one is 21 bars and the slow one 55.'),
    ('Bollinger squeeze', 'Bollinger Bands (20, 2) unusually narrow: the market is quiet, which usually ends in a sharp move.'),
    ('RSI divergence', 'Price makes a new extreme but RSI does not - the move is losing momentum.'),
    ('NR4', 'Narrow range 4: the smallest candle of the last four. A sign of a pause before the next move.'),
]


def build() -> str:
    data = json.loads(SRC.read_text(encoding='utf-8'))
    pbs = data['playbooks']
    groups = data['groups']
    toc = ''.join(f'<a href="#g-{g["key"]}">{esc(g["title"])}</a>' for g in groups)
    toc += '<a href="#common">Rules every playbook shares</a><a href="#glossary">Glossary</a>'
    body = []
    for g in groups:
        items = [p for p in pbs if p['group'] == g['key']]
        cards = []
        for p in items:
            cards.append(
                f'<article class="card" id="{esc(p["id"])}" data-q="{esc((p["title"] + " " + p["id"] + " " + p["idea"]).lower())}">'
                f'<h3>{esc(p["title"])}</h3><div class="id">{esc(p["id"])}</div>'
                f'<p class="idea">{esc(p["idea"])}</p>{svg_for(p)}'
                f'<dl class="rule"><dt>When</dt><dd>{esc(p["when"])}</dd>'
                f'<dt>Entry</dt><dd>{esc(p["entry"])}</dd><dt>Stop</dt><dd>{esc(p["stop"])}</dd>'
                f'<dt>Target</dt><dd>{esc(p["target"])}</dd></dl>'
                + (f'<p class="tip">{esc(p["tip"])}</p>' if p.get('tip') else '') + '</article>')
        body.append(f'<section id="g-{g["key"]}"><h2>{esc(g["title"])}<small>{esc(g["hint"])} - '
                    f'{len(items)} playbooks</small></h2><div class="grid">{"".join(cards)}</div></section>')
    common = """
<section id="common"><h2>Rules every playbook shares</h2><div class="panel"><ul>
<li><b>Closed bars only.</b> A setup is judged when its bar closes; a market entry is at that close.</li>
<li><b>TP1 is 1R.</b> TP2 is the playbook's own target when it is between 1.2R and 6R away, otherwise 2R.</li>
<li><b>Stops sit beyond structure</b> (a swing, a level, a zone, a wick) plus a small ATR pad, then are widened to the noise floor (Risk &amp; gates: minimum stop in ATR) if too tight.</li>
<li><b>The exit</b> follows Risk &amp; gates: by default, at TP1 the stop locks in a small profit and then trails by 1 ATR on closed bars.</li>
<li><b>A signal is not a trade.</b> Every signal still passes the gates (confidence, spread, ATR cost, news, daily limits, room to target) before it can be sent.</li>
<li><b>On / off</b> is per playbook in Settings &rarr; Playbooks. New playbooks start <b>OFF</b> and have not been backtested - test one in the Backtest lab before switching it on for live.</li>
</ul></div></section>"""
    gloss = '<section id="glossary"><h2>Glossary</h2><div class="panel gloss">' + ''.join(
        f'<div><b>{esc(k)}</b><p>{esc(v)}</p></div>' for k, v in GLOSSARY) + '</div></section>'
    script = """<script>
const q=document.getElementById('q');q.addEventListener('input',()=>{const v=q.value.trim().toLowerCase();
document.querySelectorAll('.card').forEach(c=>c.classList.toggle('hide',!!v&&!c.dataset.q.includes(v)));
document.querySelectorAll('section[id^="g-"]').forEach(s=>s.classList.toggle('hide',!!v&&!s.querySelector('.card:not(.hide)')));});
</script>"""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>DiaNurFx Playbooks</title>
<style>{CSS}</style></head><body>
<header><h1>DiaNurFx <span>Playbooks</span></h1>
<p class="sub">Every trading rule the signal engine can run - what it looks for, where it enters, where the stop and target go - each with a schematic chart. The diagrams are illustrations, not real prices.</p>
<p class="warn">The {len(pbs) - 8} new playbooks (added {date.today():%d %b %Y}) are switched OFF and have not been backtested. Test each one in the Backtest lab before switching it on for live trading.</p>
</header><main><input id="q" class="search" type="search" placeholder="Search playbooks (e.g. sweep, FVG, retest)">
<nav class="toc">{toc}</nav>{''.join(body)}{common}{gloss}</main>
<footer>Built by tools/build_playbook_docs.py from web/src/data/playbooks.json.</footer>{script}</body></html>
"""


if __name__ == '__main__':
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(), encoding='utf-8')
    print(f'wrote {OUT.relative_to(ROOT)}')
