"""
server/lab/challenge.py - Phase E: the AI supervisor challenges the forecast.

The engine has already produced every number on screen: the range cone, the
regime and direction odds with their intervals and confidence labels, the
similar past moments, the trade odds, the gates. This module asks the other
question - where is that read weak, and where does it stop being true?

It follows server/llm.py's two rules and adds a third:

  1. THE MODEL NEVER COMPUTES. It gets a fact sheet built from the forecast
     payload and writes the challenge from it. The mechanical findings (stop
     inside the typical adverse move, regime against the side, a release
     inside the horizon...) are computed HERE, by rules, and handed to it.
  2. THE MODEL IS OPTIONAL. Without a key, or on any failure, the same
     findings come back as a rule-written challenge, labelled as such.
  3. EVERY NUMBER IS TRACED. Each number in the model's reply must be one
     the fact sheet contains (rounding allowed). A reply with a number the
     engine did not produce is withheld - the rule-written challenge is shown
     instead, with the untraced numbers named.

On demand only (a button in the lab), never per bar. Measurements, not advice.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import CONFIG, TF_SECONDS
from ..forecast.reads import STATES

ROOT = Path(__file__).resolve().parents[2]
MODEL = 'claude-sonnet-5'
MAX_TOKENS = 700
ODDS_HORIZON = '72 x 5m'


def _pct(p) -> str:
    return f'{100 * float(p):.0f}%'


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _subject(frame: dict, signal_id: str = None):
    """The signal to challenge: the one asked for, else the first live or qualified one."""
    sigs = frame.get('signals') or []
    if signal_id:
        return next((s for s in sigs if s.get('id') == signal_id), None)
    for want in (lambda s: s.get('stage') in ('SENT', 'FILLED'),
                 lambda s: s.get('status') == 'qualified'):
        hit = next((s for s in sigs if want(s)), None)
        if hit:
            return hit
    return None


def _filtertest(tf: str) -> str | None:
    base = ROOT / 'runs' / 'forecast' / 'filtertest'
    try:
        runs = sorted((p for p in base.iterdir() if (p / 'results.json').exists()),
                      key=lambda p: (p / 'results.json').stat().st_mtime)
        doc = json.loads((runs[-1] / 'results.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, IndexError):
        return None
    v = (doc.get('verdicts') or {}).get(tf)
    if not v:
        return None
    passed = [k for k, x in v.items() if x == 'PASS']
    return (f"Phase D filter test on {tf}: " + ', '.join(f'{k} {x}' for k, x in v.items())
            + ('. A pass there is marginal and is not used to filter live trades.' if passed
               else '. No forecast filter improved results in every year.'))


def _news_reaction(nr: dict | None) -> str | None:
    """The payload's reaction line - releases resolved before this bar only."""
    if not nr:
        return None
    return (f"Around the {nr['n']} {nr['kind']} releases resolved before this bar, the range over "
            f"the next horizon ran a median {nr['x']:.2f}x the same hour without a release "
            f"(middle half {nr['x_p25']:.2f}x-{nr['x_p75']:.2f}x); {_pct(nr['p_up'])} of those "
            "horizons closed up - a measured share, not a tested direction forecast.")


