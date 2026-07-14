"""Redis-backed session state for the agent API (P3.1).

With REDIS_URL set, the three pieces of per-session state that used to
live inside the agent-api process move into a shared Redis server, so
any number of agent-api instances can serve the same session pool:

  - conversation checkpoints -> RedisSaver (a LangGraph checkpointer)
  - the session registry     -> RedisSessionStore (hash + EXPIRE + zset)
  - the per-session turn lock -> a Redis distributed lock (SET NX PX)

Without REDIS_URL, none of this module is used -- agent_api.py keeps
its original in-process dict + MemorySaver behavior.

Key layout (everything namespaced under agnes:):

  agnes:session:{id}          hash  created_at / last_used (informational;
                                    the key's EXPIRE is the real idle TTL)
  agnes:sessions:index        zset  member=session id, score=last_used
                                    epoch -- the LRU index for the cap
  agnes:lock:{id}             str   the turn lock (redis-py Lock token)
  agnes:ckpt:{thread}:{ns}    hash  field=checkpoint id -> envelope
  agnes:blob:{thread}:{ns}    hash  field=channel\\x1fversion -> envelope
  agnes:wr:{thread}:{ns}:{id} hash  field=task\\x1fidx -> envelope

Envelopes are pickled tuples of our own making whose payloads are
already serialized by LangGraph's JsonPlusSerializer -- pickle here
only wraps (str, bytes) pairs we wrote ourselves, read back from a
store we own, so it is not exposed to untrusted input.
"""

import asyncio
import logging
import pickle
import random
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlsplit

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

logger = logging.getLogger("ease_clients.redis_state")

# field separator inside hash field names; \x1f is the ASCII "unit
# separator", which cannot appear in channel names or task ids
_SEP = "\x1f"


def _host_only(url: str) -> str:
    """host:port of a redis URL, safe to log (never the password)."""
    parts = urlsplit(url)
    return f"{parts.hostname or '?'}:{parts.port or 6379}"


# -- checkpointer --

