"""/api/v1/muse — Muse (muse.ai, agent AI của Meta) làm nhà cung cấp AI như 9Router.

Hai nhóm route:
  * quản lý: GET status · GET/PUT settings (hồ sơ trình duyệt giữ phiên Muse, số lượt mỗi chat phụ) · POST test
  * cổng chuẩn OpenAI ở /api/v1/muse/v1: GET models · POST chat/completions (cả stream) · POST images/generations
    — cho chỗ nào chỉ biết gọi kiểu OpenAI qua HTTP: Content Studio đọc PROVIDERS["muse"]["base_url"], storyboard
    của «Tạo video từ nội dung» stream qua brain.openai_compat_params. Brain và bộ vẽ ảnh của lõi gọi thẳng
    tubecli.core.muse, không qua HTTP.

Mọi lời gọi Muse chạy trong thread (asyncio.to_thread): mỗi lượt lái trình duyệt mất 5–60 s.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.responses import JSONResponse, StreamingResponse

from tubecli.core import muse

router = APIRouter(prefix="/api/v1/muse", tags=["muse"])

# kind của MuseError → mã HTTP (client kiểu OpenAI đọc mã để biết nên thử lại hay báo cấu hình).
_STATUS = {"config": 400, "refused": 400, "auth": 401, "approval": 409, "busy": 429,
           "browser": 503, "timeout": 504, "error": 502}


def _error(e: "muse.MuseError") -> JSONResponse:
    return JSONResponse(status_code=_STATUS.get(e.kind, 502),
                        content={"error": {"message": f"Muse: {e}", "type": "muse_" + e.kind, "code": e.kind}})


def _profile_names() -> List[str]:
    """Tên hồ sơ trình duyệt để chọn (bỏ bản '<tên>_bas' — anh em của hồ sơ chính, không chọn riêng)."""
    try:
        root = muse._profiles_dir()
        return sorted(n for n in os.listdir(root)
                      if os.path.isdir(os.path.join(root, n)) and not n.endswith("_bas"))
    except Exception:      # noqa: BLE001
        return []


class MuseSettingsRequest(BaseModel):
    profile: Optional[str] = None          # None = giữ nguyên; "" = bỏ chọn
    turns_per_chat: Optional[int] = None


@router.get("/status")
async def api_status():
    return await asyncio.to_thread(muse.status)


@router.get("/settings")
async def api_get_settings():
    return {**muse.settings(), "profiles": _profile_names(),
            "default_turns_per_chat": muse.DEFAULT_TURNS_PER_CHAT, "max_turns_per_chat": muse.MAX_TURNS_PER_CHAT}


@router.put("/settings")
async def api_put_settings(req: MuseSettingsRequest):
    try:
        return muse.set_settings(profile=req.profile, turns_per_chat=req.turns_per_chat)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/test")
async def api_test():
    return await asyncio.to_thread(muse.test_chat)


# ── cổng chuẩn OpenAI ─────────────────────────────────────────────────────────

@router.get("/v1/models")
async def api_models():
    rows = [{"id": m, "object": "model", "created": 0, "owned_by": "muse", "type": "chat"} for m in muse.CHAT_MODELS]
    rows += [{"id": m, "object": "model", "created": 0, "owned_by": "muse", "type": "image"} for m in muse.IMAGE_MODELS]
    return {"object": "list", "data": rows}


class ChatRequest(BaseModel):
    model: str = muse.CHAT_MODEL
    messages: List[Any] = []
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    class Config:
        extra = "allow"


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@router.post("/v1/chat/completions")
async def api_chat_completions(req: ChatRequest):
    if not req.messages:
        return JSONResponse(status_code=400, content={"error": {"message": "messages is required",
                                                                 "type": "invalid_request_error"}})
    # Chờ trả lời XONG rồi mới mở luồng: hỏng thì còn trả đúng mã lỗi (401/429/504…) thay vì một luồng 200
    # chứa câu lỗi mà phía gọi đem đi parse như kịch bản. Client đọc tối đa 600 s, Muse ít khi quá 300 s.
    try:
        text = await asyncio.to_thread(muse.chat_completion, list(req.messages), req.model or muse.CHAT_MODEL)
    except muse.MuseError as e:
        return _error(e)
    cid = "chatcmpl-muse-" + uuid.uuid4().hex[:12]
    created = int(time.time())
    model = req.model or muse.CHAT_MODEL
    usage = {"prompt_tokens": 0, "completion_tokens": max(1, len(text) // 4), "total_tokens": max(1, len(text) // 4)}
    if not req.stream:
        return {"id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": usage}

    def chunk(delta: dict, finish=None) -> str:
        return _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    async def gen():
        yield chunk({"role": "assistant", "content": ""})
        for i in range(0, len(text), 400):
            yield chunk({"content": text[i:i + 400]})
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ImagesRequest(BaseModel):
    prompt: str
    model: str = muse.IMAGE_MODEL
    n: int = 1
    size: Optional[str] = None             # "1792x1024" kiểu OpenAI/9Router — chỉ lấy hướng khung
    aspect_ratio: Optional[str] = None     # "16:9" … thắng size khi có
    response_format: str = "b64_json"


def aspect_from(size: Optional[str], aspect: Optional[str]) -> str:
    if aspect and aspect in muse.ASPECTS:
        return aspect
    try:
        w, h = (int(x) for x in str(size or "").lower().split("x", 1))
    except ValueError:
        return "16:9"
    if w == h:
        return "1:1"
    r = w / h
    if r >= 1.5:
        return "16:9"
    if r > 1:
        return "4:3"
    return "9:16" if r <= 0.67 else "3:4"


@router.post("/v1/images/generations")
async def api_images(req: ImagesRequest):
    if not (req.prompt or "").strip():
        return JSONResponse(status_code=400, content={"error": {"message": "prompt is required",
                                                                 "type": "invalid_request_error"}})
    ar = aspect_from(req.size, req.aspect_ratio)
    out = []
    try:
        for _ in range(max(1, min(4, int(req.n or 1)))):
            data = await asyncio.to_thread(muse.generate_image_bytes, req.prompt, ar)
            out.append({"b64_json": base64.b64encode(data).decode("ascii"), "revised_prompt": req.prompt})
    except muse.MuseError as e:
        if not out:
            return _error(e)
    return {"created": int(time.time()), "data": out}
