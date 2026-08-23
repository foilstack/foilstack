"""Client for the image encoder service."""

from __future__ import annotations

import base64

import httpx
import numpy as np


class EmbedderError(RuntimeError):
    pass


async def embed_image(url: str, image_bytes: bytes, timeout: float = 60.0) -> np.ndarray:
    payload = {"data": base64.b64encode(image_bytes).decode("ascii")}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(f"{url.rstrip('/')}/embed", json=payload)
        except httpx.HTTPError as exc:
            raise EmbedderError(f"encoder unreachable at {url}: {exc}") from exc
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
