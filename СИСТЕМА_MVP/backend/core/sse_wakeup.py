import asyncio
import logging

from django.conf import settings


logger = logging.getLogger(__name__)

# Fail-closed defaults mirroring the pilot settings block. ``config/settings.py``
# is not part of this transfer: an empty URL means "Redis is not used", so the
# after-commit wakeup is a no-op and the stream falls back to DB safety scans.
SSE_REDIS_DEFAULTS = {
    'SSE_REDIS_URL': '',
    'SSE_REDIS_NAMESPACE': 'accounting-mvp:sse:local',
}


def sse_redis_setting(name):
    return getattr(settings, name, SSE_REDIS_DEFAULTS[name])


def operational_channel():
    namespace = str(sse_redis_setting('SSE_REDIS_NAMESPACE') or '').strip().strip(':')
    return f'{namespace}:operational-state'


def publish_operational_wakeup(version):
    """Best-effort after-commit wakeup; PostgreSQL remains authoritative."""
    redis_url = str(sse_redis_setting('SSE_REDIS_URL') or '').strip()
    if not redis_url:
        return False
    try:
        import redis

        client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        try:
            client.publish(operational_channel(), str(int(version)))
        finally:
            client.close()
        return True
    except Exception:
        logger.warning('SSE Redis wakeup failed; DB safety scan will recover.', exc_info=True)
        return False


class RedisWakeSubscription:
    """Confirmed Redis subscription used only to shorten DB scan latency."""

    def __init__(self):
        self._client = None
        self._pubsub = None
        self.available = False

    async def connect(self):
        redis_url = str(sse_redis_setting('SSE_REDIS_URL') or '').strip()
        if not redis_url:
            return False
        try:
            import redis.asyncio as redis_async

            self._client = redis_async.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                health_check_interval=15,
            )
            self._pubsub = self._client.pubsub(ignore_subscribe_messages=False)
            await self._pubsub.subscribe(operational_channel())
            # subscribe() returning is not enough for the replay race. Do not read
            # the journal until Redis acknowledged this exact channel.
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=False,
                    timeout=1,
                )
                if (
                    message
                    and message.get('type') == 'subscribe'
                    and message.get('channel') == operational_channel()
                ):
                    self.available = True
                    return True
            raise TimeoutError('Redis did not acknowledge SSE subscription')
        except Exception:
            logger.warning(
                'SSE Redis subscribe failed; continuing with DB safety scans.',
                exc_info=True,
            )
            await self.close()
            return False

    async def wait(self, timeout):
        if not self.available or self._pubsub is None:
            await asyncio.sleep(timeout)
            await self.connect()
            return None
        try:
            return await self._pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=timeout,
            )
        except Exception:
            logger.warning(
                'SSE Redis subscription lost; DB safety scans remain active.',
                exc_info=True,
            )
            await self.close()
            return None

    async def close(self):
        self.available = False
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._pubsub = None
        self._client = None
