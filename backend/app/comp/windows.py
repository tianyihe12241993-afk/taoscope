"""Submission windows: one shape for every competition subnet.

Each adapter stores its platform's round/window fields in the platform's own
vocabulary (ORO races, Nepher tournaments, Ridges sets, Harnyx batches, Cascade
epochs, NPA rounds, Prism rolling submissions, Soma competitions). This module
folds them into one record the Subnets table can show:

    state     open      you can submit now; `closes_at` is the deadline
              eval      submissions closed, scoring runs until `ends_at`
              rolling   submit any time (no window)
              closed    nothing open; `opens_at` is the next window if known
              unknown   tracker has no usable window fields
    label     the round / race / batch / competition being described
    note      one line of platform-specific meaning

Everything is derived from the stored snapshot plus the poll time, never from a
live call, so it costs nothing on the screener path.
"""
from __future__ import annotations

import datetime as dt

from ..db import pool

BLOCK_S = 12


def _ts(v) -> dt.datetime | None:
    """ISO string or Unix epoch (s or ms) -> aware UTC datetime. Naive strings
    are taken as UTC. Nepher stores epochs; everyone else stores ISO."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        secs = v / 1000.0 if v > 1e11 else float(v)
        try:
            return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(v, str):
        return None
    s = v.strip()
    if s.isdigit():
        return _ts(int(s))
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _iso(d: dt.datetime | None) -> str | None:
    return d.isoformat() if d else None


def _blocks_from(polled: dt.datetime | None, blocks) -> dt.datetime | None:
    if polled is None or not isinstance(blocks, (int, float)):
        return None
    return polled + dt.timedelta(seconds=max(blocks, 0) * BLOCK_S)


def window_for(netuid: int, d: dict, polled: dt.datetime | None) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    w: dict = {"netuid": netuid, "state": "unknown", "label": None, "note": None,
               "opens_at": None, "closes_at": None, "ends_at": None,
               "polled_at": _iso(polled)}

    if netuid == 15:                                   # ORO: qualifying -> race
        w["label"] = f"race {d.get('race_no')}" if d.get("race_no") else "race"
        phase = d.get("phase")
        if phase == "qualifying":
            w.update(state="open", closes_at=_iso(_ts(d.get("closes_at"))),
                     note="qualifying window — agents that clear the gate before it closes run in this race")
        elif phase == "racing":
            w.update(state="eval", ends_at=_iso(_ts(d.get("eta"))),
                     note="race running — next qualifying window opens when it completes")
        elif phase == "complete":
            w.update(state="closed", note="race complete — waiting for the next race record")

    elif netuid == 49:                                 # Nepher: several tournaments
        tours = [t for t in (d.get("tours") or {}).values() if isinstance(t, dict)]
        open_ = [t for t in tours if t.get("stage") in ("contest", "submit")
                 and (_ts(t.get("end_ts")) or now) > now]
        if open_:
            t = min(open_, key=lambda x: _ts(x.get("end_ts")) or now)
            lock = _ts(t.get("lock_ts"))
            stage = t.get("stage")
            note = f"{len(open_)} of {len(tours)} active tournaments taking uploads · stage {stage}"
            if stage == "contest" and lock:
                note += f" · final-submission window from {lock:%b %-d %H:%M} UTC"
            w.update(state="open", label=t.get("title") or t.get("task"),
                     closes_at=_iso(_ts(t.get("end_ts"))), note=note)
        else:
            ev = [t for t in tours if t.get("stage") in ("evaluation", "review")]
            if ev:
                t = min(ev, key=lambda x: _ts(x.get("eval_end_ts")) or now)
                w.update(state="eval", label=t.get("title") or t.get("task"),
                         ends_at=_iso(_ts(t.get("eval_end_ts"))),
                         note=f"{len(ev)} tournament(s) in evaluation; none taking submissions")
            elif tours:
                w.update(state="closed", label=tours[0].get("title") or tours[0].get("task"),
                         note="active tournaments are past submission")

    elif netuid == 62:                                 # Ridges: evaluation sets
        if d.get("comp_id") is not None:
            w["label"] = f"set {d.get('comp_id')} · {d.get('comp_name') or ''}".strip(" ·")
            ended = _ts(d.get("set_ended"))
            if ended and ended <= now:
                w.update(state="closed", ends_at=_iso(ended), note="latest set has ended")
            else:
                w.update(state="open", opens_at=_iso(_ts(d.get("set_started"))), closes_at=_iso(ended),
                         note="agents upload any time inside the set; no fixed deadline"
                              if not ended else "set closes at the deadline")

    elif netuid == 67:                                 # Harnyx: daily batch cutoff
        cut = _ts(d.get("open_cutoff"))
        if cut:
            w.update(state="open", label="daily batch", closes_at=_iso(cut),
                     note="submissions before the 15:00 UTC cutoff enter that day's batch")

    elif netuid == 91:                                 # Cascade: epoch boundary
        blk, bnd = d.get("block"), d.get("boundary")
        if isinstance(blk, int) and isinstance(bnd, int):
            w.update(state="open", label=f"round @{d.get('round_epoch') or d.get('epoch_start')}",
                     closes_at=_iso(_blocks_from(polled, bnd - blk)),
                     note=f"commit + reveal must land before the epoch boundary · stage {d.get('stage') or '?'}")

    elif netuid == 98:                                 # NPA: dated rounds
        if d.get("open_round"):
            w.update(state="open", label=f"round {d.get('open_round')}",
                     closes_at=_iso(_blocks_from(polled, d.get("open_blocks_left"))),
                     note=f"evaluating round {d.get('eval_round')}" if d.get("eval_round") else None)
        elif d.get("eval_round"):
            w.update(state="eval", label=f"round {d.get('eval_round')}", note="no round open for submission")

    elif netuid == 100:                                # Prism: rolling, one shot per hotkey
        w.update(state="rolling", label=d.get("comp_id") or "competition",
                 note="rolling submissions — one accepted submission per hotkey, ever")

    elif netuid == 114:                                # Soma: upload -> evaluation
        if d.get("comp_id") is not None:
            w["label"] = f"#{d.get('comp_id')} {d.get('comp_name') or ''}".strip()
            st = d.get("comp_state")
            if st == "upload":
                w.update(state="open", opens_at=_iso(_ts(d.get("upload_start"))),
                         closes_at=_iso(_ts(d.get("upload_end"))), note="upload window")
            elif st == "evaluation":
                w.update(state="eval", ends_at=_iso(_ts(d.get("eval_end"))),
                         note="uploads closed; evaluation running")
            elif st:
                w.update(state="closed", note=f"competition {st}")

    return w


async def all_windows() -> dict[int, dict]:
    rows = await pool().fetch(
        "SELECT c.netuid, s.data, s.updated_at FROM comp_subnet c"
        " JOIN comp_state s USING (netuid) WHERE c.enabled")
    out: dict[int, dict] = {}
    for r in rows:
        data = r["data"] or {}
        if isinstance(data, str):
            import json
            data = json.loads(data)
        out[r["netuid"]] = window_for(r["netuid"], data, r["updated_at"])
    return out