class RedisSaver(BaseCheckpointSaver[str]):
    """LangGraph checkpointer on PLAIN Redis (no modules required).

    Mirrors the storage model of langgraph's InMemorySaver exactly --
    checkpoints, channel-value blobs, and pending writes are kept
    separately and reassembled on read -- but keyed into Redis hashes
    instead of process dicts, so a checkpoint written by one agent-api
    instance is readable by every other.

    Async-only: the API server runs everything through the graph's
    async surface (astream_events / aget_state), so the sync
    checkpointer methods are deliberately not implemented.

    Every key carries the session idle TTL, refreshed on each read and
    write, so conversation state expires together with its session
    registry entry instead of leaking.
    """

    def __init__(self, client, ttl_seconds: float) -> None:
        super().__init__()
        self._r = client
        self._ttl = int(ttl_seconds)

    # -- key helpers --

    def _ckpt_key(self, thread_id: str, ns: str) -> str:
        return f"agnes:ckpt:{thread_id}:{ns}"

    def _blob_key(self, thread_id: str, ns: str) -> str:
        return f"agnes:blob:{thread_id}:{ns}"

    def _writes_key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return f"agnes:wr:{thread_id}:{ns}:{checkpoint_id}"

    async def _touch(self, *keys: str) -> None:
        pipe = self._r.pipeline()
        for key in keys:
            pipe.expire(key, self._ttl)
        await pipe.execute()

    # -- reads --

    async def _load_blobs(
        self, thread_id: str, ns: str, versions: ChannelVersions
    ) -> dict[str, Any]:
        if not versions:
            return {}
        fields = [f"{k}{_SEP}{v}" for k, v in versions.items()]
        raw = await self._r.hmget(self._blob_key(thread_id, ns), fields)
        channel_values: dict[str, Any] = {}
        for k, blob in zip(versions.keys(), raw):
            if blob is None:
                continue
            typed = pickle.loads(blob)
            if typed[0] != "empty":
                channel_values[k] = self.serde.loads_typed(typed)
        return channel_values

    async def _load_writes(
        self, thread_id: str, ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        raw = await self._r.hgetall(self._writes_key(thread_id, ns, checkpoint_id))
        entries = [pickle.loads(v) for v in raw.values()]
        # deterministic order: by task id, then write index (hash field
        # order is arbitrary, unlike the insertion-ordered dict the
        # in-memory saver iterates)
        order = {
            field: idx
            for field, idx in (
                (f, int(f.decode().split(_SEP)[1])) for f in raw.keys()
            )
        }
        paired = sorted(
            zip(raw.keys(), entries), key=lambda fv: (fv[1][0], order[fv[0]])
        )
        return [
            (task_id, channel, self.serde.loads_typed(typed))
            for _, (task_id, channel, typed, _path) in paired
        ]

    async def _tuple_for(
        self, thread_id: str, ns: str, checkpoint_id: str, envelope: bytes
    ) -> CheckpointTuple:
        ckpt_typed, meta_typed, parent_id = pickle.loads(envelope)
        checkpoint: Checkpoint = self.serde.loads_typed(ckpt_typed)
        checkpoint = {
            **checkpoint,
            "channel_values": await self._load_blobs(
                thread_id, ns, checkpoint["channel_versions"]
            ),
        }
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=self.serde.loads_typed(meta_typed),
            pending_writes=await self._load_writes(thread_id, ns, checkpoint_id),
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        key = self._ckpt_key(thread_id, ns)

        if checkpoint_id := get_checkpoint_id(config):
            envelope = await self._r.hget(key, checkpoint_id)
            if envelope is None:
                return None
        else:
            fields = await self._r.hkeys(key)
            if not fields:
                return None
            # checkpoint ids are lexicographically ordered by langgraph,
            # so the latest is simply the max -- same rule as the
            # in-memory saver
            checkpoint_id = max(f.decode() for f in fields)
            envelope = await self._r.hget(key, checkpoint_id)
            if envelope is None:  # expired between hkeys and hget
                return None
        await self._touch(key, self._blob_key(thread_id, ns))
        return await self._tuple_for(thread_id, ns, checkpoint_id, envelope)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            raise NotImplementedError(
                "RedisSaver.alist requires a config naming the thread_id"
            )
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        before_id = get_checkpoint_id(before) if before else None
        entries = await self._r.hgetall(self._ckpt_key(thread_id, ns))
        remaining = limit
        for checkpoint_id in sorted((f.decode() for f in entries), reverse=True):
            if before_id and checkpoint_id >= before_id:
                continue
            tup = await self._tuple_for(
                thread_id, ns, checkpoint_id, entries[checkpoint_id.encode()]
            )
            if filter and not all(
                tup.metadata.get(k) == v for k, v in filter.items()
            ):
                continue
            if remaining is not None:
                if remaining <= 0:
                    break
                remaining -= 1
            yield tup

    # -- writes --

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        c = checkpoint.copy()
        values: dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]

        blob_fields = {}
        for k, v in new_versions.items():
            typed = (
                self.serde.dumps_typed(values[k]) if k in values else ("empty", b"")
            )
            blob_fields[f"{k}{_SEP}{v}"] = pickle.dumps(typed)

        envelope = pickle.dumps(
            (
                self.serde.dumps_typed(c),
                self.serde.dumps_typed(get_checkpoint_metadata(config, metadata)),
                config["configurable"].get("checkpoint_id"),  # parent
            )
        )

        ckpt_key = self._ckpt_key(thread_id, ns)
        blob_key = self._blob_key(thread_id, ns)
        pipe = self._r.pipeline()
        if blob_fields:
            pipe.hset(blob_key, mapping=blob_fields)
        pipe.hset(ckpt_key, checkpoint["id"], envelope)
        pipe.expire(ckpt_key, self._ttl)
        pipe.expire(blob_key, self._ttl)
        await pipe.execute()

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        key = self._writes_key(thread_id, ns, checkpoint_id)

        fields = {}
        existing = None
        for idx, (channel, value) in enumerate(writes):
            widx = WRITES_IDX_MAP.get(channel, idx)
            field = f"{task_id}{_SEP}{widx}"
            if widx >= 0:
                # non-special writes are first-write-wins, matching the
                # in-memory saver's dedup rule
                if existing is None:
                    existing = set(await self._r.hkeys(key))
                if field.encode() in existing:
                    continue
            fields[field] = pickle.dumps(
                (task_id, channel, self.serde.dumps_typed(value), task_path)
            )
        if fields:
            pipe = self._r.pipeline()
            pipe.hset(key, mapping=fields)
            pipe.expire(key, self._ttl)
            await pipe.execute()

    async def adelete_thread(self, thread_id: str) -> None:
        patterns = (
            f"agnes:ckpt:{thread_id}:*",
            f"agnes:blob:{thread_id}:*",
            f"agnes:wr:{thread_id}:*",
        )
        for pattern in patterns:
            async for key in self._r.scan_iter(match=pattern, count=100):
                await self._r.delete(key)

    # -- sync surface: not supported (the API server is async-only) --

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError("RedisSaver is async-only; use aget_tuple")

    def list(self, config, *, filter=None, before=None, limit=None):
        raise NotImplementedError("RedisSaver is async-only; use alist")

    def put(self, config, checkpoint, metadata, new_versions):
        raise NotImplementedError("RedisSaver is async-only; use aput")

    def put_writes(self, config, writes, task_id, task_path=""):
        raise NotImplementedError("RedisSaver is async-only; use aput_writes")

    def delete_thread(self, thread_id: str) -> None:
        raise NotImplementedError("RedisSaver is async-only; use adelete_thread")

    def get_next_version(self, current: str | None, channel: None) -> str:
        # same format as the in-memory saver: monotonically increasing
        # integer part, random tail to disambiguate concurrent writers
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        return f"{current_v + 1:032}.{random.random():016}"


