"""
server/engine/narrator.py - the analyst that talks.

This is the deterministic reasoning layer. It does not guess and it does not
generate prose from a model; it reads the same computed evidence the signal
engine used and states it in sentences, which means:

  * the same setup always reads the same way - you can learn its voice
  * every claim is traceable to a number in the snapshot
  * it runs in microseconds, offline, at no cost per signal

Four modes, because a trader needs different things at different moments:

    ANALYSIS   what is happening and why a signal exists
    CHALLENGE  the case AGAINST the trade, argued properly
    RISK       what it costs, what it risks, what breaks it
    BACKTEST   how this playbook has actually performed

CHALLENGE IS NOT A DISCLAIMER. It is the most valuable mode and it is built to
find real objections: counter-evidence the playbook already recorded, gate
warnings, higher-timeframe conflict, proximity to opposing levels, how far
price has already run, and whether the same setup has been failing lately. If
it cannot find a real objection it says so rather than inventing one.

An optional LLM layer (llm.py) can rephrase or extend this, but it never
replaces it: the structured reasoning is the source of truth and the model only
ever sees facts this module produced.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import CONFIG


def _pct(x) -> str:
    return f'{x:+.0f}' if x is not None else 'n/a'


def _fmt(p, digits=2) -> str:
    return f'{p:,.{digits}f}' if p is not None else '-'


# --------------------------------------------------------------------------- #
# market narrative (no signal required)                                       #
# --------------------------------------------------------------------------- #
def describe_market(snap: dict) -> dict:
    """The standing read of the chart, signal or no signal."""
    if not snap.get('ok'):
        return {'headline': 'Not enough data to read this market.',
                'paragraphs': [], 'bullets': []}

    trend = snap['trend']
    regime = snap['regime']
    vol = regime['volatility']
    mtf = snap['mtf']
    mom = snap['momentum']
    sess = snap['session']
    price = snap['price']

    headline = f"{snap['symbol']} {snap['tf']} - {regime['label']}, {vol['label'].lower()}"

    paragraphs = []

    # --- structure --------------------------------------------------------- #
    struct = (f"Structure is {trend['state']} with {trend['strength'].lower()} "
              f"conviction; the last two labelled swings read {trend['structure']}.")
    if trend.get('last_break'):
        b = trend['last_break']
        struct += (f" The most recent structural event was a {b['kind']} "
                   f"{b['direction']} through {_fmt(b['price'])}.")
    if trend.get('invalidation') is not None:
        struct += (f" That read is wrong on a close beyond "
                   f"{_fmt(trend['invalidation'])}.")
    paragraphs.append(struct)

    # --- regime ------------------------------------------------------------ #
    reg = (f"The regime classifier calls this {regime['label']} at "
           f"{regime['confidence']}/100 confidence. "
           + '; '.join(regime['evidence'][:3]) + '. ' + regime['guidance'])
    paragraphs.append(reg)

    # --- multi-timeframe --------------------------------------------------- #
    if mtf.get('rows'):
        rows = ', '.join(f"{r['tf']} {r['state']}" for r in mtf['rows'])
        mtf_txt = (f"Across timeframes the read is {mtf['verdict']} "
                   f"({_pct(mtf['score'])} on a -100..100 scale): {rows}.")
        if mtf['verdict'] == 'conflicted':
            mtf_txt += (" Conflicted timeframes are a size-down condition, not a "
                        "direction - the edge in a scalp comes from agreement.")
        paragraphs.append(mtf_txt)

    # --- momentum and participation ---------------------------------------- #
    mo = (f"Momentum totals {_pct(mom['total'])}: RSI {mom['rsi']:.0f}, "
          f"MACD histogram {mom['macd_hist']:+.2f}, rate of change "
          f"{mom['roc']:+.2f}%.")
    div = mom.get('divergence') or {}
    if div.get('kind'):
        mo += f" There is {div['kind'].replace('_', ' ')} divergence - {div['note']}."
    paragraphs.append(mo)

    # --- volatility and session -------------------------------------------- #
    vtxt = (f"ATR is {vol['atr_points']:.0f} points, the "
            f"{vol['atr_percentile']:.0f}th percentile of its own recent range.")
    if vol['squeeze']:
        vtxt += " Bollinger width is compressed - an expansion is pending."
    elif vol['expanding']:
        vtxt += " Volatility is expanding."
    elif vol['contracting']:
        vtxt += " Volatility is contracting."
    vtxt += (f" Session is {sess['primary']} (tradeability {sess['quality']:.2f}).")
    paragraphs.append(vtxt)

    # --- what is actually on the chart ------------------------------------- #
    bullets = []
    nearest = snap.get('nearest') or {}
    if nearest.get('above'):
        a = nearest['above']
        bullets.append(f"Nearest resistance {_fmt(a['price'])} "
                       f"({a['touches']} touches, score {a['score']}, "
                       f"{a['distance_atr']:+.1f} ATR away)")
    if nearest.get('below'):
        b = nearest['below']
        bullets.append(f"Nearest support {_fmt(b['price'])} "
                       f"({b['touches']} touches, score {b['score']}, "
                       f"{b['distance_atr']:+.1f} ATR away)")
    for p in (snap.get('patterns') or [])[:2]:
        bullets.append(f"{p['label']} {p['status']} - quality {p['quality']}, "
                       f"break {_fmt(p['break_level'])}, target {_fmt(p['target'])}")
    for e in (snap.get('events') or [])[:3]:
        bullets.append(f"{e['kind'].replace('_', ' ')} {e['bars_since']} bars ago "
                       f"at {_fmt(e['price'])} (strength {e['strength']})")
    pools = [p for p in (snap.get('liquidity') or []) if not p['swept']][:2]
    for p in pools:
        bullets.append(f"Unswept {p['label']} at {_fmt(p['price'])} "
                       f"({p['distance_atr']:.1f} ATR, pull {p['pull']})")

    return {'headline': headline, 'paragraphs': paragraphs, 'bullets': bullets}


# --------------------------------------------------------------------------- #
# mode: ANALYSIS - why this signal exists                                     #
# --------------------------------------------------------------------------- #
def explain_signal(sig, snap: dict) -> dict:
    """The case FOR the trade, built from the evidence that produced it."""
    d = sig.to_dict() if hasattr(sig, 'to_dict') else sig
    side = d['side'].upper()
    pb = d['label']

    plan = d.get('exit_plan') or {}
    if plan.get('mode') == 'trail':
        thesis = (f"{pb} {side} on {d['symbol']} {d['tf']}. "
                  f"Enter {_fmt(d['entry'])}, stop {_fmt(d['stop'])}. "
                  f"At {_fmt(d['tp1'])} ({d['rr1']}R) the stop locks "
                  f"+{plan.get('lock_r', 0.3)}R and trails {plan.get('trail_atr', 1.0)} ATR; "
                  f"objective {_fmt(d['tp2'])} ({d['rr2']}R).")
    else:
        thesis = (f"{pb} {side} on {d['symbol']} {d['tf']}. "
                  f"Enter {_fmt(d['entry'])}, stop {_fmt(d['stop'])}, "
                  f"targets {_fmt(d['tp1'])} ({d['rr1']}R) and "
                  f"{_fmt(d['tp2'])} ({d['rr2']}R).")

    # Rank the evidence so the strongest reason leads.
    ev = sorted([e for e in d.get('evidence', []) if e['weight'] > 0],
                key=lambda e: -e['weight'])
    why = []
    for e in ev:
        why.append({'text': e['text'], 'kind': e['kind'], 'weight': e['weight']})

    sizing = d.get('sizing') or {}
    mechanics = []
    if sizing.get('lots'):
        mechanics.append(
            f"{sizing['lots']} lots risks {sizing.get('risk_cash', 0):.2f} "
            f"{CONFIG.risk.account_currency} "
            f"({sizing.get('risk_pct', 0):.2f}% of equity) over "
            f"{sizing.get('stop_points', 0):.0f} points.")
    if sizing.get('net_rr2') is not None:
        mechanics.append(
            f"After commission and a {sizing.get('spread_points') or '?'} point "
            f"spread the net reward is {sizing.get('net_rr1')}R to TP1 and "
            f"{sizing.get('net_rr2')}R to TP2.")
    mechanics.append(f"Invalidation: {d['invalidation']}.")
    if d.get('entry_type') != 'market':
        mechanics.append(
            f"This is a {d['entry_type']} order - it arms at "
            f"{_fmt(d.get('trigger'))} and does nothing until price trades there.")

    return {
        'mode': 'analysis',
        'thesis': thesis,
        'why': why,
        'mechanics': mechanics,
        'confidence': d.get('confidence', 0),
        'status': d.get('status'),
    }


# --------------------------------------------------------------------------- #
# mode: CHALLENGE - the case against                                          #
# --------------------------------------------------------------------------- #
def challenge_signal(sig, snap: dict, history: dict = None) -> dict:
    """
    Argue the other side, using only things that are actually true.

    Objections are graded so the trader can tell a fatal one from a nag:
        severe   would stop a careful trader taking it
        moderate worth sizing down for
        minor    noted, not decisive
    """
    d = sig.to_dict() if hasattr(sig, 'to_dict') else sig
    objections = []

    def add(sev, text, source):
        objections.append({'severity': sev, 'text': text, 'source': source})

    # 1. counter-evidence the playbook itself recorded
    for a in d.get('against', []):
        add('moderate', a, 'playbook')

    # 2. negative evidence weights
    for e in d.get('evidence', []):
        if e['weight'] < 0:
            add('moderate', f"{e['text']} ({e['weight']})", e['kind'])

    # 3. gate verdicts
    for g in d.get('gates', []):
        if g['verdict'] == 'BLOCK':
            add('severe', g['detail'], f"gate:{g['name']}")
        elif g['verdict'] == 'WARN':
            add('minor' if g['penalty'] <= 6 else 'moderate',
                g['detail'], f"gate:{g['name']}")

    # 4. structural objections the gates do not cover
    mtf = snap.get('mtf') or {}
    if mtf.get('verdict') == 'conflicted':
        add('moderate',
            f"timeframes disagree ({_pct(mtf.get('score'))}); in a scalp the "
            f"edge comes from agreement, so this is a size-down at best",
            'mtf')

    regime = snap.get('regime') or {}
    if regime.get('state') == 'transition':
        add('severe',
            f"the market is in TRANSITION (confidence {regime.get('confidence')}) "
            f"- this is where fixed-R scalps bleed, because neither the trend "
            f"nor the range assumption holds",
            'regime')

    vol = regime.get('volatility') or {}
    if vol.get('atr_percentile', 50) >= 88:
        add('moderate',
            f"ATR is in the {vol['atr_percentile']:.0f}th percentile - the stop "
            f"was sized on an ATR that is itself extreme, so a normal pullback "
            f"may take it out",
            'volatility')
    if vol.get('squeeze'):
        add('minor',
            'volatility is compressed; the first move out of a squeeze often '
            'fakes before it runs',
            'volatility')

    # 5. how far has price already gone?
    fib = snap.get('fib') or {}
    if fib.get('zone') == 'extended':
        add('moderate',
            'price is already extended beyond the last leg - the easy part of '
            'this move has been paid out',
            'structure')

    # 6. is the opposing level uncomfortably close?
    nearest = snap.get('nearest') or {}
    wall = nearest.get('above') if d['side'] == 'buy' else nearest.get('below')
    if wall and abs(wall['distance_atr']) < 1.2 and wall['score'] >= 60:
        add('severe' if abs(wall['distance_atr']) < 0.6 else 'moderate',
            f"a score-{wall['score']} {wall['kind']} sits at {_fmt(wall['price'])}, "
            f"only {abs(wall['distance_atr']):.1f} ATR from entry - that is "
            f"where this move is most likely to stall",
            'levels')

    # 7. counter-trend
    tr = snap.get('trend') or {}
    if ('up' in tr.get('state', '') and d['side'] == 'sell') or \
       ('down' in tr.get('state', '') and d['side'] == 'buy'):
        add('moderate',
            f"this trades against the {snap['tf']} structure, which is "
            f"{tr['state']} with {tr['strength'].lower()} conviction",
            'structure')

    # 8. recent performance of this exact playbook, when we have it
    if history:
        stat = (history.get('by_playbook') or {}).get(d['playbook'])
        if stat and stat.get('trades', 0) >= 8:
            wr = stat.get('win_rate', 0)
            if wr < 40:
                add('severe' if wr < 32 else 'moderate',
                    f"this playbook has won {wr:.0f}% of its last "
                    f"{stat['trades']} trades on this symbol - the pattern is "
                    f"currently not working",
                    'backtest')
            elif stat.get('expectancy_r', 0) < 0:
                add('moderate',
                    f"expectancy for this playbook is "
                    f"{stat['expectancy_r']:+.2f}R over {stat['trades']} trades",
                    'backtest')

    # 9. session
    sess = snap.get('session') or {}
    if sess.get('quality', 1.0) < 0.5:
        add('moderate',
            f"{sess.get('primary')} is a thin session; stops get run in thin "
            f"books and targets take longer to reach",
            'session')

    # Deduplicate. The gate ledger and the structural checks above legitimately
    # notice the same fact - an extreme ATR shows up as both a gate warning and
    # a stop-sizing objection - and listing it twice makes a single concern look
    # like two, which is exactly the kind of padding this mode exists to avoid.
    # Topic is inferred from the source so the more severe phrasing survives.
    severity_rank = {'severe': 0, 'moderate': 1, 'minor': 2}

    def topic(o):
        src = o['source'].split(':')[-1]
        return src if src in ('volatility', 'session', 'mtf', 'regime', 'spread',
                              'room', 'reward', 'sizing', 'exposure', 'daily',
                              'news', 'broker', 'levels') else o['text'][:40].lower()

    objections.sort(key=lambda o: severity_rank.get(o['severity'], 3))
    seen, deduped = set(), []
    for o in objections:
        key = topic(o)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(o)
    objections = deduped

    severe = sum(1 for o in objections if o['severity'] == 'severe')
    moderate = sum(1 for o in objections if o['severity'] == 'moderate')

    if severe:
        verdict = 'stand aside'
        summary = (f"{severe} severe objection{'s' if severe > 1 else ''}. "
                   f"This is not a trade to take as specified.")
    elif moderate >= 3:
        verdict = 'size down'
        summary = (f"No single objection is fatal, but {moderate} moderate ones "
                   f"stack. Half size or wait for one more confirmation.")
    elif moderate:
        verdict = 'proceed with care'
        summary = (f"{moderate} moderate objection{'s' if moderate > 1 else ''} "
                   f"worth pricing in; the core case stands.")
    elif objections:
        verdict = 'proceed'
        summary = 'Only minor objections. The case holds.'
    else:
        verdict = 'proceed'
        summary = ("No material objection found. That is itself worth noting - "
                   "clean setups are rare, so check you have not mis-specified "
                   "the stop.")

    return {
        'mode': 'challenge',
        'verdict': verdict,
        'summary': summary,
        'objections': objections,
        'counts': {'severe': severe, 'moderate': moderate,
                   'minor': len(objections) - severe - moderate},
    }


# --------------------------------------------------------------------------- #
# mode: RISK                                                                  #
# --------------------------------------------------------------------------- #
def risk_view(sig, snap: dict, context: dict = None) -> dict:
    d = sig.to_dict() if hasattr(sig, 'to_dict') else sig
    ctx = context or {}
    sizing = d.get('sizing') or {}
    r = CONFIG.risk
    ccy = r.account_currency

    equity = float(ctx.get('equity') or sizing.get('equity') or r.equity)
    risk_cash = float(sizing.get('risk_cash') or 0.0)
    lots = float(sizing.get('lots') or 0.0)

    lines = []
    if lots:
        lines.append(f"Position: {lots} lots. A stop-out costs "
                     f"{risk_cash:.2f} {ccy}, {sizing.get('risk_pct', 0):.2f}% "
                     f"of {equity:,.0f} {ccy} equity.")
        tp1_gain = abs(d['tp1'] - d['entry']) / max(abs(d['entry'] - d['stop']), 1e-9) * risk_cash
        tp2_gain = abs(d['tp2'] - d['entry']) / max(abs(d['entry'] - d['stop']), 1e-9) * risk_cash
        lines.append(f"TP1 returns about {tp1_gain:.2f} {ccy} gross, TP2 about "
                     f"{tp2_gain:.2f} {ccy}, before the "
                     f"{sizing.get('commission_round_trip', 0):.2f} {ccy} "
                     f"round-trip commission.")
    else:
        lines.append('No size could be calculated for this signal.')

    open_pos = int(ctx.get('open_positions') or 0)
    trades_today = int(ctx.get('trades_today') or 0)
    daily = float(ctx.get('daily_pnl_pct') or 0.0)
    lines.append(f"Exposure: {open_pos}/{r.max_concurrent} positions open, "
                 f"{trades_today}/{r.max_daily_trades} trades today, "
                 f"{daily:+.2f}% on the day against a "
                 f"-{r.max_daily_loss_pct:.1f}% stop.")

    # What a cluster of losses does - the number people skip.
    consecutive = 5
    drawdown = (1 - (1 - sizing.get('risk_pct', 0) / 100.0) ** consecutive) * 100
    lines.append(f"{consecutive} losses in a row at this size is about "
                 f"{drawdown:.1f}% of equity. The daily limit stops you at "
                 f"{r.max_daily_loss_pct:.1f}%.")

    vol = (snap.get('regime') or {}).get('volatility') or {}
    lines.append(f"The stop sits {sizing.get('stop_points', 0):.0f} points away "
                 f"against an ATR of {vol.get('atr_points', 0):.0f} points "
                 f"({sizing.get('stop_points', 0) / max(vol.get('atr_points', 1), 1):.1f} ATR).")

    sgn = 1 if d['side'] == 'buy' else -1
    one_r = abs(d['entry'] - d['stop'])
    if r.exit_mode == 'trail':
        exit_lines = [
            f"Hold the whole position to TP1 {_fmt(d['tp1'])} - no partial.",
            f"At TP1, move the stop to {_fmt(d['entry'] + sgn * one_r * r.trail_lock_r)} "
            f"(+{r.trail_lock_r}R, which covers costs).",
            f"Then trail it {r.trail_atr} ATR behind the best close-bar price; "
            f"no fixed target. {_fmt(d['tp2'])} is an objective, not an order.",
        ]
    else:
        exit_lines = [
            f"Move to break-even at +{r.breakeven_at_r:.0f}R "
            f"({_fmt(d['entry'] + sgn * one_r * r.breakeven_at_r)}).",
            f"Take partial at TP1 {_fmt(d['tp1'])}, leave the rest for TP2 {_fmt(d['tp2'])}.",
        ]
    management = [
        *exit_lines,
        f"Abandon the idea if {d['invalidation']}",
        f"This signal expires after {d.get('expiry_bars', 20)} bars if it has not triggered.",
    ]

    return {'mode': 'risk', 'lines': lines, 'management': management,
            'sizing': sizing}


# --------------------------------------------------------------------------- #
# mode: BACKTEST                                                              #
# --------------------------------------------------------------------------- #
def backtest_view(sig, stats: dict) -> dict:
    d = sig.to_dict() if hasattr(sig, 'to_dict') else sig
    pb = d['playbook']
    lines = []
    if not stats:
        return {'mode': 'backtest',
                'lines': ['No backtest has been run for this configuration yet. '
                          'Run one from the BACKTEST tab and this panel will '
                          'quote its numbers instead of guessing.'],
                'stat': None}

    overall = stats.get('summary') or {}
    pf = overall.get('profit_factor')
    pf_txt = 'undefined (no losing trades)' if pf is None else f'{pf:.2f}'
    lines.append(f"Overall: {overall.get('trades', 0)} trades, "
                 f"{overall.get('win_rate', 0):.1f}% win rate, profit factor "
                 f"{pf_txt}, expectancy "
                 f"{overall.get('expectancy_r', 0):+.2f}R, max drawdown "
                 f"{overall.get('max_drawdown_pct', 0):.1f}%.")

    stat = (stats.get('by_playbook') or {}).get(pb)
    if stat:
        lines.append(f"{d['label']} specifically: {stat.get('trades', 0)} trades, "
                     f"{stat.get('win_rate', 0):.1f}% win rate, expectancy "
                     f"{stat.get('expectancy_r', 0):+.2f}R, average winner "
                     f"{stat.get('avg_win_r', 0):.2f}R against average loser "
                     f"{stat.get('avg_loss_r', 0):.2f}R.")
        if stat.get('trades', 0) < 20:
            lines.append(f"Only {stat.get('trades')} samples - treat this as an "
                         f"indication, not a statistic.")
    else:
        lines.append(f"No completed trades for {d['label']} in the current "
                     f"backtest window.")

    by_sess = stats.get('by_session') or {}
    if by_sess:
        best = max(by_sess.items(), key=lambda kv: kv[1].get('expectancy_r', -99))
        worst = min(by_sess.items(), key=lambda kv: kv[1].get('expectancy_r', 99))
        lines.append(f"By session, {best[0]} has been the best "
                     f"({best[1].get('expectancy_r', 0):+.2f}R over "
                     f"{best[1].get('trades', 0)} trades) and {worst[0]} the worst "
                     f"({worst[1].get('expectancy_r', 0):+.2f}R over "
                     f"{worst[1].get('trades', 0)}).")

    return {'mode': 'backtest', 'lines': lines, 'stat': stat}


# --------------------------------------------------------------------------- #
# the full brief                                                              #
# --------------------------------------------------------------------------- #
def brief(sig, snap: dict, context: dict = None, stats: dict = None) -> dict:
    """Everything the agent has to say about one signal, in all four modes."""
    return {
        'signal_id': (sig.id if hasattr(sig, 'id') else sig.get('id')),
        'analysis': explain_signal(sig, snap),
        'challenge': challenge_signal(sig, snap, stats),
        'risk': risk_view(sig, snap, context),
        'backtest': backtest_view(sig, stats),
        'generated_ms': int(datetime.now(timezone.utc).timestamp() * 1000),
    }


__all__ = ['describe_market', 'explain_signal', 'challenge_signal',
           'risk_view', 'backtest_view', 'brief']
