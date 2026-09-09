import asyncio
import logging
import threading

import bittensor as bt

from ..config import settings

log = logging.getLogger("taoscope.chain")


class ChainClient:
    """Serialised access to the (synchronous, non-reentrant) substrate client.

    Every call runs in a worker thread behind a lock, and a failed call drops the
    connection so the next one reconnects instead of wedging the poller forever.
    """

    def __init__(self) -> None:
        self._st: bt.Subtensor | None = None
        self._lock = threading.Lock()

    # -- connection -------------------------------------------------
    def _get(self) -> bt.Subtensor:
        if self._st is None:
            target = settings.chain_endpoint or settings.network
            log.info("connecting to subtensor: %s", target)
            self._st = bt.Subtensor(network=target)
        return self._st

    def _reset(self) -> None:
        st, self._st = self._st, None
        try:
            if st is not None and hasattr(st, "close"):
                st.close()
        except Exception:  # noqa: BLE001
            pass

    # -- invocation -------------------------------------------------
    def _call(self, method: str, *args, **kwargs):
        with self._lock:
            last: Exception | None = None
            for attempt in range(2):
                try:
                    return getattr(self._get(), method)(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    log.warning("chain.%s failed (attempt %d): %s", method, attempt + 1, exc)
                    self._reset()
            raise RuntimeError(f"chain.{method} failed: {last}")

    async def call(self, method: str, *args, **kwargs):
        return await asyncio.to_thread(self._call, method, *args, **kwargs)

    async def block(self) -> int:
        return await asyncio.to_thread(lambda: self._get().block)

    async def all_subnets(self):
        return await self.call("all_subnets")

    async def all_metagraphs(self):
        return await self.call("get_all_metagraphs_info")


chain = ChainClient()
