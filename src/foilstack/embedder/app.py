"""A single-purpose service that turns a card image into a vector.

Its own container for one reason: torch plus CUDA is ~2.5 GB, and keeping the
encoder here means the web image stays small enough that someone can actually
pull it.

It holds no state, talks to no database, and has no route off the box. The only
thing it can do is return 1024 floats.

The model must be the same one that encoded your catalogue — vectors from two
encoders are not comparable and the failure is silent, plausible nonsense.
`/healthz` reports the loaded name so a mismatch is a question anyone can ask
rather than something you deduce from bad matches.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("EMBED_MODEL", "facebook/dinov3-vitl16-pretrain-lvd1689m")
EMBEDDING_DIM = 1024
# A phone photo can be 8 MB; the encoder resamples to 224x224 regardless, so
# anything past this is decode cost for no accuracy.
MAX_BYTES = 12 * 1024 * 1024

app = FastAPI(title="foilstack embedder", docs_url=None, redoc_url=None)
_encoder = None
_lock = asyncio.Lock()


class EmbedRequest(BaseModel):
    data: str = Field(description="base64-encoded image bytes")


def _load():
    """Build the encoder. Blocking and slow (~15 s); call it off the event loop."""
    import torch
    from transformers import AutoImageProcessor, AutoModel

    device = "cuda" if torch.cuda.is_available() else "cpu"
    processor = AutoImageProcessor.from_pretrained(MODEL_NAME)
    # bfloat16, never float16 — DINOv3 ViT-L overflows in fp16 and returns an
    # all-NaN pooled output for every image, with no error raised anywhere.
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    model = AutoModel.from_pretrained(MODEL_NAME, dtype=dtype).to(device).eval()
    logger.info("encoder loaded: %s on %s (%s)", MODEL_NAME, device, dtype)
    return torch, processor, model, device


def _encode(image: Image.Image) -> list[float]:
    assert _encoder is not None, "call _load() before encoding"
    torch, processor, model, device = _encoder
    inputs = processor(images=[image], return_tensors="pt").to(device)
    with torch.inference_mode():
        outputs = model(**inputs)
    pooled = getattr(outputs, "pooler_output", None)
    if pooled is None:
        pooled = outputs.last_hidden_state[:, 0]
    vec = torch.nn.functional.normalize(pooled, dim=-1).float()
    if not torch.isfinite(vec).all():
        raise ValueError("encoder produced non-finite values")
    return vec[0].cpu().tolist()


async def _ensure_loaded():
    global _encoder
    if _encoder is None:
        async with _lock:  # one loader, not one per concurrent first request
            if _encoder is None:
                _encoder = await asyncio.to_thread(_load)
    return _encoder


@app.get("/healthz")
async def healthz() -> dict:
    """Ready means *loaded*. Reporting ok before the weights are in means the
    first real request eats a 15-second cold start."""
    return {"status": "ok", "model": MODEL_NAME, "loaded": _encoder is not None}


@app.post("/embed")
async def embed(req: EmbedRequest) -> JSONResponse:
    try:
        raw = base64.b64decode(req.data, validate=True)
    except (binascii.Error, ValueError):
        return JSONResponse({"error": "data is not valid base64"}, status_code=400)
    if not raw or len(raw) > MAX_BYTES:
        return JSONResponse({"error": "empty or oversized image"}, status_code=400)

    try:
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:  # noqa: BLE001 — any decode failure is the same 400
        return JSONResponse({"error": "could not decode image"}, status_code=400)

    await _ensure_loaded()
    try:
        vector = await asyncio.to_thread(_encode, image)
    except ValueError as exc:
        logger.error("encode failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"vector": vector, "dim": len(vector), "model": MODEL_NAME})


@app.on_event("startup")
async def _warm() -> None:
    try:
        await _ensure_loaded()
    except Exception:
        logger.exception("encoder failed to warm; will retry on first request")
