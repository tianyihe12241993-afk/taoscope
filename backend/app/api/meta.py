from fastapi import APIRouter
from pydantic import BaseModel

from ..db import pool
from ..security import CsrfGuard, RequireUser

router = APIRouter(prefix="/api", tags=["meta"])

FIELDS = ["dashboard_url", "github_repo", "docs_url", "discord", "website",
          "twitter", "notes", "tags", "watch", "our_uids", "extra"]


class MetaIn(BaseModel):
    dashboard_url: str | None = None
    github_repo: str | None = None
    docs_url: str | None = None
    discord: str | None = None
    website: str | None = None
    twitter: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    watch: bool | None = None
    our_uids: list[str] | None = None
    extra: dict | None = None


@router.put("/subnets/{netuid}/meta", dependencies=[CsrfGuard])
async def upsert_meta(netuid: int, body: MetaIn, user=RequireUser):
    """Partial update: only the fields actually sent are written."""
    sent = body.model_dump(exclude_unset=True)
    if not sent:
        return {"ok": True, "changed": 0}
    cols = [c for c in FIELDS if c in sent]
    ph = ", ".join(f"${i + 2}" for i in range(len(cols)))
    sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
    vals = [sent[c] for c in cols]
    # jsonb needs a real JSON string through asyncpg
    if "extra" in cols:
        import json
        vals[cols.index("extra")] = json.dumps(sent["extra"])
    await pool().execute(
        f"INSERT INTO subnet_meta (netuid, {', '.join(cols)}) VALUES ($1, {ph})"
        f" ON CONFLICT (netuid) DO UPDATE SET {sets}, updated_at=now()",
        netuid, *vals,
    )
    row = await pool().fetchrow("SELECT * FROM subnet_meta WHERE netuid=$1", netuid)
    return {"ok": True, "meta": dict(row)}


@router.get("/subnets/{netuid}/meta")
async def get_meta(netuid: int, user=RequireUser):
    row = await pool().fetchrow("SELECT * FROM subnet_meta WHERE netuid=$1", netuid)
    return dict(row) if row else {"netuid": netuid}


class ColdkeyLabelIn(BaseModel):
    label: str | None = None
    notes: str | None = None
    is_ours: bool | None = None


@router.put("/coldkeys/{coldkey}/label", dependencies=[CsrfGuard])
async def label_coldkey(coldkey: str, body: ColdkeyLabelIn, user=RequireUser):
    await pool().execute(
        "INSERT INTO coldkey_label (coldkey, label, notes, is_ours) VALUES ($1,$2,$3,COALESCE($4,false))"
        " ON CONFLICT (coldkey) DO UPDATE SET"
        "   label=COALESCE(EXCLUDED.label, coldkey_label.label),"
        "   notes=COALESCE(EXCLUDED.notes, coldkey_label.notes),"
        "   is_ours=COALESCE($4, coldkey_label.is_ours), updated_at=now()",
        coldkey, body.label, body.notes, body.is_ours,
    )
    return {"ok": True}


@router.get("/labels")
async def all_labels(user=RequireUser):
    rows = await pool().fetch("SELECT * FROM coldkey_label ORDER BY is_ours DESC, label")
    return {"labels": [dict(r) for r in rows]}