def build(fc: dict, frame: dict, digits: int = 2, signal_id: str = None,
          news_minutes=None) -> dict:
    """
    The fact sheet and the rule findings for the bar on screen.
    fc is SessionForecast.payload(); frame the session frame (signals, positions).
    """
    facts, checks, invalid = [], [], []
    tf = fc.get('tf')
    if not fc.get('available'):
        return {'facts': [f"No forecast for this bar: {fc.get('reason') or 'unavailable'}."],
                'checks': [], 'invalid': [], 'subject': None}
    H, close, atr = int(fc['horizon']), float(fc['close']), float(fc['atr'])
    fmt = f'{{:.{digits}f}}'
    px = lambda x: fmt.format(x)                                    # noqa: E731
    facts.append(f"Timeframe {tf}; the bar closed {_utc(fc['close_ms'])}; close {px(close)}; "
                 f"ATR {px(atr)}. Horizon: the next {H} bars.")

    # ---- range cone: the one the lab draws
    promoted = bool(fc.get('promoted'))
    up = fc['up'][-1] if promoted else fc['base_up'][-1]
    dn = fc['dn'][-1] if promoted else fc['base_dn'][-1]
    who = 'the promoted range model' if promoted else \
        'the baseline (the range model is not promoted on this timeframe)'
    facts.append(f"Range forecast from {who}, over the next {H} bars: "
                 f"upside P20/P50/P80 {up[0]:.2f}/{up[1]:.2f}/{up[2]:.2f} ATR "
                 f"(to {px(close + up[0] * atr)} / {px(close + up[1] * atr)} / {px(close + up[2] * atr)}); "
                 f"downside {dn[0]:.2f}/{dn[1]:.2f}/{dn[2]:.2f} ATR "
                 f"(to {px(close - dn[0] * atr)} / {px(close - dn[1] * atr)} / {px(close - dn[2] * atr)}).")
    if promoted:
        facts.append(f"The forecast range is {fc['ratio']:.2f}x the baseline range for this hour.")
    cell = fc.get('cell') or {}
    kind, mins = cell.get('news_next'), cell.get('news_next_min')
    hmin = H * TF_SECONDS.get(tf, 300) / 60
    news_in = bool(kind and mins is not None and mins <= hmin)
    if news_in:
        facts.append(f"A major US release ({kind}) is due in {mins:.0f} minutes - inside the horizon.")
        r = _news_reaction(fc.get('news'))
        if r:
            facts.append(r)
        checks.append(f"A {kind} release lands inside the horizon: ranges run wider around "
                      "it, so both the stop and the target are more likely to be reached.")
    elif kind and mins is not None and mins < 1440:
        facts.append(f"Next major US release: {kind} in {mins / 60:.1f} hours (outside the horizon).")
    if news_minutes is not None:
        facts.append(f"The session's news gate reads {news_minutes:.0f} minutes to the next "
                     f"high-impact release (live blocks sends inside "
                     f"{CONFIG.gates.news_blackout_min} minutes).")
    if cell.get('session'):
        facts.append(f"Session: {cell['session']}; volatility: {cell.get('vol_name', 'n/a')}.")

    # ---- regime
    rg = fc.get('regime')
    if rg:
        top = int(rg['top'])
        facts.append(f"Regime in {H} bars: {STATES[top]} is likeliest at {_pct(rg['p'][top])} "
                     f"[{_pct(rg['lo'])}-{_pct(rg['hi'])}], baseline {_pct(rg['base'][top])}; "
                     f"confidence {rg.get('confidence')}; passed its promotion gate: "
                     f"{'yes' if rg.get('promoted') else 'no'}.")
        facts.append('Regime probabilities: ' + ', '.join(
            f'{STATES[i]} {_pct(p)}' for i, p in enumerate(rg['p'])) + '.')
        if STATES[top] == 'TRANSITION':
            checks.append('TRANSITION is the likeliest regime ahead - the structure a setup '
                          'relies on may not hold over the horizon.')
    # ---- direction
    dr = fc.get('direction')
    if dr:
        facts.append(f"P(close higher after {H} bars) {_pct(dr['p'][-1])} [{_pct(dr['lo'])}-"
                     f"{_pct(dr['hi'])}], baseline {_pct(dr['base'][-1])}; confidence "
                     f"{dr.get('confidence')}; direction forecasts "
                     + ('passed their promotion gate.' if dr.get('promoted') else
                        'did NOT pass their promotion gate - testing found no directional edge.'))
        if not dr.get('promoted'):
            checks.append(f"Direction has no promoted forecast: the {_pct(dr['p'][-1])} is not "
                          "evidence for either side.")
    an = fc.get('analogs')
    if an and an.get('n_eff'):
        facts.append(f"Similar past moments ({', '.join(an.get('why') or [])}): {an['n_eff']} "
                     f"independent cases from {an['n']} matches; {_pct(an['p_up'])} closed higher; "
                     f"median move up {an['up_med']:.2f} ATR, down {an['dn_med']:.2f} ATR; versus the "
                     f"direction table: {an.get('agreement', 'n/a')}.")
        if an.get('agreement') == 'disagree':
            checks.append('The similar past moments point the other way from the direction table.')
    ag = fc.get('agreement')
    if ag:
        facts.append('Direction read by timeframe: ' + ', '.join(
            f'{t} {_pct(p)} up' for t, p in ag['rows']) + f" - {ag['label']}.")
    ft = _filtertest(tf)
    if ft:
        facts.append(ft)

    # ---- the signal
    s = _subject(frame, signal_id)
    subject = None
    if s:
        side, buy = s['side'].upper(), s['side'] == 'buy'
        entry, stop, tp1, tp2 = (float(s[k]) for k in ('entry', 'stop', 'tp1', 'tp2'))
        # Distances from the CLOSE - where the cone starts - whatever the entry was.
        stop_atr = ((close - stop) if buy else (stop - close)) / atr
        tp1_atr = ((tp1 - close) if buy else (close - tp1)) / atr
        stage = s.get('stage') or s.get('status')
        subject = {'id': s.get('id'), 'side': s['side'], 'label': s.get('label') or s.get('playbook')}
        facts.append(f"Signal under review: {side} {subject['label']}, entry {px(entry)}, stop "
                     f"{px(stop)}, TP1 {px(tp1)}, TP2 {px(tp2)}; status {stage}; detection "
                     f"confidence {s.get('confidence')}. From the close {px(close)}: the stop is "
                     f"{stop_atr:.2f} ATR away, TP1 {tp1_atr:.2f} ATR away.")
        warns = [g['detail'] for g in s.get('gates') or [] if g.get('verdict') in ('WARN', 'BLOCK')]
        if warns:
            facts.append('Gate warnings the engine recorded: ' + '; '.join(warns[:4]) + '.')
        if s.get('against'):
            facts.append('Counter-evidence the engine recorded: ' + '; '.join(
                str(a) for a in s['against'][:4]) + '.')
        if s.get('invalidation'):
            facts.append(f"The playbook's own invalidation: {s['invalidation']}.")
        trades = fc.get('trades') or []
        if stage == 'FILLED':
            # An open trade is judged from where price is now: its own position row.
            row = next((t for t in trades if t.get('kind') == 'position'
                        and t.get('side') == s['side']), None)
            o, what = (row or {}).get('tp') or {}, 'for the open position from here, to its broker TP'
        else:
            row = next((t for t in trades if t.get('id') == s.get('id')), None)
            o, what = (row or {}).get('tp1') or {}, "to TP1 as a fresh entry, at this bar's cost"
        if o.get('target') is not None:
            txt = (f"Odds {what}, within {ODDS_HORIZON} (baseline): target first "
                   f"{_pct(o['target'])}, stop first {_pct(o['stop'])}, neither {_pct(o['neither'])}")
            m = o.get('model')
            if m:
                txt += (f"; with the conditions' lift {_pct(m['target'])} / {_pct(m['stop'])} "
                        f"({'promoted' if m.get('promoted') else 'not promoted'})")
            facts.append(txt + '.')
        adv, fav = (dn, up) if buy else (up, dn)
        adv_side, adv_word = ('downside', 'below') if buy else ('upside', 'above')
        if stop_atr <= 0:
            checks.append(f'Price has already closed through the stop ({px(stop)}).')
        elif stop_atr < adv[1]:
            checks.append(f"The stop ({stop_atr:.2f} ATR from the close) is inside the median "
                          f"{adv_side} move the range forecast expects over the horizon "
                          f"({adv[1]:.2f} ATR).")
        elif stop_atr < adv[2]:
            checks.append(f"The stop ({stop_atr:.2f} ATR from the close) is past the median "
                          f"{adv_side} move ({adv[1]:.2f} ATR) but inside its P80 ({adv[2]:.2f} ATR).")
        if tp1_atr <= 0:
            checks.append(f'Price is already past TP1 ({px(tp1)}).')
        elif tp1_atr > fav[2]:
            checks.append(f"TP1 ({tp1_atr:.2f} ATR from the close) is beyond the P80 favourable "
                          f"move ({fav[2]:.2f} ATR) for this horizon - it needs more than the "
                          "horizon's usual range.")
        elif tp1_atr > fav[1]:
            checks.append(f"TP1 ({tp1_atr:.2f} ATR from the close) is beyond the median favourable "
                          f"move ({fav[1]:.2f} ATR) for this horizon.")
        if rg:
            name = STATES[int(rg['top'])]
            against = (name == 'DOWNTREND' and s['side'] == 'buy') or \
                (name == 'UPTREND' and s['side'] == 'sell')
            if against and rg.get('confidence') != 'baseline':
                checks.append(f"The likeliest regime ahead ({name}, {_pct(rg['p'][int(rg['top'])])}) "
                              f"points against the {side}.")
            elif against:
                checks.append(f"The likeliest regime ahead is {name}, against the {side}, but that "
                              "forecast is no better than its baseline here.")
        invalid.append(f"the trade is wrong at its stop, {px(stop)}")
        if s.get('invalidation'):
            invalid.append(f"the setup is void by the playbook's own rule: {s['invalidation']}")
        adv_px = close - adv[2] * atr if buy else close + adv[2] * atr
        invalid.append(f"the range read is wrong if price trades {adv_word} {px(adv_px)} (its P80 "
                       f"{adv_side}) inside the horizon")
    else:
        invalid.append(f"the range read is wrong if price leaves {px(close - dn[2] * atr)} - "
                       f"{px(close + up[2] * atr)} (its P80 down and up) inside the horizon")
    facts.append('Where it stops being true: ' + '; '.join(invalid) + '.')
    return {'facts': facts, 'checks': checks, 'invalid': invalid, 'subject': subject}


