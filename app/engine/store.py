"""Where open conversions and app settings are kept.

- REDIS_URL set (e.g. Vercel Marketplace Redis / Upstash): stored in Redis, so every serverless
  instance sees the same conversion and settings survive redeploys.
- Not set (local / single server): kept in this process's memory; settings in the YAML file.

Conversions are pickled + zlib-compressed (a 5,000-ledger job is a few hundred KB) and expire
after JOB_TTL seconds of inactivity.
"""
import json
import os
import pickle
import time
import zlib
from functools import lru_cache

JOB_TTL = 4 * 3600
_PREFIX = "accobiz:"


class MemoryStore:
    kind = "memory"

    def __init__(self):
        self._jobs: dict[str, tuple[float, object]] = {}
        self._settings: dict[str, dict] = {}

    def get_job(self, job_id: str):
        now = time.time()
        for k in [k for k, (t, _) in self._jobs.items() if now - t > JOB_TTL]:
            del self._jobs[k]
        hit = self._jobs.get(job_id)
        if hit:
            self._jobs[job_id] = (now, hit[1])
            return hit[1]
        return None

    def put_job(self, job):
        self._jobs[job.id] = (time.time(), job)

    def get_settings(self, name: str) -> dict | None:
        return self._settings.get(name)

    def put_settings(self, name: str, data: dict) -> bool:
        return False            # caller falls back to the YAML file


class RedisStore:
    kind = "redis"

    def __init__(self, url: str):
        import redis
        self.r = redis.Redis.from_url(url, socket_timeout=10, socket_connect_timeout=10)

    def get_job(self, job_id: str):
        key = f"{_PREFIX}job:{job_id}"
        raw = self.r.get(key)
        if raw is None:
            return None
        self.r.expire(key, JOB_TTL)
        return pickle.loads(zlib.decompress(raw))

    def put_job(self, job):
        data = zlib.compress(pickle.dumps(job, protocol=pickle.HIGHEST_PROTOCOL), 6)
        self.r.set(f"{_PREFIX}job:{job.id}", data, ex=JOB_TTL)

    def get_settings(self, name: str) -> dict | None:
        raw = self.r.get(f"{_PREFIX}settings:{name}")
        return json.loads(raw) if raw else None

    def put_settings(self, name: str, data: dict) -> bool:
        self.r.set(f"{_PREFIX}settings:{name}", json.dumps(data))
        return True


@lru_cache
def store():
    url = os.environ.get("REDIS_URL") or os.environ.get("KV_URL")
    return RedisStore(url) if url else MemoryStore()
