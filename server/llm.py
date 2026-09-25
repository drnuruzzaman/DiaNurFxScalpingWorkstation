"""
server/llm.py - the optional language layer.

The deterministic narrator is the system's voice. This module exists only for
free-text questions that do not map onto a computed field, and it is built so
that switching it off changes nothing about the trading logic.

Two rules, both load-bearing:

  1. THE MODEL NEVER COMPUTES. It receives a fact sheet built from the snapshot
     and answers from it. It is not asked to judge a setup, size a position, or
     decide direction - those come from the engine, which is testable.
  2. THE MODEL IS OPTIONAL. With no API key the router answers from the same
     facts using pattern matching. Every answer says which path produced it, so
     an LLM answer is never mistaken for a computed one.

That ordering matters on a trading desk: a model that hallucinates a support
level costs money, and there is no way to unit-test prose. Keeping the numbers
on the deterministic side means the worst an LLM failure can do is phrase
something awkwardly.
"""

from __future__ import annotations

import json
import os
import re

from .config import CONFIG
from .engine.legs import MEASURED, htf_trend
from .engine.legs import describe as describe_leg

MODEL = 'claude-sonnet-5'
MAX_TOKENS = 900


def llm_available() -> bool:
    return bool(CONFIG.anthropic_key or os.environ.get('ANTHROPIC_API_KEY'))


# --------------------------------------------------------------------------- #
# fact sheet                                                                  #
# --------------------------------------------------------------------------- #
def build_facts(snap: dict, sig=None, context: dict = None,
                stats: dict = None) -> list:
    """
    Everything true about this moment, as flat strings.

    This is both the LLM's entire context and the deterministic router's lookup
    table, so the two paths can never disagree about the facts.
    """
    if not snap or not snap.get('ok'):
        return ['No valid analysis is available for this symbol and timeframe.']

    f = []
    t = snap['trend']
    r = snap['regime']
    v = r['volatility']
    m = snap['momentum']
    mtf = snap['mtf']
    sess = snap['session']

    f.append(f"Symbol {snap['symbol']} on {snap['tf']}, price {snap['price']}.")
    f.append(f"ATR is {v['atr_points']:.0f} points, the {v['atr_percentile']:.0f}th "
             f"percentile of its recent range ({v['label']}).")
    f.append(f"Regime is {r['label']} at {r['confidence']}/100 confidence. "
             f"Evidence: {'; '.join(r['evidence'][:4])}.")
    f.append(f"Guidance for this regime: {r['guidance']}")
    f.append(f"Structure is {t['state']} ({t['strength']}), pattern {t['structure']}, "
             f"invalidation {t.get('invalidation')}.")
    if t.get('last_break'):
        b = t['last_break']
        f.append(f"Last structural event: {b['kind']} {b['direction']} through {b['price']}.")
    leg = snap.get('leg_gate') or snap.get('leg')
    if leg:
        htf_dir, htf_tf = htf_trend(mtf, snap.get('tf'))
        f.append(f"Leg in progress: {describe_leg(leg, htf_dir, htf_tf)}.")
    for line in MEASURED:
        f.append(f"Measured behaviour: {line}")
    f.append(f"Multi-timeframe read: {mtf.get('verdict')} with score {mtf.get('score')} "
             f"(-100 to +100). Rows: "
             f"{', '.join(f"{x['tf']}={x['state']}" for x in mtf.get('rows', []))}.")
    f.append(f"Momentum total {m['total']}, RSI {m['rsi']}, MACD histogram "
             f"{m['macd_hist']}, ROC {m['roc']}%.")
    div = m.get('divergence') or {}
    if div.get('kind'):
        f.append(f"Divergence: {div['kind']} - {div['note']}.")
    f.append(f"Session is {sess['primary']} with tradeability {sess['quality']}.")
    f.append(f"Fear/Greed gauge reads {snap['gauge']['value']} ({snap['gauge']['label']}).")

    near = snap.get('nearest') or {}
    if near.get('above'):
        a = near['above']
        f.append(f"Nearest resistance {a['price']} (score {a['score']}, "
                 f"{a['touches']} touches, {a['distance_atr']} ATR away).")
    if near.get('below'):
        b = near['below']
        f.append(f"Nearest support {b['price']} (score {b['score']}, "
                 f"{b['touches']} touches, {b['distance_atr']} ATR away).")

    for lv in (snap.get('levels') or [])[:6]:
        f.append(f"Level {lv['price']} is {lv['kind']}, score {lv['score']}, "
                 f"{lv['touches']} touches, {', '.join(lv['sources'])}.")
    for p in (snap.get('patterns') or [])[:4]:
        f.append(f"Pattern {p['label']} is {p['status']}, quality {p['quality']}, "
                 f"break {p['break_level']}, target {p['target']}, R:R {p['rr']}. "
                 f"{'; '.join(p['notes'][:2])}.")
    for e in (snap.get('events') or [])[:5]:
        f.append(f"Event {e['kind']} {e['bars_since']} bars ago at {e['price']}, "
                 f"strength {e['strength']}, implies {e['bias']}.")
    for pool in (snap.get('liquidity') or [])[:4]:
        f.append(f"Liquidity {pool['label']} at {pool['price']}, pull {pool['pull']}, "
                 f"{'already swept' if pool['swept'] else 'unswept'}.")
    for ch in (snap.get('channels') or [])[:2]:
        f.append(f"Channel {ch['kind']}, containment {ch['containment']}, "
                 f"price sits at {ch['position']} of its height.")
    rev = snap.get('reversal') or {}
    if rev.get('detected'):
        f.append(f"Reversal signals present, score {rev['score']}, bias {rev['bias']}: "
                 f"{'; '.join(rev.get('components', []))}.")

    if sig is not None:
        d = sig.to_dict() if hasattr(sig, 'to_dict') else sig
        f.append(f"Active signal: {d['label']} {d['side']} at {d['entry']}, "
                 f"stop {d['stop']}, TP1 {d['tp1']} ({d['rr1']}R), "
                 f"TP2 {d['tp2']} ({d['rr2']}R), confidence {d['confidence']}, "
                 f"status {d['status']}.")
        for e in d.get('evidence', []):
            f.append(f"Signal evidence ({e['kind']}): {e['text']}")
        for a in d.get('against', []):
            f.append(f"Signal counter-evidence: {a}")
        for g in d.get('gates', []):
            f.append(f"Gate {g['name']}: {g['verdict']} - {g['detail']}")
        sz = d.get('sizing') or {}
        if sz.get('lots'):
            f.append(f"Sizing: {sz['lots']} lots, risk {sz.get('risk_cash')} "
                     f"{CONFIG.risk.account_currency} ({sz.get('risk_pct')}%), "
                     f"net {sz.get('net_rr2')}R to TP2 after costs.")

    if context:
        f.append(f"Account context: equity {context.get('equity')}, "
                 f"{context.get('open_positions')} positions open, "
                 f"{context.get('trades_today')} trades today, "
                 f"spread {context.get('spread_points')} points.")

    if stats and stats.get('summary'):
        s = stats['summary']
        f.append(f"Latest backtest: {s.get('trades')} trades, "
                 f"{s.get('win_rate')}% win rate, profit factor "
                 f"{s.get('profit_factor')}, expectancy {s.get('expectancy_r')}R, "
                 f"max drawdown {s.get('max_drawdown_pct')}%.")
        for pb, st in (stats.get('by_playbook') or {}).items():
            f.append(f"Backtest {pb}: {st['trades']} trades, {st['win_rate']}% win, "
                     f"expectancy {st['expectancy_r']}R.")
    return f


