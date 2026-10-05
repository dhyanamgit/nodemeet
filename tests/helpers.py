import asyncio
from datetime import datetime, timezone


def run(coro):
    return asyncio.run(coro)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)
