"""SN91 Cascade -- synthetic time-series data generators, king-of-the-hill.

Miners submit a *data generator*, not a model. The owner trains a fixed
Toto2-4M forecaster from a shared init on each corpus and scores it on 2000
private held-out windows; the challenger dethrones when the paired-bootstrap
LCB of its advantage clears a tenure-decayed margin.

Everything below comes from the manifest bucket, which is public and needs no
auth -- the subnet's own dashboard reads the same three status files:

    status/chain.json    block, epoch, stage windows, every revealed commitment
    status/round.json    live stage (heat / duel / validation) + heat progress
    status/heat.json     the screening standings, published when the heat settles
    receipts/index.json  settled rounds: lcb, margin, dethrone, reward ladder

What matters here and nowhere else: a round is decided in ONE duel between the
king and a single finalist, entering a heat burns the hotkey for life, and the
win margin *decays* with the king's tenure (0.02 -> 0.005 over 8 rounds), so the
bar moves under you while you iterate.
"""
from __future__ import annotations

from ..base import (CompEvent, SubnetAdapter, changed, esc, fetch_json, num,
                    short, two_col)

BUCKET = "https://s3.hippius.com/cascade-manifests"
SITE = "https://cascade.tensorlink.ai"

EPOCH_BLOCKS = 3600
BLOCK_S = 12.0
# chain.toml [scoring]: fresh-king margin decaying to a floor over N tenure
# rounds. Mirrored for the *projection* in render_state only -- every diff()
# reads the margin the receipt actually carries.
MARGIN_START, MARGIN_END, MARGIN_WARMUP = 0.02, 0.005, 8

# Our generators all deploy under this Hippius namespace, so ownership is read
# off the artifact ref rather than a hand-maintained hotkey list -- a new
# registration is picked up with no code change.
OUR_NS = "tony/"

# Minutes-before-boundary at which the countdown speaks. A 12h window with a
# for-life hotkey cost is a planning decision long before it is an urgent one,
# so the early marks are quiet and the late ones are not. One alert per mark
# per boundary -- the dedup key carries both.
DEADLINE_MARKS = (360, 180, 60, 30, 15)
# [round] reveal_margin_blocks = 25 -- the timelock opens ~5 min out, which is
# why the visible field is empty for almost the whole window.
REVEAL_MARGIN_MIN = 25 * BLOCK_S / 60


def _hm(minutes) -> str:
    """`105` -> `1h45m`. Countdown marks read wrong as bare minutes."""
    if minutes is None:
        return "?"
    m = int(minutes)
    return f"{m//60}h{m%60:02d}m" if m >= 60 else f"{m}m"


def _margin_at(tenure: int) -> float:
    frac = min(max(tenure, 0) / MARGIN_WARMUP, 1.0)
    return MARGIN_START + frac * (MARGIN_END - MARGIN_START)


def _field_line(s: dict) -> str:
    """How much of the next round's field is visible -- and why it is not.

    Commit-reveal means a rival's entry stays sealed until ~REVEAL_MARGIN_BLOCKS
    before the boundary, so `n_pending` reads 0 for ~11h55m of a 12h window.
    Printing the bare count made the alert say "0 commitments revealed", which
    reads as "the field is empty, we would walk it" when it actually means
    "the field is hidden". Same failure as absence-is-not-a-change, one layer up.
    """
    n = s.get("n_pending")
    if n is None:
        return "field unknown"
    if n:
        return f"{n} commitments revealed"
    return (f"0 revealed — <i>sealed until ~{REVEAL_MARGIN_MIN:.0f} min "
            f"before the boundary; the field is hidden, not empty</i>")


def _scored_rounds(idx: dict) -> dict:
    """epoch -> the scored receipt for it.

    Every round also gets a `rejected` receipt from one permanently
    misconfigured validator (contract_digest_mismatch, identical hash on every
    round including ones that dethroned). Taking `status == scored` drops it.
    """
    out: dict = {}
    for r in idx.get("rounds") or []:
        if r.get("status") == "scored" and r.get("lcb") is not None:
            e = r.get("epoch_start_block")
            if isinstance(e, int):
                out[e] = r
    return out