# --------------------------------------------------------------------------- #
# deterministic router                                                        #
# --------------------------------------------------------------------------- #
TOPICS = [
    ('level|support|resistance|s/r|zone',
     lambda f: [x for x in f if x.startswith(('Level ', 'Nearest '))]),
    ('pattern|formation|head|shoulder|triangle|flag|wedge|double|channel',
     lambda f: [x for x in f if x.startswith(('Pattern ', 'Channel '))]),
    ('trend|structure|bos|choch|direction|bias',
     lambda f: [x for x in f if x.startswith(('Structure ', 'Last structural',
                                              'Multi-timeframe', 'Regime '))]),
    ('volatil|atr|quiet|choppy|wild',
     lambda f: [x for x in f if 'ATR' in x or 'volatil' in x.lower()]),
    ('momentum|rsi|macd|diverg|overbought|oversold',
     lambda f: [x for x in f if x.startswith(('Momentum ', 'Divergence '))]),
    ('liquidity|sweep|stop hunt|equal high|equal low',
     lambda f: [x for x in f if x.startswith(('Liquidity ', 'Event sweep'))]),
    ('risk|size|lot|position|stop loss|how much',
     lambda f: [x for x in f if x.startswith(('Sizing:', 'Account context'))]),
    ('gate|reject|block|why not|qualif',
     lambda f: [x for x in f if x.startswith('Gate ')]),
    ('backtest|history|performance|win rate|expectancy',
     lambda f: [x for x in f if x.startswith(('Latest backtest', 'Backtest '))]),
    ('session|london|tokyo|new york|time',
     lambda f: [x for x in f if x.startswith('Session ')]),
    ('signal|trade|entry|target|setup|should i',
     lambda f: [x for x in f if x.startswith(('Active signal', 'Signal '))]),
    ('event|breakout|retest|false|rejection',
     lambda f: [x for x in f if x.startswith('Event ')]),
    ('leg|impulse|correction|pullback|retrace|extended|exhaust|fade|pocket|fib|reverse|turn',
     lambda f: [x for x in f if x.startswith(('Leg ', 'Measured '))]),
]


