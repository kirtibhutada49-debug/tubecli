"""Skill công khai «Trình duyệt từ xa» (browser.remote) — user 28/9/2026: «agent skill remote
browser, cho phép share browser mình sở hữu public, nhưng hạn chế chọn file khi upload».

Người lạ trên Town dùng MỘT hồ sơ trình duyệt chủ đã chọn (cài đặt công khai của agent):
xem trực tiếp, bấm, gõ, lướt web — trong N phút. Dựng trên hạ tầng khách-có-phạm-vi của
workspace (guest token + cookie tubecli_guest + gate DENY mặc định), nhưng hẹp hơn hẳn:

  · preview mở bằng --isolate: mạng qua extensions/browser/net_guard.cjs (không với tới
    127.0.0.1 = dashboard của chủ, LAN, metadata cloud), lệnh điều hướng chỉ http/https,
    không tải file về máy chủ, phiên sạch (không thấy tab của người trước);
  · token scope {public: True, profiles: [hồ sơ], upload: media|off} — gate chỉ cho WS +
    screenshot + tải lên có trần (≤5 file, ≤25 MB, ảnh/video/PDF từ máy NGƯỜI XEM); không
    tự mở/tắt browser, không chọn file trên máy chủ, không Drive, không danh sách hồ sơ;
  · mỗi agent MỘT phiên, gắn với ĐÚNG người gọi (mã caller cloud gửi); hết giờ → thu token,
    WS đang mở bị cắt (routes.guest_expiry), tắt browser, dọn file tải lên.

Input (JSON do cloud dựng): {"action": "start"} | {"action": "stop", "session": "<hex>"}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from typing import Any, Dict, Optional

from tubecli.core.public_agents import PublicSkillError

logger = logging.getLogger("public_browser")

_SESSION_RE = re.compile(r"^[a-f0-9]{16}$")
OWNER_SESSION_SEC = 2 * 3600       # chủ tự dùng: đủ đẩy một video lớn qua tunnel rồi lên YouTube


def public_agents_owner() -> str:
    from tubecli.core import public_agents
    try:
        return str(public_agents.owner_caller() or "")
    except Exception:      # noqa: BLE001 — không biết chủ thì coi như người lạ
        return ""
_sessions: Dict[str, Dict[str, Any]] = {}      # agent_id → phiên đang chạy
_lock = asyncio.Lock()


def _alive(s: Dict[str, Any]) -> bool:
    try:
        from tubecli.extensions.browser.routes import port_is_isolated
        return s["exp"] > time.time() and port_is_isolated(s["port"])
    except Exception:      # noqa: BLE001
        return False


async def _end(agent_id: str, sid: str) -> None:
    """Thu token (WS đang mở tự bị cắt trong ≤5 s), tắt browser, dọn file tải lên."""
    s = _sessions.get(agent_id)
    if not s or s["sid"] != sid:
        return
    _sessions.pop(agent_id, None)
    # KHÔNG huỷ task hẹn giờ khi chính nó đang gọi _end (hết giờ): tự cancel mình thì lệnh
    # huỷ ập vào ngay ở await tắt preview bên dưới → token đã thu nhưng trình duyệt chạy mãi,
    # giữ hồ sơ, người sau chỉ gặp «bận» (lộ ra 28/9 — phiên hết giờ để lại preview mồ côi).
    t = s.get("task")
    if t is not None and t is not asyncio.current_task():
        t.cancel()
    from tubecli.core import auth

    auth.revoke_guest_tokens_for_workspace(s["workspace"])
    try:
        from tubecli.extensions.browser.routes import stop_public_preview
        # shield: lượt gọi bị huỷ giữa chừng (invoke hết hạn…) thì việc tắt vẫn chạy trọn.
        await asyncio.shield(asyncio.to_thread(stop_public_preview, s["profile"], s["port"]))
    except asyncio.CancelledError:
        raise
    except Exception as e:      # noqa: BLE001
        logger.warning("[browser.remote] tắt preview hỏng: %s", e)
    logger.info("[browser.remote] phiên %s của agent %s kết thúc", sid, agent_id)


async def _expire_later(agent_id: str, sid: str, secs: float) -> None:
    try:
        await asyncio.sleep(max(1.0, secs))
    except asyncio.CancelledError:
        return
    await _end(agent_id, sid)


def _live(s: Dict[str, Any], token: str) -> Dict[str, Any]:
    return {"kind": "browserlive", "session": s["sid"], "token": token, "port": s["port"],
            "expires_in": max(0, int(s["exp"] - time.time())), "upload": s["upload"]}


async def resolve(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    o = opts or {}
    try:
        req = json.loads(str(text or "")) or {}
    except ValueError:
        raise PublicSkillError("bad_input")
    action = str(req.get("action") or "")
    agent_id = str(o.get("_agent_id") or "")
    caller = str(o.get("_caller") or "")
    st = o.get("_settings") or {}
    if not agent_id or not caller:
        # Phiên phải gắn với MỘT người xem — không có mã người gọi thì không mở.
        raise PublicSkillError("login_required", status=401)
    profile = str(st.get("browser_profile") or "")
    if not profile:
        raise PublicSkillError("skill_unavailable", status=503)

    from tubecli.core import auth

    if action == "stop":
        sid = str(req.get("session") or "")
        async with _lock:
            s = _sessions.get(agent_id)
            if s and s["sid"] == sid and s["caller"] == caller:
                await _end(agent_id, sid)
        return {"kind": "browserend", "session": sid if _SESSION_RE.match(sid) else ""}

    if action != "start":
        raise PublicSkillError("bad_input")

    async with _lock:
        s = _sessions.get(agent_id)
        if s and not _alive(s):
            await _end(agent_id, s["sid"])
            s = None
        if s:
            if s["caller"] != caller:
                raise PublicSkillError("browser_busy", status=429)
            # Cùng người (tải lại trang, mở máy khác): cấp token MỚI cho đúng phiên cũ,
            # hạn không dài thêm.
            tok = auth.mint_guest_token(s["scope"], max(60, int(s["exp"] - time.time())))
            return _live(s, tok["guest_token"])

        from tubecli.extensions.browser.routes import launch_public_preview

        r = await launch_public_preview(profile)
        if not r.get("ok"):
            reason = str(r.get("reason") or "")
            msg = str(r.get("message") or "")
            logger.info("[browser.remote] không mở được %s: %s %s", profile, reason, msg[:200])
            if reason == "in_use":
                raise PublicSkillError("browser_in_use", status=409)
            if "ISOLATE_UNSUPPORTED" in msg:
                raise PublicSkillError("browser_unsupported", status=503)
            raise PublicSkillError("browser_failed", status=502)

        minutes = int(st.get("browser_minutes") or 15)
        ttl = max(60, min(3600, minutes * 60))
        sid = secrets.token_hex(8)
        upload = "media" if str(st.get("browser_upload") or "media") == "media" else "off"
        # Người xem CHÍNH LÀ chủ máy (mã người gọi = mã chủ cloud ghi vào cloud_identity; cloud
        # tính mã từ phiên đăng nhập, người xem không tự xưng được): tải lên mọi loại file từ
        # máy đang dùng (vd. video đăng YouTube — user 28/9), kể cả khi chủ tắt tải lên cho
        # người lạ; phiên dài đủ cho một video lớn.
        owner = public_agents_owner()
        if owner and caller == owner:
            upload = "owner"
            ttl = max(ttl, OWNER_SESSION_SEC)
            minutes = ttl // 60
        scope = {"workspace": f"pubbrowser_{sid}", "public": True, "profiles": [profile],
                 "access": "control", "upload": upload}
        tok = auth.mint_guest_token(scope, ttl)
        s = {"sid": sid, "caller": caller, "profile": profile, "port": int(r["port"]),
             "exp": time.time() + ttl, "workspace": scope["workspace"], "scope": scope,
             "upload": upload}
        s["task"] = asyncio.create_task(_expire_later(agent_id, sid, ttl))
        _sessions[agent_id] = s
        logger.info("[browser.remote] mở phiên %s cho agent %s (%d phút)", sid, agent_id, minutes)
        return _live(s, tok["guest_token"])