class SN91(SubnetAdapter):
    # ---- identity -----------------------------------------------------------
    netuid = 91
    slug = "cascade"
    label = "Cascade"

    # A 12h round with a ~1.25h heat and a 3.25h duel; 5 min resolves every
    # transition we act on and still catches the deadline warning in time.
    poll_seconds = 300

    repos: list[str] = ["TensorLink-AI/cascade", "TensorLink-AI/cascade-miner"]

    links = {
        "receipts": f"{BUCKET}/receipts/index.json",
        "status": f"{BUCKET}/status/round.json",
        "site": SITE,
    }

    # What each alert MEANS here -- rendered by the base class into /guide.
    alerts = {
        "king": "the throne changed hands — the artifact to fork moved",
        "margin": "the dethrone bar moved; it DECAYS 0.02 → 0.005 as a king's "
                  "tenure grows, so a long reign is the cheap moment to attack",
        "verdict": "a duel settled. A `cohort duel k>1` line means the heat could "
                   "not separate the top, the whole tied cohort duelled, and every "
                   "member was judged at bootstrap_alpha/k — a TIGHTER bar, not a "
                   "looser one",
        "heat": "the screen published and one of ours was in it",
        "heat_field": "the screen published with none of ours in it",
        "stage": "the round moved heat → duel → validation → settled",
        "window_open": "a boundary passed; this is the moment to decide whether to "
                       "spend a registration — one hotkey = one submission, for life",
        "deadline": "the reveal countdown. `0 revealed` is the TIMELOCK, not an "
                    "empty field: rivals stay sealed until ~5 min before the boundary",
    }

    # ---- 1. COLLECT ---------------------------------------------------------
    async def snapshot(self) -> dict:
        s: dict = {}

        chain = await fetch_json(f"{BUCKET}/status/chain.json")
        if chain and isinstance(chain.get("current_block"), int):
            blk = chain["current_block"]
            epoch = chain.get("epoch_start_block")
            s["block"] = blk
            if isinstance(epoch, int):
                boundary = epoch + EPOCH_BLOCKS
                s["epoch_start"] = epoch
                s["boundary"] = boundary
                s["deadline_min"] = round(max(boundary - blk, 0) * BLOCK_S / 60)
            econ = chain.get("economics") or {}
            if econ.get("alpha_price_tao") is not None:
                s["alpha_tao"] = econ["alpha_price_tao"]

            # Commitments revealed for the round that is about to lock. The
            # reveal must land strictly before the boundary, so entries for the
            # NEXT round are the ones committed after this epoch began.
            subs = chain.get("submissions") or []
            if isinstance(epoch, int) and subs:
                pend = [x for x in subs
                        if isinstance(x.get("commit_block"), int)
                        and x["commit_block"] >= epoch]
                s["n_pending"] = len(pend)
                s["n_pending_ours"] = sum(
                    1 for x in pend if str(x.get("gen_ref") or "").startswith(OUR_NS))

        rnd = await fetch_json(f"{BUCKET}/status/round.json")
        if rnd and rnd.get("stage"):
            s["stage"] = rnd["stage"]
            s["round_id"] = str(rnd.get("round_id") or "")
            s["round_epoch"] = rnd.get("epoch_start_block")
            if rnd.get("heat_total") is not None:
                s["heat_done"] = rnd.get("heat_done")
                s["heat_total"] = rnd.get("heat_total")

        heat = await fetch_json(f"{BUCKET}/status/heat.json")
        if heat and heat.get("entrants"):
            ents = [e for e in heat["entrants"] if e.get("crps") is not None]
            if ents:
                s["heat_epoch"] = heat.get("epoch_start_block")
                s["n_entrants"] = len(ents)
                ents.sort(key=lambda e: e.get("rank") or 9999)
                top = ents[0]
                s["heat_leader_uid"] = top.get("uid")
                s["heat_leader_crps"] = top.get("crps")
                s["heat_board"] = [{
                    "rank": e.get("rank"),
                    "uid": e.get("uid"),
                    "crps": e.get("crps"),
                    "status": e.get("status"),
                    "ours": str(e.get("gen_ref") or "").startswith(OUR_NS),
                } for e in ents[:20]]
                ours = [e for e in ents if str(e.get("gen_ref") or "").startswith(OUR_NS)]
                if ours:
                    best = min(ours, key=lambda e: e.get("rank") or 9999)
                    s["our_heat_rank"] = best.get("rank")
                    s["our_heat_crps"] = best.get("crps")
                    s["our_heat_n"] = len(ours)
                    s["our_advanced"] = any(e.get("status") == "advanced" for e in ours)

        idx = await fetch_json(f"{BUCKET}/receipts/index.json")
        if idx:
            rounds = _scored_rounds(idx)
            if rounds:
                last_e = max(rounds)
                r = rounds[last_e]
                s["last_epoch"] = last_e
                s["last_lcb"] = r.get("lcb")
                s["last_margin"] = r.get("margin")
                s["last_dethroned"] = bool(r.get("dethroned"))
                s["last_chal_uid"] = r.get("chal_uid")
                s["last_chal_ours"] = str(r.get("chal_gen_ref") or "").startswith(OUR_NS)
                # DEC-CA-0012 cohort duel. k>1 means the heat could not separate
                # the top, so the WHOLE tied cohort duelled under a family-wise
                # bootstrap_alpha/k -- every member's bar was tightened, not
                # loosened. 0/None = an ordinary single-challenger round.
                if r.get("cohort_k") is not None:
                    s["cohort_k"] = int(r.get("cohort_k") or 0)
                if r.get("cohort_alpha") is not None:
                    s["cohort_alpha"] = r.get("cohort_alpha")
                s["king_uid"] = r.get("post_round_king_uid") or r.get("king_uid")
                s["king_hk"] = r.get("king_hotkey") or ""
                s["king_ref"] = r.get("king_gen_ref") or ""
                s["reward_uids"] = list(r.get("reward_uids") or [])
                s["n_scored_rounds"] = len(rounds)

                # Tenure = consecutive settled rounds this king has held.
                king = s["king_uid"]
                tenure = 0
                for e in sorted(rounds, reverse=True):
                    rr = rounds[e]
                    if (rr.get("post_round_king_uid") or rr.get("king_uid")) != king:
                        break
                    tenure += 1
                    if rr.get("dethroned"):
                        break
                s["king_tenure"] = tenure
        return s

    async def seed_watchlist(self) -> list[dict]:
        # Ownership is derived from the OUR_NS artifact prefix, so nothing has
        # to be seeded or kept in sync by hand.
        return []

    # ---- 2. DIFF ------------------------------------------------------------
    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        out: list[CompEvent] = []
        if not old:
            return out                      # first sight is a baseline

        # -- the throne moved: our target, and the whole ladder, changed.
        #    uid and hotkey come from the same receipt, so either moving is the
        #    same event; checking both catches a uid recycled onto a new key.
        if changed(old, new, "king_uid") or changed(old, new, "king_hk"):
            out.append(CompEvent(
                kind="king", severity="critical", icon="👑",
                title="New king",
                body=((f"uid {old.get('king_uid')} → <b>{new.get('king_uid')}</b>\n"
                       if old.get("king_uid") != new.get("king_uid")
                       else f"uid {new.get('king_uid')} · new hotkey\n"
                            f"{esc(short(new.get('king_hk') or ''))}\n")
                      + f"{esc(new.get('king_ref') or '')[:60]}\n"
                      f"<i>margin resets to {num(MARGIN_START, 3)} and decays again</i>"),
                dedup_key=f"king:{new.get('king_uid')}"))

        # -- the bar itself moved. Decays every round the king survives, so the
        #    number to beat is not the one you planned against yesterday.
        if changed(old, new, "last_margin"):
            o, n = old.get("last_margin"), new.get("last_margin")
            out.append(CompEvent(
                kind="margin", severity="critical", icon="🎯",
                title="Win margin changed",
                body=(f"{num(o, 6)} → <b>{num(n, 6)}</b>"
                      + (f"\nking tenure {new.get('king_tenure')}"
                         if new.get("king_tenure") is not None else "")
                      + ("\n<i>at the floor — lowest it will go</i>"
                         if n is not None and n <= MARGIN_END else "")),
                dedup_key=f"margin:{n}"))

        # -- a duel settled. Ours or not, the LCB is the only number that pays.
        if changed(old, new, "last_epoch") and new.get("last_lcb") is not None:
            lcb, mg = new["last_lcb"], new.get("last_margin")
            ours = new.get("last_chal_ours")
            short_by = (mg - lcb) if mg is not None else None
            sev = "good" if new.get("last_dethroned") else ("warn" if ours else "info")
            out.append(CompEvent(
                kind="verdict", severity=sev, icon="⚔️",
                title=("We took the throne" if (ours and new.get("last_dethroned"))
                       else "Our duel lost" if ours
                       else "Duel settled"),
                body=(f"lcb <b>{num(lcb, 5)}</b> vs margin {num(mg, 5)}\n"
                      + (f"short by {num(short_by, 5)}\n" if short_by and short_by > 0 else "")
                      + f"challenger uid {new.get('last_chal_uid')}"
                      + (" · <b>ours</b>" if ours else "")
                      + (f"\n<b>cohort duel k={new['cohort_k']}</b> — alpha "
                         f"{num(new.get('cohort_alpha'), 4)}, every member judged "
                         f"on a TIGHTER bound"
                         if (new.get("cohort_k") or 0) > 1 else "")),
                dedup_key=f"verdict:{new.get('last_epoch')}"))

        # -- heat standings published: did we survive the screen?
        if changed(old, new, "heat_epoch") and new.get("n_entrants"):
            if new.get("our_heat_rank"):
                adv = new.get("our_advanced")
                out.append(CompEvent(
                    kind="heat", severity="good" if adv else "warn",
                    icon="🏁" if adv else "🔻",
                    title="Advanced to the duel" if adv else "Screened out in the heat",
                    body=(f"best rank <b>{new['our_heat_rank']}</b> of "
                          f"{new['n_entrants']} · crps {num(new.get('our_heat_crps'), 6)}\n"
                          f"leader uid {new.get('heat_leader_uid')} "
                          f"crps {num(new.get('heat_leader_crps'), 6)}\n"
                          f"{new.get('our_heat_n')} of ours entered"),
                    dedup_key=f"heat:{new.get('heat_epoch')}"))
            else:
                out.append(CompEvent(
                    kind="heat_field", severity="info", icon="🏁",
                    title="Heat settled",
                    body=(f"{new['n_entrants']} entrants · leader uid "
                          f"{new.get('heat_leader_uid')} "
                          f"crps {num(new.get('heat_leader_crps'), 6)}\n"
                          f"<i>none of ours in this round</i>"),
                    dedup_key=f"heatf:{new.get('heat_epoch')}"))

        # -- stage transition. Cheap, and it is how you know scoring is close.
        if changed(old, new, "stage"):
            out.append(CompEvent(
                kind="stage", severity="info", icon="⏱",
                title=f"Round stage → {esc(new.get('stage') or '?')}",
                body=(f"{esc(str(old.get('stage')))} → <b>{esc(str(new.get('stage')))}</b>"
                      + (f"\nheat {new.get('heat_done')}/{new.get('heat_total')}"
                         if new.get("heat_total") else "")),
                dedup_key=f"stage:{new.get('round_epoch')}:{new.get('stage')}",
                cooldown_h=0.5))

        # -- a boundary passed: the previous round locked and a fresh
        #    submission window opened. This is the moment to decide whether to
        #    spend a registration, so it is announced with the whole schedule.
        if changed(old, new, "boundary"):
            mins = new.get("deadline_min")
            out.append(CompEvent(
                kind="window_open", severity="info", icon="🟢",
                title="Submission window open",
                body=(f"next round locks at block <b>{new.get('boundary')}</b>\n"
                      + (f"<b>{_hm(mins)}</b> to reveal\n" if mins is not None else "")
                      + "<i>reveal must land before the boundary; "
                        "1 hotkey = 1 submission, for life</i>"),
                dedup_key=f"open:{new.get('boundary')}"))

        # -- countdown. Fires once per mark per boundary, louder as it closes
        #    and only loud at all while we have nothing revealed.
        o_min, n_min = old.get("deadline_min"), new.get("deadline_min")
        if isinstance(n_min, (int, float)) and isinstance(o_min, (int, float)):
            ours_in = bool(new.get("n_pending_ours"))
            # Ascending: after a poller outage that skips several marks,
            # the MOST urgent applicable one must fire, not the earliest.
            for mark in sorted(DEADLINE_MARKS):
                if n_min <= mark < o_min:
                    if ours_in:
                        sev, icon = "info", "⏳"
                        tail = (f"{new.get('n_pending_ours')} of ours revealed "
                                f"— we are in this round")
                    elif mark <= 30:
                        sev, icon = "warn", "🔴"
                        tail = "<b>nothing of ours revealed</b> — last call"
                    elif mark <= 60:
                        sev, icon = "warn", "⏳"
                        tail = "<b>nothing of ours revealed</b>"
                    else:
                        sev, icon = "info", "⏳"
                        tail = "nothing of ours revealed yet"
                    out.append(CompEvent(
                        kind="deadline", severity=sev, icon=icon,
                        title=f"Window closes in {_hm(mark)}",
                        body=(f"boundary block <b>{new.get('boundary')}</b>\n"
                              f"{_field_line(new)}\n"
                              f"{tail}"),
                        dedup_key=f"dl:{new.get('boundary')}:{mark}"))
                    break

        return out

    # ---- 3. RENDER ----------------------------------------------------------
    def render_state(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b>"]
        if s.get("round_epoch"):
            L.append(f"round <code>{s['round_epoch']}</code> · "
                     f"stage <b>{esc(str(s.get('stage') or '?'))}</b>")
        L.append("")

        if s.get("king_uid") is not None:
            ten = s.get("king_tenure")
            L += [f"<b>👑 KING</b>  uid {s['king_uid']}"
                  + (f" · tenure {ten}" if ten is not None else ""),
                  f"<code>{esc(str(s.get('king_ref') or ''))[:44]}</code>"]
            mg = s.get("last_margin")
            if mg is not None:
                nxt = _margin_at((ten or 0) + 1)
                L.append(f"bar to dethrone <b>{num(mg, 6)}</b>"
                         + (f" → {num(nxt, 6)} next round" if nxt < mg else " (floor)"))
        else:
            L.append("<b>👑 KING</b>  receipts unavailable")
        L.append("")

        if s.get("our_heat_rank"):
            L.append(f"<b>🏁 US</b>  heat rank {s['our_heat_rank']} of "
                     f"{s.get('n_entrants')} · crps {num(s.get('our_heat_crps'), 6)}"
                     + ("  <b>ADVANCED</b>" if s.get("our_advanced") else ""))
        elif s.get("n_pending_ours"):
            L.append(f"<b>🏁 US</b>  {s['n_pending_ours']} revealed for the next round")
        else:
            L.append("<b>🏁 US</b>  nothing entered")

        if s.get("last_lcb") is not None:
            L.append(f"last duel  lcb <code>{num(s['last_lcb'], 5)}</code> vs "
                     f"{num(s.get('last_margin'), 5)}"
                     + ("  <b>DETHRONED</b>" if s.get("last_dethroned") else ""))

        dl = s.get("deadline_min")
        if dl is not None:
            urgent = dl <= 60 and not s.get("n_pending_ours")
            L += ["", f"{'🔴' if urgent else '⏳'} window closes in "
                      f"<b>{_hm(dl)}</b>  ·  block {s.get('boundary')}",
                  f"{s.get('n_pending', 0)} revealed, "
                  f"<b>{s.get('n_pending_ours', 0)} ours</b>"]
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        # two_col packs TWO pairs per row at 18 chars each -- keep every
        # "label value" under ~17 or the columns collide on a phone.
        rows = [("netuid", "91"), ("round", "12h"),
                ("heat", "1.25h"), ("duel", "3.25h"),
                ("advance", "top 1"), ("cp", "1 round"),
                ("margin", "2%->0.5%"), ("warmup", "8 rnds"),
                ("rewards", "king+4"), ("decay", "0.5x")]
        if s.get("alpha_tao") is not None:
            rows.append(("alpha", f"{num(s['alpha_tao'], 5)}"))
        if s.get("n_scored_rounds"):
            rows.append(("rounds", str(s["n_scored_rounds"])))
        return "\n".join([
            f"<b>SN{self.netuid} · {esc(self.label)}</b>",
            "<i>submit a data generator, not a model</i>",
            "",
            two_col(rows),
            "",
            "dethrone: paired-bootstrap LCB must clear the",
            "tenure-decayed margin. One hotkey = one",
            "submission, for life — entering the heat burns it.",
        ])

    def render_board(self, s: dict, limit: int = 10) -> str:
        board = s.get("heat_board") or []
        if not board:
            return (f"<b>SN{self.netuid}</b> heat standings not published yet"
                    + (f"\nstage <b>{esc(str(s.get('stage')))}</b>"
                       f" · {s.get('heat_done')}/{s.get('heat_total')} screened"
                       if s.get("heat_total") else ""))
        rows = []
        for e in board[:limit]:
            tag = "◀" if e.get("ours") else " "
            adv = "▲" if e.get("status") == "advanced" else " "
            rows.append((f"{e.get('rank'):>3} uid {e.get('uid')}",
                         f"{num(e.get('crps'), 6)}{adv}{tag}"))
        return (f"<b>SN{self.netuid} · heat</b> "
                f"({s.get('n_entrants')} entrants)\n" + two_col(rows)
                + "\n<i>▲ advanced   ◀ ours</i>")

    def render_me(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · ours</b>"]
        if s.get("our_heat_rank"):
            L.append(f"heat rank <b>{s['our_heat_rank']}</b> of {s.get('n_entrants')} · "
                     f"crps {num(s.get('our_heat_crps'), 6)} · "
                     f"{s.get('our_heat_n')} entered"
                     + ("\n<b>advanced to the duel</b>" if s.get("our_advanced") else ""))
        if s.get("n_pending_ours"):
            L.append(f"{s['n_pending_ours']} revealed for the next round "
                     f"(of {s.get('n_pending', 0)})")
        if s.get("last_chal_ours"):
            L.append(f"last duel was ours · lcb {num(s.get('last_lcb'), 5)} "
                     f"vs {num(s.get('last_margin'), 5)}")
        if len(L) == 1:
            L.append(f"nothing entered · artifacts under <code>{esc(OUR_NS)}</code>")
        return "\n".join(L)

    def ours_summary(self, s: dict) -> str:
        if s.get("our_advanced"):
            return f"in the duel · heat #{s.get('our_heat_rank')}"
        if s.get("our_heat_rank"):
            return (f"screened out · heat #{s['our_heat_rank']} "
                    f"of {s.get('n_entrants')}")
        if s.get("n_pending_ours"):
            return f"{s['n_pending_ours']} revealed for the next round"
        return ""

    def deadline_minutes(self, s: dict) -> int | None:
        dl = s.get("deadline_min")
        return int(dl) if isinstance(dl, (int, float)) else None

    def render_digest(self, s: dict) -> str:
        bits = [f"king uid {s.get('king_uid', '?')}"]
        if s.get("king_tenure") is not None:
            bits.append(f"t{s['king_tenure']}")
        if s.get("last_margin") is not None:
            bits.append(f"bar {num(s['last_margin'], 5)}")
        if s.get("stage"):
            bits.append(esc(str(s["stage"])))
        if s.get("our_heat_rank"):
            bits.append(f"us #{s['our_heat_rank']}")
        return f"<b>SN91</b> " + " · ".join(bits)
