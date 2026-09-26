"""Reading an HTTP answer to its end, up to a cap (``StreamReader.read(n)`` returns what has arrived so far)."""

from __future__ import annotations

import aiohttp


class TooLarge(Exception):
    pass


async def read_capped(resp: aiohttp.ClientResponse, cap: int) -> bytes:
    if resp.content_length is not None and resp.content_length > cap:
        raise TooLarge
    chunks, total = [], 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > cap:
            raise TooLarge
        chunks.append(chunk)
    return b"".join(chunks)