def deterministic(pack: dict) -> str:
    """The challenge written by rules from the findings - always available."""
    lines = ['Where this read is weak:']
    lines += [f'- {c}' for c in pack['checks']] or \
        ['- No rule-based finding against it on this bar.']
    if pack['invalid']:
        lines += ['', 'Where it stops being true:'] + [f'- {x}' for x in pack['invalid']]
    if not pack.get('subject'):
        lines += ['', 'No qualified or live signal on this bar - this challenges the forecast only.']
    return '\n'.join(lines)


# --------------------------------------------------------------------------- #
# number tracing                                                              #
# --------------------------------------------------------------------------- #
_LABEL = re.compile(r'\b(?:P\d{2}|TP\d|\d+(?:m|h|H)\b|x\s?5m)')
_LIST = re.compile(r'^\s*\d+[.)]\s', re.M)
_NUM = re.compile(r'(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?')


def _numbers(text: str) -> list:
    text = _LIST.sub('', _LABEL.sub(' ', text))
    out = []
    for m in _NUM.finditer(text):
        raw = m.group().lstrip('+-').replace(',', '')
        out.append((raw, float(raw)))
    return out


def untraced(reply: str, facts: list) -> list:
    """Numbers in the reply that no fact contains, allowing only rounding of a fact's number."""
    have = [v for _, v in _numbers('\n'.join(facts))]
    bad = []
    for raw, v in _numbers(reply):
        dec = len(raw.split('.')[1]) if '.' in raw else 0
        tol = 0.5 * 10 ** (-dec) + 1e-9
        if not any(abs(v - f) <= tol for f in have):
            bad.append(raw)
    return bad