def route(question: str, facts: list) -> list:
    q = question.lower()
    hits: list = []
    for pattern, picker in TOPICS:
        if re.search(pattern, q):
            hits.extend(picker(facts))
    # Deduplicate, preserve order.
    seen, out = set(), []
    for h in hits:
        if h in seen:
            continue
        seen.add(h)
        out.append(h)
    return out


def deterministic_answer(question: str, facts: list) -> dict:
    matched = route(question, facts)
    if not matched:
        # No topic matched: hand back the standing read rather than nothing.
        matched = facts[:8]
        lead = ("I could not match that to a specific topic, so here is the "
                "current read:")
    else:
        lead = 'From the current analysis:'
    return {
        'answer': lead + '\n\n' + '\n'.join(f'- {m}' for m in matched[:14]),
        'source': 'engine',
        'facts': matched[:14],
    }


# --------------------------------------------------------------------------- #
# LLM path                                                                    #
# --------------------------------------------------------------------------- #
SYSTEM = """You are the analyst inside DiaNurFx, a gold scalping workstation.

You will be given a FACT SHEET computed by the system's analysis engine, and a
question from the trader who owns the account.

Rules, without exception:
- Answer ONLY from the fact sheet. Every number you state must appear in it.
- If the fact sheet does not contain what was asked, say so plainly and say
  what you do have. Never estimate, infer, or fill a gap with a plausible number.
- Do not give financial advice or tell the trader what to do with their money.
  Describe what the system computed and what it implies mechanically.
- Be concise and concrete. This is a terminal, not an essay. Prefer short
  paragraphs and specific price levels over general commentary.
- If the question asks whether a trade is good, describe the evidence on both
  sides that the fact sheet contains, including the counter-evidence, and let
  the trader decide.

How to reason about price. These rules come from the "Measured behaviour"
lines in the fact sheet - this system's own data, 2024-2026:
- Treat price as a sequence of impulse legs and corrections. Lower-timeframe
  legs run both ways inside a higher-timeframe trend, so a move against the
  1h trend is a normal leg, not a mistake to be corrected.
- Never call a leg exhausted, overextended or due to reverse because of how
  far it has run. Leg length does not predict a turn.
- Never present a pullback into a Fibonacci level or "pocket" as a reason
  price will turn there. Corrections typically retrace nearly the whole leg.
- Do not argue for fading strength. Trading against a leg in the three
  measured spots lost in every year tested; the "Leg in progress" line and
  the leg gate say whether this is one of them."""


async def llm_answer(question: str, facts: list) -> dict:
    key = CONFIG.anthropic_key or os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    sheet = '\n'.join(f'- {f}' for f in facts)
    prompt = f'FACT SHEET\n{sheet}\n\nQUESTION\n{question}'
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        msg = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = ''.join(
            block.text for block in msg.content if getattr(block, 'type', '') == 'text')
        return {'answer': text.strip(), 'source': 'llm', 'model': MODEL,
                'facts': facts[:20]}
    except Exception as exc:                              # noqa: BLE001
        return {'error': f'{type(exc).__name__}: {exc}'}


async def answer_question(question: str, snap: dict, sig=None,
                          context: dict = None, stats: dict = None) -> dict:
    facts = build_facts(snap, sig, context, stats)
    if llm_available():
        result = await llm_answer(question, facts)
        if result and not result.get('error'):
            return result
        # Fall through to the deterministic path on any LLM failure, with a
        # note - a trading terminal must not go silent because an API blipped.
        fallback = deterministic_answer(question, facts)
        if result and result.get('error'):
            fallback['note'] = f"LLM unavailable ({result['error']}); answered from the engine."
        return fallback
    return deterministic_answer(question, facts)


__all__ = ['answer_question', 'build_facts', 'llm_available',
           'deterministic_answer']
