import asyncio
import json
import logging

log = logging.getLogger("taoscope.hub")


class Hub:
    """In-memory live state + websocket fan-out."""

    def __init__(self) -> None:
        self.clients: set = set()
        self.subnets: list[dict] = []
        self.network: dict = {}
        self.tao_usd: float = 0.0
        self.tao_usd_change: float = 0.0
        self.status: dict = {
            "block": 0,
            "last_fast_poll": None,
            "last_neuron_poll": None,
            "neuron_poll_seconds": None,
            "errors": 0,
            "last_error": None,
        }
        self._lock = asyncio.Lock()

    def register(self, ws) -> None:
        self.clients.add(ws)

    def unregister(self, ws) -> None:
        self.clients.discard(ws)

    async def broadcast(self, event: str, payload: dict) -> None:
        if not self.clients:
            return
        msg = json.dumps({"event": event, "data": payload}, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.unregister(ws)


hub = Hub()
