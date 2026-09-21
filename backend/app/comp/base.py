"""The per-subnet adapter contract.

Every mining subnet runs its own competition with its own dashboard, its own
vocabulary ("submission", "entry", "attempt", "round") and its own idea of what
winning means. None of that fits a shared schema, so an adapter owns all of it
and the framework owns only:

  * polling on a cadence
  * diffing this snapshot against the last one, restart-safe
  * cooldown/dedup so a flapping value cannot spam the chat
  * routing the resulting event to the right forum topic
  * answering /state, /info, /board, /me out of the stored snapshot

Adding a subnet is one file: subclass SubnetAdapter, implement snapshot() and
diff(), write four renderers. Nothing else in the stack changes.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("taoscope.comp")

# severity -> (icon, is the phone allowed to buzz)
#
# Routine board churn arrives silently; only things that change what we would DO
# are allowed to make a noise. Getting this wrong is how a monitor gets muted,
# and a muted monitor is worth nothing.
SEVERITY = {
    "info":     ("•",  False),
    "good":     ("✅", False),
    "warn":     ("⚠️", True),
    "critical": ("🚨", True),
}


@dataclass
class CompEvent:
    kind: str
    title: str
    body: str = ""
    severity: str = "info"
    # Shown instead of the severity glyph. Set it when the adapter knows
    # something more informative than "this is a warning" -- a pipeline stage,
    # a crown, a repo. Severity still decides whether the phone buzzes.
    icon: str = ""
    dedup_key: str = ""
    cooldown_h: float = 0.0
    detail: dict = field(default_factory=dict)


def changed(old: dict, new: dict, *keys: str) -> bool:
    """True only if EVERY key was actually fetched this round, and one differs.

    A failed fetch omits its keys from the snapshot, and `.get()` on a missing
    key returns None -- so comparing raw `.get()`s turns a backend outage into a
    fake "pin changed -> None". That is not hypothetical: on 2026-08-18 09:31Z
    the SN100 API returned `503 no healthy backends` and a watcher built the
    naive way announced that every patch we were holding had been invalidated.

    Absence is not a change. Adapters must route every optional field through
    this rather than comparing dicts directly.
    """
    if any(k not in new for k in keys):
        return False
    # Symmetric: a key ABSENT from `old` is being seen for the first time --
    # a newly tracked field, or one restored after a failed fetch. First sight
    # is a baseline, never an alert, in both directions.
    if any(k not in old for k in keys):
        return False
    return any(old.get(k) != new.get(k) for k in keys)


def esc(v) -> str:
    return html.escape(str(v), quote=False)


def num(v, d: int = 2) -> str:
    try:
        return f"{float(v):,.{d}f}"
    except (TypeError, ValueError):
        return "—"


def pct(v, d: int = 1) -> str:
    try:
        return f"{float(v) * 100:.{d}f}%"
    except (TypeError, ValueError):
        return "—"


def short(ss58: str | None, head: int = 8, tail: int = 4) -> str:
    if not ss58:
        return "—"
    return ss58 if len(ss58) <= head + tail + 1 else f"{ss58[:head]}…{ss58[-tail:]}"


def arrow(old, new) -> str:
    try:
        return "▲" if float(new) > float(old) else "▼"
    except (TypeError, ValueError):
        return "→"


def sci(v) -> str:
    """3e+18 -> 3.0e18. A raw float repr of a FLOP budget is unreadable."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f == 0:
        return "0"
    if 1e-3 <= abs(f) < 1e6:
        return f"{f:,.10g}"
    mant, _, exp = f"{f:.1e}".partition("e")
    return f"{mant}e{int(exp)}"


