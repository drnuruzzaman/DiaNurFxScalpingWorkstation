"""
sim/clock.py — broker-server-clock resolution, shared by the bridge and by any
downloader that has to stamp MT5 bars onto a real UTC axis.

MT5 hands you the BROKER's wall clock, not UTC. IC Markets runs UTC+2/+3 with
DST, so every timestamp arrives shifted by hours. Everything downstream — bar
alignment, session shading, "is this candle closed yet" — is wrong if that
shift is wrong, and it is wrong in a way that looks like working software.

The offset is measured, not assumed: the freshest tick a live terminal holds is
by definition ~now, so (tick_time - true_now) is the offset. That only works
while the market is OPEN. On a weekend the freshest tick is Friday's, which
would read as a -48h offset, so a measurement is only trusted when it lands on
a plausible whole-or-half hour and the ticks agree with each other. Otherwise we
fall back to the last offset measured while the market was open, persisted in
data/manifest.json.

Confidence is returned alongside so callers can show it rather than pretend:
    'measured' — live ticks agreed, offset is current
    'stale'    — market shut, reusing the offset from the manifest
    'assumed'  — nothing to go on, offset 0, treat times with suspicion
"""

from __future__ import annotations

import json
import os
from typing import Iterable, Sequence

HOUR_MS = 3_600_000
# Brokers sit between UTC-12 and UTC+14. Anything outside that is not a
# timezone, it is a stale tick or a broken clock.
MIN_OFFSET_MS = -12 * HOUR_MS
MAX_OFFSET_MS = 14 * HOUR_MS
# Offsets are whole or half hours in practice. Snapping to the nearest half hour
# both cleans up network jitter and acts as a sanity test: a genuine live
# measurement lands within a couple of minutes of a half-hour boundary, a
# Friday-night tick does not.
SNAP_MS = HOUR_MS // 2
SNAP_TOLERANCE_MS = 4 * 60 * 1000          # 4 min from a half-hour boundary
AGREEMENT_MS = 90 * 1000                   # probes must agree within 90 s


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        raise ValueError('median of empty sequence')
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def snap_offset(raw_ms: float) -> tuple[int, int]:
    """Snap a raw offset to the nearest half hour. Returns (snapped, residual)."""
    snapped = int(round(raw_ms / SNAP_MS) * SNAP_MS)
    return snapped, int(abs(raw_ms - snapped))


def manifest_path(data_dir: str) -> str:
    return os.path.join(data_dir, 'manifest.json')


def manifest_offset(data_dir: str) -> tuple[int | None, str]:
    """Last offset measured while the market was open, if we ever stored one."""
    path = manifest_path(data_dir)
    if not os.path.exists(path):
        return None, 'none'
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None, 'none'
    clock = doc.get('clock') or {}
    offset = clock.get('offset_ms')
    if offset is None:
        return None, 'none'
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        return None, 'none'
    if not (MIN_OFFSET_MS <= offset <= MAX_OFFSET_MS):
        return None, 'none'
    return offset, 'stale'


def store_offset(data_dir: str, offset_ms: int, measured_at_ms: float) -> None:
    """Persist a MEASURED offset so a weekend restart does not lose it."""
    path = manifest_path(data_dir)
    doc = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                doc = json.load(fh) or {}
        except (OSError, ValueError):
            doc = {}
    doc['clock'] = {
        'offset_ms': int(offset_ms),
        'offset_hours': round(offset_ms / HOUR_MS, 2),
        'measured_at_ms': int(measured_at_ms),
    }
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass        # persistence is a convenience, never a hard failure


def resolve_offset(tick_times_ms: Iterable[float],
                   true_now_ms: float,
                   known_offset_ms: int | None = None,
                   known_confidence: str = 'none') -> tuple[int, str]:
    """
    Best estimate of (broker clock - true UTC) in milliseconds.

    tick_times_ms  — time_msc of the freshest tick from each probe symbol
    true_now_ms    — this machine's UTC now, in ms
    known_offset_ms— the manifest fallback, used when live measurement fails
    """
    ticks = [float(t) for t in (tick_times_ms or []) if t]

    def fallback() -> tuple[int, str]:
        if known_offset_ms is not None:
            return int(known_offset_ms), (known_confidence or 'stale')
        return 0, 'assumed'

    if not ticks:
        return fallback()

    # The freshest tick is the one closest to "now" in broker time. Using the
    # median of all probes instead would drag the estimate toward whichever
    # symbol stopped ticking first (metals close before FX).
    freshest = max(ticks)
    raw = freshest - true_now_ms

    if not (MIN_OFFSET_MS <= raw <= MAX_OFFSET_MS + HOUR_MS):
        return fallback()

    snapped, residual = snap_offset(raw)
    if residual > SNAP_TOLERANCE_MS:
        # Not near a timezone boundary => this is a stale feed, not a timezone.
        return fallback()
    if not (MIN_OFFSET_MS <= snapped <= MAX_OFFSET_MS):
        return fallback()

    # Cross-check: at least one other probe should agree, when we have others.
    lively = [t for t in ticks if abs((freshest - t)) <= AGREEMENT_MS]
    if len(ticks) > 1 and len(lively) < 2:
        # Only one symbol is actually live. Still usable, but say so if we have
        # a stored value that disagrees materially.
        if known_offset_ms is not None and abs(known_offset_ms - snapped) > SNAP_MS:
            return int(known_offset_ms), (known_confidence or 'stale')

    return snapped, 'measured'


__all__ = [
    'HOUR_MS', 'manifest_offset', 'manifest_path', 'resolve_offset',
    'snap_offset', 'store_offset',
]