SYSTEM = """You are the supervisor inside DiaNurFx, a gold scalping workstation's backtest lab.

The forecast engine has produced every number in the FACT SHEET below, and a set of
RULE FINDINGS computed from it. Your job is to CHALLENGE the read, not to support it:
where is the evidence weak, where does the thesis stop being true, what would change it.

Rules, without exception:
- Use ONLY the fact sheet and the findings. Every number you write must appear in them.
  Do not introduce any other number: no new probabilities, prices, sizes, dates or counts.
- Where a forecast's confidence is 'baseline' or it did not pass its promotion gate, say
  it is not evidence. Never present a forecast as stronger than its label.
- No financial advice and no instruction to trade. Describe what the engine computed.
- Measured behaviour of this market: legs run both ways inside a higher-timeframe
  trend; leg length does not predict a turn; a pullback into a Fibonacci level is not
  a reason to expect a turn; do not argue for fading strength.
- Format: three short sections with '-' bullets, never numbered lists:
  'Weak points' (2-5 bullets), 'Where it stops being true' (1-3 bullets, price levels
  from the sheet), 'What would change the read' (1-2 bullets). Under 180 words."""


async def challenge(pack: dict) -> dict:
    """The model's challenge if a key is set and every number traces; the rules' otherwise."""
    rules = deterministic(pack)
    key = CONFIG.anthropic_key or os.environ.get('ANTHROPIC_API_KEY', '')
    base = {'facts': pack['facts'], 'checks': pack['checks'], 'subject': pack['subject']}
    if not key:
        return dict(base, text=rules, source='engine',
                    note='No language model key is set - written by the engine\'s rules.')
    try:
        import anthropic
    except ImportError:
        return dict(base, text=rules, source='engine',
                    note='The anthropic package is not installed - written by the engine\'s rules.')
    sheet = '\n'.join(f'- {f}' for f in pack['facts'])
    found = '\n'.join(f'- {c}' for c in pack['checks']) or '- none'
    prompt = f'FACT SHEET\n{sheet}\n\nRULE FINDINGS\n{found}\n\nChallenge this read.'
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        msg = await client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
                                           messages=[{'role': 'user', 'content': prompt}])
        text = ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text').strip()
    except Exception as exc:                                  # noqa: BLE001
        return dict(base, text=rules, source='engine',
                    note=f'The language model was unavailable ({type(exc).__name__}) - '
                         'written by the engine\'s rules.')
    bad = untraced(text, pack['facts'] + pack['checks'])
    if bad:
        return dict(base, text=rules, source='engine', withheld=True, untraced=bad,
                    note=f"The model's reply used {len(bad)} number(s) the engine did not produce "
                         f"({', '.join(bad[:6])}) and was withheld - written by the engine's rules.")
    return dict(base, text=text, source='llm', model=MODEL, traced=True,
                note='Every number in this reply was traced to the engine\'s fact sheet.')


__all__ = ['build', 'deterministic', 'untraced', 'challenge', 'SYSTEM', 'MODEL']