def clean_error(text: str | None, limit: int = 110) -> str:
    """Reduce a stack of wrapped error prefixes to the sentence that matters.

    Platform errors arrive as `measure: provision: lium api: lium api: POST
    /executors/<uuid>/rent -> 400 Bad Request: {"success":false,"message":"..."}`
    or as a diagnosis followed by a whole harness log. Pasted raw, either is
    several lines of noise.

    Order matters: the wrapper prefixes come off FIRST, because the useful
    clause can be either the JSON `message` or the diagnosis right behind the
    prefixes -- and the closing quote of that JSON is often already gone, since
    the platform truncates `error_detail` itself. So the string match must not
    require one.
    """
    if not text:
        return ""
    s = str(text)

    # Short lowercase wrapper segments only ("measure: ", "lium api: ") -- never
    # a real sentence, which starts with a capital or contains punctuation.
    for _ in range(6):
        m = re.match(r"[a-z][a-z0-9 _-]{0,14}: ", s)
        if not m:
            break
        s = s[m.end():]

    body = re.search(r'"(?:message|error|detail)"\s*:\s*"((?:[^"\\]|\\.)*)"?', s)
    if body:
        s = body.group(1).replace('\\"', '"').replace("\\n", " ")
    else:
        # A diagnosis followed by a log: the first line is the diagnosis.
        s = next((ln for ln in s.splitlines() if ln.strip()), s)

    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
               "<id>", s)
    s = " ".join(s.split())
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")


def two_col(pairs: list[tuple[str, str]], width: int = 18) -> str:
    """Aligned two-column monospace block -- readable on a phone."""
    out = []
    for i in range(0, len(pairs), 2):
        row = "".join(f"{k} {v}".ljust(width) for k, v in pairs[i:i + 2])
        out.append(row.rstrip())
    return "<pre>" + "\n".join(out) + "</pre>"


def diff_block(lines: list[str]) -> str:
    """A monospace block a client will COLOUR, for a table with rows of ours in it.

    Telegram's HTML has no colour markup at all -- the entire tag list is
    <b> <i> <u> <s> <code> <pre> <a> <blockquote>. The one place a client paints
    text is inside a syntax-highlighted code block, and `language-diff` gives
    three usable colours off the FIRST CHARACTER of each line:

        "+ ..."   green        "- ..."   red        "  ..."   plain

    So a table whose rows carry that prefix picks out our own rows where the
    client highlights, and reads as ordinary aligned monospace where it does
    not. That degradation is the point: the marker is a real character in a real
    column rather than styling, so nothing is lost when the colour is -- some
    clients and older versions do not highlight at all, and a board that only
    worked in colour would be unreadable on those.

    `router.BANNER` uses the same three characters for event severity; keep the
    meanings aligned (`+` = ours/good, `-` = red/critical) so one convention
    covers both.

    Callers must esc() anything that came from an API BEFORE passing it here.
    A <pre> block is still HTML-parsed: one agent named with a stray `<` and
    Telegram rejects the whole message.
    """
    return ('<pre><code class="language-diff">' + "\n".join(lines)
            + "</code></pre>")


