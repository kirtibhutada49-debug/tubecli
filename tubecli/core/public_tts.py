"""Skill công khai «văn bản → giọng đọc CapCut» (user 27/9/2026: «nhập text, chọn
giọng, trả về mp3; file lưu trên server của agent và có thời hạn»).

Neo an toàn:
  · ĐẦU VÀO: CHỮ THUẦN ≤600 ký tự — lõi không dùng bất kỳ URL nào trong đó làm địa chỉ
    gọi đi; `voice` là MỘT MÃ (cloud giữ danh sách cứng, đây kiểm thêm dạng ký tự).
  · ĐẦU RA: đường dẫn /s/<token> TƯƠNG ĐỐI như youtube.download — cloud tự ghép tunnel.
  · Tài khoản CapCut là CỦA CHỦ (bể tài khoản của extension); trần lượt/ngày chủ đặt ở
    tab Công khai là van tổng. Email tài khoản KHÔNG bao giờ nằm trong kết quả trả ra.

capcut_tts là extension EXTERNAL (data/extensions_external) — không import module được,
nên gọi qua HTTP loopback của chính máy, đúng cách dây chuyền content_video vẫn làm."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from functools import partial
from typing import Any, Dict, Optional

from tubecli.core.public_agents import PublicSkillError

TTS_MAX_CHARS = 600
TTS_TTL_SEC = 6 * 3600
TTS_TIMEOUT_SEC = 32           # trong trần 40 s của invoke; CapCut đọc 600 chữ chỉ vài giây
_VOICE_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")


def _dir() -> str:
    from tubecli.config import DATA_DIR

    d = os.path.join(str(DATA_DIR), "public_tts")
    os.makedirs(d, exist_ok=True)
    return d


def _base() -> str:
    from tubecli.config import get_api_port

    return f"http://127.0.0.1:{get_api_port()}"


def _pick_account() -> str:
    """Mượn một tài khoản đang rảnh trong bể của chủ; không có cái nào → «tts_unavailable»."""
    import requests

    try:
        r = requests.get(_base() + "/api/v1/capcut-tts/accounts", timeout=10)
    except requests.RequestException:
        raise PublicSkillError("tts_unavailable", status=503)
    if r.status_code >= 400:
        raise PublicSkillError("tts_unavailable", status=503)
    now = time.time()
    for a in (r.json() or {}).get("accounts") or []:
        if a.get("enabled") and float(a.get("rest_until") or 0) <= now:
            return str(a.get("email") or "")
    raise PublicSkillError("tts_unavailable", status=503)


CATALOG_TTL_SEC = 3600
_LANG_RE = re.compile(r"^[a-z]{2,3}$")
_catalog: Dict[str, Any] = {"at": 0.0, "data": None}


def catalog_blocking() -> Dict[str, Any]:
    """{languages: [{code, count}], voices: [{id, name, lang}]} — đúng danh mục extension CapCut
    TTS dựng theo vùng của tài khoản (/languages + /speakers; giọng đọc sai tiếng đã bị nó lọc).
    Nhớ 1 giờ: danh mục gần như không đổi, còn mỗi lượt hỏi Node sidecar mất vài giây."""
    import requests

    if _catalog["data"] and time.time() - _catalog["at"] < CATALOG_TTL_SEC:
        return _catalog["data"]
    email = _pick_account()
    try:
        rl = requests.get(_base() + "/api/v1/capcut-tts/languages", params={"email": email}, timeout=60)
        rs = requests.get(_base() + "/api/v1/capcut-tts/speakers", params={"email": email}, timeout=60)
    except requests.RequestException:
        raise PublicSkillError("tts_unavailable", status=503)
    if rl.status_code >= 400 or rs.status_code >= 400:
        raise PublicSkillError("tts_unavailable", status=503)
    voices = []
    for s in rs.json() or []:
        vid, lang = str(s.get("id") or ""), str(s.get("language") or "").lower()
        if _VOICE_RE.match(vid) and _LANG_RE.match(lang):
            voices.append({"id": vid, "name": str(s.get("name") or vid).strip()[:60], "lang": lang})
    have = {v["lang"] for v in voices}
    languages = [{"code": l["language"], "count": int(l.get("speakerCount") or 0)}
                 for l in (rl.json() or {}).get("languages") or []
                 if _LANG_RE.match(str(l.get("language") or "")) and l["language"] in have]
    data = {"languages": languages, "voices": voices}
    _catalog.update(at=time.time(), data=data)
    return data


def _synth_blocking(text: str, voice: str, dest: str) -> str:
    import requests

    base = _base()
    email = _pick_account()

    body: Dict[str, Any] = {"email": email, "text": text}
    if voice:
        body["speaker"] = voice
    try:
        r = requests.post(base + "/api/v1/capcut-tts/synthesize", json=body, timeout=TTS_TIMEOUT_SEC - 3)
    except requests.RequestException:
        raise PublicSkillError("tts_failed", status=502)
    if r.status_code >= 400:
        raise PublicSkillError("tts_unavailable" if r.status_code == 503 else "tts_failed",
                               status=503 if r.status_code == 503 else 502)
    audio = r.content
    if "json" in (r.headers.get("content-type") or "").lower():
        import base64

        try:
            obj = r.json() or {}
            audio = base64.b64decode(obj.get("audio_b64") or obj.get("audio") or "")
        except Exception:      # noqa: BLE001
            audio = b""
    if not audio or len(audio) < 1000:
        raise PublicSkillError("tts_failed", status=502)
    tmp = dest + ".tmp"
    with open(tmp, "wb") as f:
        f.write(audio)
    os.replace(tmp, dest)
    return dest


async def resolve(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = str(text or "").strip()
    if not body:
        raise PublicSkillError("need_text")
    if len(body) > TTS_MAX_CHARS:
        raise PublicSkillError("text_too_long")
    voice = str((opts or {}).get("voice") or "").strip()
    if voice and not _VOICE_RE.match(voice):
        raise PublicSkillError("bad_input")
    # Cloud chỉ kiểm DẠNG mã giọng (danh mục giờ lấy từ chính máy này) → máy kiểm giọng có
    # thật trong danh mục; lạ → giọng mặc định của CapCut thay vì lỗi.
    if voice and _catalog["data"] and voice not in {v["id"] for v in _catalog["data"]["voices"]}:
        voice = ""

    from tubecli.core import public_share as ps

    if not ps.fm_enabled():
        raise PublicSkillError("skill_unavailable", status=503)
    d = _dir()
    ps.sweep(d, TTS_TTL_SEC)

    # Cùng giọng + cùng chữ = cùng file: ai đó gửi lại y nguyên thì trả ngay, không tốn
    # thêm lượt CapCut của chủ.
    key = hashlib.sha256((voice + "|" + body).encode("utf-8")).hexdigest()[:16]
    fp = os.path.join(d, key + ".mp3")
    if not (os.path.isfile(fp) and os.path.getsize(fp) >= 1000):
        try:
            await asyncio.wait_for(
                asyncio.to_thread(partial(_synth_blocking, body, voice, fp)), timeout=TTS_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            raise PublicSkillError("timeout", status=504)

    share = ps.share_path(fp, "tts_" + key + ".mp3", TTS_TTL_SEC)
    return {
        "kind": "ttsfile",
        "voice": voice,
        "text": body[:160],
        "size": int(os.path.getsize(fp)),
        "share": share + "/download",
        "page": share,
        "expires_h": int(TTS_TTL_SEC // 3600),
    }
