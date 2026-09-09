"""Watch the repos a competition is defined by.

Every subnet publishes its rules as code. A commit to that repo is the earliest
possible warning that the game changed -- earlier than the dashboard, earlier
than any announcement, and often hours before the change goes live. So this is
generic: an adapter lists `repos`, and the framework does the rest.

Two details that decide whether this works at all:

  * Every request is conditional on the stored ETag. A `304 Not Modified`
    returns no body, which is the difference between 277 KB and 0 for a commit
    list.

    **A 304 still COSTS A REQUEST.** Measured against api.github.com on
    2026-08-20, unauthenticated, three consecutive conditional reads of one
    commit list: `x-ratelimit-used` went 31 -> 32 -> 33 -> 34 and `remaining`
    fell 29 -> 28 -> 27 -> 26. GitHub's own documentation says conditional
    requests returning 304 do not count against the primary rate limit; on this
    endpoint that is not what happens, and the header is the authority.

    This matters because the arithmetic was being done against the wrong
    premise. At REPO_POLL_S=600 each watched repo costs 6 requests an hour
    whether or not it moved, so twelve watched repos is 72/hour on their own --
    already past the 60/hour anonymous budget before a single adapter makes an
    extra call. The only real remedies are FEWER REQUESTS or a token; the ETag
    saves bandwidth, not budget.
  * The FIRST sight of a repo is a baseline, never an alert. Otherwise every
    restart announces that every repo just moved.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import httpx

from ..config import settings
from . import store
from .base import CompEvent

log = logging.getLogger("taoscope.comp.github")

API = "https://api.github.com/repos/{repo}/commits/{ref}"

# Unix time until which GitHub has told us we are out of budget.
#
# Unauthenticated is 60 requests/hour PER IP. A 304 is free, so a settled
# watcher costs almost nothing -- but a first sight, a real commit, and every
# request made while already exhausted all cost 1. Once the budget hits zero
# GitHub 403s EVERYTHING, including conditional requests, so continuing to poll
# both stays blind and keeps the budget pinned at zero. Park until the reset the
# response itself names, and one exhausted window stops costing the next one.
_blocked_until = 0.0


def blocked_for() -> int:
    """Seconds until the watcher will try GitHub again. 0 when not blocked."""
    return max(0, int(_blocked_until - time.time()))


# ETags for ad-hoc conditional GETs made through conditional_json(). The repo
# watcher persists its own in `comp_repo`; these are for the extra calls an
# adapter makes (commit lists, range compares), where an in-memory cache is
# proportionate -- the cost of losing them on restart is one request each.
_ETAGS: dict[str, str] = {}


def _note_rate_limit(r: httpx.Response, what: str) -> None:
    """Park the whole watcher when GitHub says the budget is gone."""
    global _blocked_until
    remaining = r.headers.get("x-ratelimit-remaining")
    if r.status_code in (403, 429) and remaining == "0":
        try:
            reset = float(r.headers.get("x-ratelimit-reset") or 0)
        except ValueError:
            reset = 0.0
        # Cap the park at an hour so a bogus header cannot blind us for a day.
        _blocked_until = min(reset, time.time() + 3600) if reset else time.time() + 300
        log.warning("github rate limit exhausted; parking %ds "
                    "(set TAOSCOPE_GITHUB_TOKEN for 5000/hr)", blocked_for())
    else:
        log.warning("github %s -> HTTP %s (%s)", what, r.status_code, remaining)


async def conditional_json(url: str, *, cache_key: str = "",
                           timeout: float = 25.0) -> tuple[object | None, int]:
    """Conditional, authenticated GitHub GET. Returns (body, status).

    Use this for every GitHub call an adapter makes. `base.fetch_json` must not
    be used against api.github.com: it sends neither an `If-None-Match` nor an
    `Authorization` header, so the request can never 304 and is unauthenticated
    even when a token is configured -- which is how one adapter's 600s deploy
    poll quietly consumed 6 of the shared 60/hour budget every hour, and would
    have carried on doing so after the token was set.

    Status matters as much as the body:
      304  positively unchanged. Free against the rate limit. The caller's last
           known value is still true and must be carried forward, not omitted.
      0    parked or unreachable. Unknown -- omit, do not treat as unchanged.
    """
    if blocked_for():
        return None, 0
    headers = {"accept": "application/vnd.github+json"}
    key = cache_key or url
    if _ETAGS.get(key):
        headers["if-none-match"] = _ETAGS[key]
    if settings.github_token:
        headers["authorization"] = f"Bearer {settings.github_token}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as cx:
            r = await cx.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        log.debug("github %s failed: %s", url, exc)
        return None, 0
    if r.status_code == 304:
        return None, 304
    if r.status_code != 200:
        _note_rate_limit(r, url)
        return None, r.status_code
    if r.headers.get("etag"):
        _ETAGS[key] = r.headers["etag"]
    try:
        return r.json(), 200
    except ValueError:
        return None, 200


def parse_spec(spec: str) -> tuple[str, str]:
    """'owner/name@branch' -> ('owner/name', 'branch'); default branch if absent."""
    if "@" in spec:
        repo, _, branch = spec.partition("@")
        return repo, branch
    return spec, ""


def _dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def check(netuid: int, spec: str) -> CompEvent | None:
    global _blocked_until
    if time.time() < _blocked_until:
        return None
    repo, branch = parse_spec(spec)
    prev = await store.repo_row(netuid, repo, branch)

    headers = {"accept": "application/vnd.github+json"}
    if prev.get("etag"):
        headers["if-none-match"] = prev["etag"]
    if settings.github_token:
        headers["authorization"] = f"Bearer {settings.github_token}"

    url = API.format(repo=repo, ref=branch or "HEAD")
    try:
        async with httpx.AsyncClient(timeout=25) as cx:
            r = await cx.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        log.debug("github %s failed: %s", repo, exc)
        return None

    if r.status_code == 304:
        await store.save_repo(netuid, repo, branch)   # refresh checked_at only
        return None
    if r.status_code != 200:
        # 403 here is almost always the rate limit, not a permissions problem.
        _note_rate_limit(r, repo)
        return None

    data = r.json()
    sha = data.get("sha")
    if not sha:
        return None
    commit = data.get("commit") or {}
    subject = (commit.get("message") or "").splitlines()[0][:180]
    author = ((commit.get("author") or {}).get("name") or "")[:80]
    when = _dt((commit.get("author") or {}).get("date"))

    await store.save_repo(netuid, repo, branch, sha=sha, etag=r.headers.get("etag"),
                          subject=subject, author=author, committed_at=when)

    if not prev.get("sha"):
        log.info("repo baseline %s@%s = %s", repo, branch or "HEAD", sha[:10])
        return None
    if prev["sha"] == sha:
        return None

    where = f"{repo}" + (f"@{branch}" if branch else "")
    return CompEvent(
        kind="repo",
        title=f"Repo moved — {where}",
        body=(f"<code>{prev['sha'][:10]}</code> → <code>{sha[:10]}</code>\n"
              f"{subject}\n"
              f"<i>{author}</i>\n"
              f"https://github.com/{repo}/compare/{prev['sha'][:10]}...{sha[:10]}"),
        severity="critical",
        dedup_key=f"{repo}:{sha}",
        detail={"repo": repo, "branch": branch, "old": prev["sha"], "new": sha,
                "subject": subject},
    )


async def check_all(netuid: int, specs: list[str]) -> list[CompEvent]:
    out = []
    for spec in specs:
        ev = await check(netuid, spec)
        if ev:
            out.append(ev)
    return out