async def fetch_json(url: str, timeout: float = 30.0, **kw):
    """GET JSON, returning None on any failure.

    None means "unknown", never "empty" -- the caller must omit the key rather
    than record a zero, or `changed()` above cannot do its job.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cx:
            r = await cx.get(url, headers={"accept": "application/json"}, **kw)
            if r.status_code != 200:
                log.debug("fetch %s -> HTTP %s", url, r.status_code)
                return None
            return r.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch %s failed: %s", url, exc)
        return None


def chain_block(s: dict) -> str:
    """Operator / registration facts, rendered the same way for EVERY subnet.

    The poller merges these into every snapshot as `_chain`, so no adapter has
    to fetch or render them and none can drift from the others.

    Why they belong in /state rather than only /info: a competitor must
    burn-register a hotkey before they can submit anything, so the operator
    count is the earliest PUBLIC signal that a field is growing. On subnets
    whose in-flight rosters are private it is the only such signal -- SN98
    returns 403 on the roster of any round that has not completed, so "who is
    submitting right now" is unanswerable and "how many operators exist" is the
    closest available proxy.
    """
    ch = s.get("_chain") or {}
    if not ch:
        return ""
    reg = "OPEN" if ch.get("registration_allowed") else "closed"
    # "operators" counts coldkeys holding a UID; "earning" counts the ones the
    # emission actually reaches. The gap is the real answer to "how big is this
    # field" -- SN91 shows 36 and 3.
    earn = ch.get("earning_coldkeys")
    ops = str(ch.get("unique_coldkeys") or "?")
    rows = [("operators", ops if earn is None else f"{ops} ({earn} earn)"),
            ("uids", f"{ch.get('num_uids')}/{ch.get('max_uids')}"),
            ("entry", f"τ{num(ch.get('burn_tao'), 4)}"),
            ("reg", reg)]
    em = ch.get("emission_share")
    if em is not None:
        rows.append(("emission", pct(em, 3)))   # pct() already scales by 100
    tph = ch.get("realized_tao_per_hour")
    if tph is not None:
        rows.append(("τ/h", num(tph, 3)))
    out = "\n\n<b>⛓ CHAIN</b>\n" + two_col(rows, 20)
    line = ours_line(s)
    if line:
        out += f"\n<b>🔑 OURS</b> {line}"
    return out


def ours_line(s: dict) -> str:
    """Our UIDs on this subnet in one phrase, from the poller's `_ours_chain`.

    "" when the sweep was not read or we hold nothing -- a subnet we are not on
    must not grow an "0 uids" line in every /state."""
    o = s.get("_ours_chain") or {}
    if not o.get("n"):
        return ""
    bits = [f"{o['n']} uid{'s' if o['n'] != 1 else ''}",
            f"{o.get('earning', 0)} earning",
            f"τ{num(o.get('tpd'), 2)}/day"]
    ranks = [u["rank"] for u in o.get("uids") or [] if u.get("rank") and u.get("tpd")]
    if ranks:
        bits.append(f"best rank #{min(ranks)}")
    return " · ".join(bits)


def ours_chain_block(s: dict, limit: int = 40) -> str:
    """Every UID we hold on this subnet -- the same table in every topic.

    Earning rows carry the `+` diff marker, so they are green where the client
    highlights and still marked where it does not."""
    o = s.get("_ours_chain")
    if o is None:
        return "<b>🔑 OUR HOTKEYS</b>\n<i>chain sweep not read yet</i>"
    if not o.get("n"):
        return "<b>🔑 OUR HOTKEYS</b>\n<i>none of our coldkeys holds a UID here</i>"
    uids = sorted(o.get("uids") or [], key=lambda u: (-(u.get("tpd") or 0), u["uid"]))
    L = [f"{'':2}{'uid':>4} {'hotkey':<13} {'rank':>4} {'τ/day':>7}"]
    for u in uids[:limit]:
        flag = ("🛡" if u.get("immune") else "") + ("V" if u.get("vp") else "")
        L.append(f"{'+ ' if u.get('tpd') else '  '}{u['uid']:>4} "
                 f"{esc(short(u.get('hk'), 6, 4)):<13} "
                 f"{str(u.get('rank') or '—'):>4} {num(u.get('tpd'), 2):>7} {flag}")
    more = len(uids) - limit
    return (f"<b>🔑 OUR HOTKEYS</b> · {ours_line(s)}\n" + diff_block(L)
            + (f"\n<i>+{more} more</i>" if more > 0 else "")
            + "\n<i>+ earning · 🛡 immune · V validator permit</i>")


# Alerts every subnet topic gets from the framework, whatever its adapter.
GENERIC_ALERTS = {
    "registration": "registration opened or closed on chain",
    "operators": "someone burn-registered — new operators precede new submissions",
    "our_earning": "all of our UIDs here stopped earning, or started again",
    "my_miners": "one of our UIDs was deregistered (taken by another hotkey)",
    "king_change": "a different coldkey became this subnet's top earner",
    "repo": "a watched repo moved — often the earliest sign the rules changed",
}


class SubnetAdapter:
    """Base class. Subclass per subnet."""

    netuid: int = 0
    slug: str = ""
    label: str = ""
    poll_seconds: int = 180
    # "owner/repo" or "owner/repo@branch" -- watched generically for all subnets
    repos: list[str] = []
    # extra dashboard links surfaced by /info
    links: dict[str, str] = {}
    # Generic alerts this adapter already reports in its own words. The
    # framework then leaves them out of this subnet's topic instead of saying
    # the same thing twice. Values: "dereg" (one of our UIDs was taken over).
    covers: frozenset[str] = frozenset()

    # ---- required ----
    async def snapshot(self) -> dict:
        """Fetch the subnet's competition state as a flat-ish dict of scalars.

        MUST omit a key entirely when its source failed. MUST NOT substitute a
        zero or a None for an unknown -- see changed().
        """
        raise NotImplementedError

    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        """Events implied by the move from `old` to `new`. `old` may be {}."""
        raise NotImplementedError

    # ---- renderers (HTML, Telegram-safe) ----
    def render_state(self, s: dict) -> str:
        raise NotImplementedError

    def render_info(self, s: dict) -> str:
        return "No rules view for this subnet yet."

    def render_board(self, s: dict, limit: int = 10) -> str:
        return "No leaderboard view for this subnet yet."

    def render_me(self, s: dict) -> str:
        return "No submission view for this subnet yet."

    def pick_options(self, s: dict) -> list[dict]:
        """Items /pick can narrow this topic to: [{key, label, note, mine}].

        `key` is what gets stored, `label` is what the operator sees, `mine`
        marks the ones we are actually competing in. An empty list means this
        subnet has nothing to choose between, and /pick says so.
        """
        return []

    # kind -> one line saying what that alert MEANS and why it is worth reading.
    # Filled per adapter; the generic chain alerts are appended automatically.
    alerts: dict[str, str] = {}

    def render_guide(self, s: dict) -> str:
        """A pinnable primer for this subnet's topic.

        Generic by default so every subnet gets one the moment it is
        registered; override to add the rules that only this subnet has.
        """
        L = [f"<b>📌 SN{self.netuid} · {esc(self.label)}</b>", ""]
        L.append(f"Polled every {self.poll_seconds // 60} min.")
        if self.links:
            L.append(" · ".join(f'<a href="{esc(v)}">{esc(k)}</a>'
                                for k, v in self.links.items()))
        L += ["", "<b>Commands</b>",
              "<code>/state</code> now · <code>/board</code> standings · "
              "<code>/mine</code> our entries",
              "<code>/info</code> the rules · <code>/poll</code> force a refresh",
              "<code>/mute &lt;kind&gt;</code> silence one alert kind"]
        cat = dict(self.alerts)
        for k, v in GENERIC_ALERTS.items():
            if k == "my_miners" and "dereg" in self.covers:
                continue
            if k == "repo" and not self.repos:
                continue
            cat.setdefault(k, v)
        L += ["", "<b>Alerts you will get here</b>"]
        L += [f"• <code>{esc(k)}</code> — {esc(v)}" for k, v in cat.items()]
        if self.repos:
            L += ["", "<b>Watched repos</b>"]
            L += [f"📦 <code>{esc(r)}</code>" for r in self.repos]
        return "\n".join(L)

    def render_digest(self, s: dict) -> str:
        """One line for the cross-subnet /state in the General topic."""
        return f"SN{self.netuid} {self.label}"

    def ours_summary(self, s: dict) -> str:
        """One short phrase describing OUR standing, or "" if we have nothing in.

        Optional. The General-topic board sorts subnets where this is non-empty
        to the top, because those are the ones with something at stake. Keep it
        to a few words -- it shares a line with the subnet name.
        """
        return ""

    def deadline_minutes(self, s: dict) -> int | None:
        """Minutes until this subnet's next submission cutoff, if it has one.

        Optional. The board surfaces the nearest one across all subnets so a
        window is never missed just because its topic was muted.
        """
        return None

    # ---- optional hooks ----
    async def seed_watchlist(self) -> list[dict]:
        """Rows of {ref,label,uid} we should track as ours. Called each poll."""
        return []