# -- session registry + locks --

class RedisSessionStore:
    """Session registry and per-session turn locks in Redis.

    Replaces the in-process _Session dict when REDIS_URL is set. The
    idle TTL is enforced server-side by EXPIRE (no sweeper task), the
    LRU cap uses a zset ordered by last-used time, and the lock-aware
    eviction rule is preserved: a session whose turn lock is held is
    never evicted, even if it is the LRU candidate.
    """

    _INDEX = "agnes:sessions:index"

    def __init__(self, client, checkpointer: RedisSaver,
                 ttl_seconds: float, max_sessions: int,
                 lock_timeout_seconds: float) -> None:
        self._r = client
        self._checkpointer = checkpointer
        self._ttl = int(ttl_seconds)
        self._max = max_sessions
        self._lock_timeout = lock_timeout_seconds

    @staticmethod
    def _key(session_id: str) -> str:
        return f"agnes:session:{session_id}"

    @staticmethod
    def _lock_key(session_id: str) -> str:
        return f"agnes:lock:{session_id}"

    async def ping(self) -> None:
        await self._r.ping()

    async def create_session(self) -> str:
        await self._evict_for_cap()
        session_id = str(uuid.uuid4())
        now = time.time()
        pipe = self._r.pipeline()
        pipe.hset(self._key(session_id),
                  mapping={"created_at": now, "last_used": now})
        pipe.expire(self._key(session_id), self._ttl)
        pipe.zadd(self._INDEX, {session_id: now})
        await pipe.execute()
        logger.info("session %s created (redis)", session_id)
        return session_id

    async def has_session(self, session_id: str) -> bool:
        return bool(await self._r.exists(self._key(session_id)))

    async def require(self, session_id: str) -> None:
        """Refresh the session's TTL clock, or raise KeyError.

        KeyError (not UnknownSessionError) so this module never imports
        agent_api -- the caller translates.
        """
        if not await self.has_session(session_id):
            # lazy cleanup of a stale LRU index entry whose hash expired
            await self._r.zrem(self._INDEX, session_id)
            raise KeyError(session_id)
        await self.touch(session_id)

    async def touch(self, session_id: str) -> None:
        now = time.time()
        pipe = self._r.pipeline()
        pipe.hset(self._key(session_id), "last_used", now)
        pipe.expire(self._key(session_id), self._ttl)
        pipe.zadd(self._INDEX, {session_id: now})
        await pipe.execute()

    def lock(self, session_id: str):
        """The distributed turn lock as an async context manager.

        SET NX PX under the hood (redis-py Lock): one turn at a time
        per session ACROSS instances; a crashed holder's lock frees
        itself after the timeout. Release after expiry is logged, not
        raised -- the turn already completed, losing the lock early is
        a warning sign, not a failure.
        """
        return _TurnLock(
            self._r.lock(
                self._lock_key(session_id),
                timeout=self._lock_timeout,
                blocking=True,
                sleep=0.05,
            ),
            session_id,
        )

    async def evict(self, session_id: str, reason: str) -> None:
        pipe = self._r.pipeline()
        pipe.delete(self._key(session_id))
        pipe.zrem(self._INDEX, session_id)
        await pipe.execute()
        await self._checkpointer.adelete_thread(session_id)
        logger.info("session %s evicted (%s)", session_id, reason)

    async def _evict_for_cap(self) -> None:
        """LRU-evict to stay under the cap, skipping locked sessions.

        Mirrors the in-process rule: sessions with a running turn are
        never evicted; if every candidate is mid-turn the cap
        overshoots rather than killing a live conversation.
        """
        while await self._r.zcard(self._INDEX) >= self._max:
            candidates = await self._r.zrange(self._INDEX, 0, 4)
            evicted = False
            for raw in candidates:
                sid = raw.decode()
                if await self._r.exists(self._lock_key(sid)):
                    continue  # turn in flight -- never evict
                if not await self._r.exists(self._key(sid)):
                    await self._r.zrem(self._INDEX, sid)  # stale entry
                    evicted = True
                    break
                await self.evict(sid, reason="max sessions cap")
                evicted = True
                break
            if not evicted:
                logger.warning(
                    "session cap reached but all LRU candidates are "
                    "mid-turn; overshooting the cap"
                )
                return


