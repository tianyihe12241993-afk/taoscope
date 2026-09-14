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
    def _invoke(self, label: str, fn):
        with self._lock:
            last: Exception | None = None
            for attempt in range(2):
                try:
                    return fn(self._get())
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    log.warning("chain.%s failed (attempt %d): %s", label, attempt + 1, exc)
                    self._reset()
            raise RuntimeError(f"chain.{label} failed: {last}")

    def _call(self, method: str, *args, **kwargs):
        return self._invoke(method, lambda st: getattr(st, method)(*args, **kwargs))

    async def call(self, method: str, *args, **kwargs):
        return await asyncio.to_thread(self._call, method, *args, **kwargs)

    async def block(self) -> int:
        return await asyncio.to_thread(lambda: self._get().block)

    async def all_subnets(self):
        return await self.call("all_subnets")

    async def all_metagraphs(self):
        return await self.call("get_all_metagraphs_info")

    async def miner_burned(self) -> dict[int, float]:
        """netuid -> share of last tempo's miner emission the chain withheld (0..1).

        The chain's own record, SubtensorModule::MinerBurned: incentive routed to
        the owner's hotkeys is recycled or burned instead of paid, and
        distribute_dividends_and_incentives stores withheld / total as a U96F32
        fixed-point, i.e. `bits / 2**32`. One query_map covers every subnet.

        A subnet that emitted nothing to miners stores 0 (the chain's 0/0 falls
        back to zero), so 0 here does not mean "pays miners" -- the caller has
        to read it next to the subnet's emission.
        """
        def fetch(st):
            out: dict[int, float] = {}
            for k, v in st.substrate.query_map("SubtensorModule", "MinerBurned"):
                key = k.value if hasattr(k, "value") else k
                val = v.value if hasattr(v, "value") else v
                bits = val.get("bits") if isinstance(val, dict) else val
                out[int(key)] = int(bits) / 2**32
            return out
        return await asyncio.to_thread(self._invoke, "miner_burned", fetch)


chain = ChainClient()
