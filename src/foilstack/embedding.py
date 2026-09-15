"""Client for the image encoder service."""

from __future__ import annotations

import base64

import httpx
import numpy as np


class EmbedderError(RuntimeError):
    pass


async def embed_image(url: str, image_bytes: bytes, timeout: float = 60.0) -> np.ndarray:
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(f"{url.rstrip('/')}/embed", json=_payload(image_bytes))
        except httpx.HTTPError as exc:
            raise EmbedderError(f"encoder unreachable at {url}: {exc}") from exc
    return _vector(response)


def embed_image_sync(url: str, image_bytes: bytes, timeout: float = 60.0) -> np.ndarray:
    """The same call, for a caller that is already off the event loop.

    An import runs in a worker thread, and awaiting there would mean starting
    an event loop per scan just to wait on one request.
    """
    with httpx.Client(timeout=timeout) as client:
        try:
            response = client.post(f"{url.rstrip('/')}/embed", json=_payload(image_bytes))
        except httpx.HTTPError as exc:
            raise EmbedderError(f"encoder unreachable at {url}: {exc}") from exc
    return _vector(response)


def _payload(image_bytes: bytes) -> dict:
    return {"data": base64.b64encode(image_bytes).decode("ascii")}


def _vector(response: httpx.Response) -> np.ndarray:
    if response.status_code != 200:
        raise EmbedderError(f"encoder returned {response.status_code}")
    vector = response.json().get("vector")
    if not vector:
        raise EmbedderError("encoder returned no embedding")
    return np.asarray(vector, dtype=np.float32)


async def encoder_health(url: str, timeout: float = 5.0) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{url.rstrip('/')}/healthz")
        return response.json() if response.status_code == 200 else None
    except httpx.HTTPError:
        return None
