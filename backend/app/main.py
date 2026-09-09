import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .api import auth, market, meta, miners, notify, portfolio, screen, subnets, ws
from .chain import poller
from .comp import poller as comp_poller
from .credits import poller as credits_poller
from .telegram import bot as telegram_bot
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)

# bittensor 10.5.0 sets EVERY logger that already exists to CRITICAL the moment
# it is imported (via app/chain/client.py). Our own loggers created BEFORE that
# import -- taoscope.telegram and taoscope.comp.* -- were therefore muted
# outright: no "telegram bot online", no "competition tracking", and, worst of
# all, no `telegram poll failed` traceback. Loggers created after the import are
# untouched, which is why taoscope.chain kept logging and hid the problem.
#
# Undo it for our namespace only, after every import has run.
for _name in list(logging.root.manager.loggerDict):
    if _name == "taoscope" or _name.startswith("taoscope."):
        logging.getLogger(_name).setLevel(logging.NOTSET)

log = logging.getLogger("taoscope")

tasks: list = []


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await db.migrate()
    await auth.ensure_admin(settings.admin_email, settings.admin_password)
    tasks.extend(poller.start())
    tasks.extend(telegram_bot.start())
    if settings.comp_enabled:
        tasks.extend(comp_poller.start())
    if settings.credits_enabled:
        tasks.extend(credits_poller.start())
    log.info("taoscope up: network=%s", settings.chain_endpoint or settings.network)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await db.close()


app = FastAPI(title="TaoScope", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_url] if settings.public_url else [],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["content-type", "authorization", "x-requested-with"],
)

for r in (auth.router, subnets.router, miners.router, meta.router, market.router,
          screen.router, portfolio.router, notify.router, ws.router):
    app.include_router(r)


@app.get("/health")
async def health():
    return {"ok": True}