class _TurnLock:
    """Async context manager pairing acquire/release with safe logging."""

    def __init__(self, lock, session_id: str) -> None:
        self._lock = lock
        self._session_id = session_id

    async def __aenter__(self):
        await self._lock.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            await self._lock.release()
        except Exception:
            # LockNotOwnedError: the lock expired mid-turn (turn longer
            # than AGENT_API_LOCK_TIMEOUT_SECONDS). The turn finished;
            # log loudly so the timeout can be raised.
            logger.warning(
                "turn lock for session %s expired before release -- "
                "consider raising AGENT_API_LOCK_TIMEOUT_SECONDS",
                self._session_id,
            )


# -- wiring --

async def build_redis_state(
    url: str,
    ttl_seconds: float,
    max_sessions: int,
    lock_timeout_seconds: float,
    client=None,
) -> tuple[RedisSessionStore, RedisSaver]:
    """Connect to Redis and return (session_store, checkpointer).

    Fails fast (raises RuntimeError) when Redis is unreachable: with
    REDIS_URL set, silently falling back to in-process state would
    LOOK healthy on one instance and corrupt sessions the moment a
    second instance starts. The URL may contain a password, so errors
    and logs name only the host.

    client is injectable for tests (fakeredis).
    """
    if client is None:
        import redis.asyncio as aioredis

        client = aioredis.from_url(url)
    try:
        await client.ping()
    except Exception as exc:
        raise RuntimeError(
            f"REDIS_URL is set but Redis at {_host_only(url)} is not "
            f"reachable: {exc}. Start Redis or unset REDIS_URL to run "
            "with in-process session state (single instance only)."
        ) from exc

    logger.info("session state: redis at %s", _host_only(url))
    checkpointer = RedisSaver(client, ttl_seconds)
    store = RedisSessionStore(
        client, checkpointer, ttl_seconds, max_sessions, lock_timeout_seconds
    )
    return store, checkpointer
