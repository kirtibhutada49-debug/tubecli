"""Skill công khai «Trình duyệt từ xa» (browser.remote) — user 28/9/2026: «agent skill remote
browser, cho phép share browser mình sở hữu public, nhưng hạn chế chọn file khi upload».

Người lạ trên Town dùng một hồ sơ trình duyệt chủ đã mở cho thuê: xem trực tiếp, bấm, gõ,
lướt web — trong N phút. Dựng trên hạ tầng khách-có-phạm-vi của workspace (guest token +
cookie tubecli_guest + gate DENY mặc định), nhưng hẹp hơn hẳn:

  · preview mở bằng --isolate: mạng qua extensions/browser/net_guard.cjs (không với tới
    127.0.0.1 = dashboard của chủ, LAN, metadata cloud), lệnh điều hướng chỉ http/https,
    không tải file về máy chủ, phiên sạch (không thấy tab của người trước);
  · token scope {public: True, profiles: [hồ sơ], upload: media|off} — gate chỉ cho WS +
    screenshot + tải lên có trần (≤5 file, ≤25 MB, ảnh/video/PDF từ máy NGƯỜI XEM); không
    tự mở/tắt browser, không chọn file trên máy chủ, không Drive, không danh sách hồ sơ;
  · phiên gắn với ĐÚNG người gọi (mã caller cloud gửi); hết giờ → thu token, WS đang mở bị
    cắt (routes.guest_expiry), tắt browser, dọn file tải lên.

4/10/2026 (user: «tối ưu danh sách browser dạng grid, có thể tạo thêm profile mới để thuê,
nếu server quá tải thì không cho thuê thêm»):

  · NHIỀU PHIÊN song song, một phiên cho mỗi HỒ SƠ (trước đây một phiên cho mỗi AGENT, nên
    người thứ hai luôn nhận «đang bận» dù máy còn thừa RAM). Trần = `browser_slots` của chủ,
    và luôn bị RAM chặn trước: mỗi Chromium ~800 MB, mở quá thì OOM giết cả mấy cái cùng lúc
    (xem routes._low_memory_reason) — nên không bao giờ mở vượt số RAM đo được.
  · action «info» báo trước còn mấy chỗ / hồ sơ nào rảnh / bao lâu nữa có chỗ, để Town khoá
    nút «Thuê» thay vì để khách trả tiền rồi nhận lỗi.
  · HỒ SƠ SẠCH theo yêu cầu (`fresh`): máy tạo hồ sơ `rent_<hex>` trống, hết phiên thì XOÁ cả
    hồ sơ — khách sau không thừa hưởng đăng nhập của khách trước.

Input (JSON do cloud dựng):
    {"action": "info"}
    {"action": "start", "minutes": N, "profile": "<tên>", "fresh": true}
    {"action": "stop", "session": "<hex>"}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from tubecli.core import browser_rentals as rentals
from tubecli.core.public_agents import PublicSkillError

logger = logging.getLogger("public_browser")

_SESSION_RE = re.compile(r"^[a-f0-9]{16}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OWNER_SESSION_SEC = 2 * 3600       # chủ tự dùng: đủ đẩy một video lớn qua tunnel rồi lên YouTube
# Tiền tố hồ sơ do KHÁCH tạo — xem core/browser_rentals.py (sổ ai-là-chủ, giữ tới bao giờ).
FRESH_PREFIX = rentals.PREFIX
# Hồ sơ sạch mồ côi (máy tắt giữa phiên) quá mức này thì dọn — kẻo đĩa phình dần.
FRESH_STALE_SEC = 6 * 3600


def public_agents_owner() -> str:
    from tubecli.core import public_agents
    try:
        return str(public_agents.owner_caller() or "")
    except Exception:      # noqa: BLE001 — không biết chủ thì coi như người lạ
        return ""


# sid → phiên đang chạy. Khoá theo MÃ PHIÊN (không phải agent) vì một agent nay có thể cho
# thuê nhiều hồ sơ cùng lúc.
_sessions: Dict[str, Dict[str, Any]] = {}
_lock = asyncio.Lock()


def _alive(s: Dict[str, Any]) -> bool:
    try:
        from tubecli.extensions.browser.routes import port_is_isolated
        return s["exp"] > time.time() and port_is_isolated(s["port"])
    except Exception:      # noqa: BLE001
        return False


def _prune() -> List[Dict[str, Any]]:
    """Bỏ phiên đã chết khỏi sổ (không tắt gì — chúng tắt rồi) và trả phần còn sống."""
    for sid, s in list(_sessions.items()):
        if not _alive(s):
            _sessions.pop(sid, None)
    return list(_sessions.values())


def _agent_live(agent_id: str) -> List[Dict[str, Any]]:
    return [s for s in _prune() if s["agent_id"] == agent_id]


def _drop_fresh_profile(name: str) -> None:
    """Xoá hồ sơ khách tạo sau khi browser đã tắt (còn chạy thì Windows giữ file).

    Hồ sơ GIỮ LÂU DÀI (có dòng trong sổ, chưa hết hạn) thì KHÔNG xoá — khách đã trả phí giữ
    để thuê lại chính nó."""
    if not rentals.is_rental(name):
        return
    r = rentals.get(name)
    if r and float(r.get("until") or 0) + rentals.GRACE_SEC > time.time():
        # Còn hạn giữ, HOẶC đã hết hạn nhưng còn trong ân hạn 24 giờ để gia hạn.
        logger.info("[browser.remote] giữ hồ sơ %s tới %.0f (+ân hạn)", name, r["until"])
        return
    try:
        from tubecli.extensions.browser.profile_manager import delete_profile
        delete_profile(name)
        logger.info("[browser.remote] đã xoá hồ sơ sạch %s", name)
    except Exception as e:      # noqa: BLE001 — xoá hỏng thì để bộ quét dọn sau
        logger.warning("[browser.remote] xoá hồ sơ sạch %s hỏng: %s", name, e)


def _sweep_fresh() -> None:
    """Dọn hồ sơ khách tạo đã hết việc:
      · hết HẠN GIỮ → xoá hồ sơ + bỏ dòng trong sổ (khách không gia hạn nữa);
      · mồ côi (máy tắt giữa phiên nên sổ _sessions mất) và cũ hơn FRESH_STALE_SEC → xoá.
    """
    try:
        from tubecli.extensions.browser.profile_manager import PROFILES_DIR, delete_profile
        live = {s["profile"] for s in _sessions.values()}
        for name in rentals.expired():
            if name in live:
                continue
            rentals.drop(name)                  # bỏ dòng TRƯỚC để _drop_fresh_profile chịu xoá
            try:
                delete_profile(name)
                logger.info("[browser.remote] hết hạn giữ, đã xoá hồ sơ %s", name)
            except Exception as e:      # noqa: BLE001
                logger.warning("[browser.remote] xoá hồ sơ hết hạn %s hỏng: %s", name, e)
        now = time.time()
        for name in os.listdir(PROFILES_DIR):
            if not rentals.is_rental(name) or name in live or rentals.get(name):
                continue
            path = os.path.join(PROFILES_DIR, name)
            if os.path.isdir(path) and now - os.path.getmtime(path) > FRESH_STALE_SEC:
                _drop_fresh_profile(name)
    except Exception as e:      # noqa: BLE001 — dọn rác không được làm chết lượt gọi
        logger.debug("[browser.remote] quét hồ sơ khách tạo hỏng: %s", e)


async def _end(sid: str) -> None:
    """Thu token (WS đang mở tự bị cắt trong ≤5 s), tắt browser, dọn file tải lên."""
    s = _sessions.pop(sid, None)
    if not s:
        return
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
    # Hồ sơ sạch: xoá SAU khi đã tắt browser — khách sau không thừa hưởng gì của khách trước.
    if s.get("fresh"):
        try:
            await asyncio.shield(asyncio.to_thread(_drop_fresh_profile, s["profile"]))
        except asyncio.CancelledError:
            raise
        except Exception:      # noqa: BLE001 — bộ quét sẽ dọn
            pass
    logger.info("[browser.remote] phiên %s của agent %s kết thúc", sid, s["agent_id"])


async def _expire_later(sid: str, secs: float) -> None:
    try:
        await asyncio.sleep(max(1.0, secs))
    except asyncio.CancelledError:
        return
    await _end(sid)


def _live(s: Dict[str, Any], token: str) -> Dict[str, Any]:
    out = {"kind": "browserlive", "session": s["sid"], "token": token, "port": s["port"],
           "profile": s["profile"], "fresh": bool(s.get("fresh")),
           "expires_in": max(0, int(s["exp"] - time.time())), "upload": s["upload"],
           "label": rentals.label_of(s["profile"]) if rentals.is_rental(s["profile"]) else ""}
    if s.get("keep_until"):
        out["keep_until"] = int(s["keep_until"])
    return out


def _rentable(st: Dict[str, Any]) -> List[str]:
    """Hồ sơ chủ mở cho thuê. Danh sách mới (browser_profiles) ưu tiên; không có thì hồ sơ lẻ
    của bản cũ (browser_profile) — agent đang chạy không được mất đường thuê."""
    names = [str(x) for x in (st.get("browser_profiles") or []) if _PROFILE_RE.match(str(x))]
    if not names:
        one = str(st.get("browser_profile") or "")
        if _PROFILE_RE.match(one):
            names = [one]
    return names


def _capacity(agent_id: str, st: Dict[str, Any], caller: str = "") -> Dict[str, Any]:
    """Còn mấy chỗ, hồ sơ nào rảnh, bao lâu nữa có chỗ. Không mở gì cả.

    Danh sách hồ sơ = hồ sơ CHỦ mở cho thuê + hồ sơ GIỮ LÂU DÀI của ĐÚNG người gọi này. Hồ sơ
    của khách khác không bao giờ xuất hiện (user 4/10: «chỉ họ mới xem được»).
    """
    from tubecli.extensions.browser.routes import public_browser_capacity

    mine_kept = rentals.owned_by(caller) if caller else []
    cap = public_browser_capacity(_rentable(st) + [k["name"] for k in mine_kept])
    mine = _agent_live(agent_id)
    busy_names = {s["profile"] for s in mine}
    kept = {k["name"]: k for k in mine_kept}
    rows = []
    for p in cap["profiles"]:
        row = {"name": p["name"], "busy": bool(p["busy"]) or p["name"] in busy_names}
        k = kept.get(p["name"])
        if k:
            # Hồ sơ của chính khách: bày NHÃN họ đặt, kèm mốc hết hạn giữ để họ biết khi nào
            # phải gia hạn. `expired` + `grace_left` = đã hết hạn, còn bấy nhiêu giây để gia
            # hạn trước khi xoá vĩnh viễn.
            row.update({"mine": True, "label": k["label"], "keep_until": int(k["until"]),
                        "expired": bool(k["expired"]), "grace_left": int(k["grace_left"])})
        rows.append(row)
    slots_max = max(1, min(8, int(st.get("browser_slots") or 1)))
    ram = cap.get("ram_slots")
    # Trần thật = thấp nhất trong ba thứ: chủ cho phép, RAM mở nổi (RAM CÒN TRỐNG đã trừ phiên
    # đang chạy, nên cộng lại số đang chạy để ra tổng), và số hồ sơ thuê được.
    room = slots_max if ram is None else min(slots_max, len(mine) + ram)
    free_profiles = sum(1 for r in rows if not r["busy"])
    fresh_on = bool(st.get("browser_fresh")) and bool(cap.get("can_create"))
    # Hồ sơ sạch tạo bao nhiêu cũng được, nên khi bật thì số hồ sơ không còn là nút thắt.
    usable = room - len(mine) if fresh_on else min(room - len(mine), free_profiles)
    nxt = None
    free = max(0, usable)
    if free <= 0 and mine:
        nxt = max(0, int(min(s["exp"] for s in mine) - time.time()))
    return {"kind": "browserinfo", "profiles": rows, "can_create": fresh_on,
            "kept": len(mine_kept), "kept_max": rentals.KEEP_PER_CALLER_MAX,
            "slots": {"free": free, "max": max(room, len(mine)), "used": len(mine),
                      "next_free_in": nxt},
            "ram_free_mb": cap.get("ram_free_mb"), "session_mb": cap.get("session_mb")}


async def _make_fresh(caller: str, label: str = "") -> str:
    """Tạo hồ sơ trống cho khách, tên gồm mã người gọi + nhãn họ đặt (core/browser_rentals).

    Nhân ShardX phải có sẵn (máy đã từng tạo hồ sơ) — tải nhân là việc hàng trăm MB, không làm
    trong một lượt gọi của khách."""
    from tubecli.extensions.browser.profile_manager import create_profile

    base = rentals.dir_name(caller, label)
    for i in range(5):
        name = base if i == 0 else f"{base[:56]}-{secrets.token_hex(2)}"
        if not rentals.valid_name(name):
            raise PublicSkillError("bad_input")
        try:
            await asyncio.to_thread(create_profile, name)
            return name
        except ValueError:      # trùng tên → thêm hậu tố rồi thử lại
            continue
        except Exception as e:      # noqa: BLE001
            logger.warning("[browser.remote] tạo hồ sơ cho khách hỏng: %s", e)
            raise PublicSkillError("browser_failed", status=502)
    raise PublicSkillError("browser_failed", status=502)


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
    rentable = _rentable(st)
    if not rentable and not st.get("browser_fresh"):
        raise PublicSkillError("skill_unavailable", status=503)

    from tubecli.core import auth

    if action == "info":
        async with _lock:
            _sweep_fresh()
            return _capacity(agent_id, st, caller)

    if action == "stop":
        sid = str(req.get("session") or "")
        async with _lock:
            s = _sessions.get(sid)
            if s and s["agent_id"] == agent_id and s["caller"] == caller:
                await _end(sid)
        return {"kind": "browserend", "session": sid if _SESSION_RE.match(sid) else ""}

    if action != "start":
        raise PublicSkillError("bad_input")

    async with _lock:
        want = str(req.get("profile") or "")
        # Cùng người (tải lại trang, mở máy khác): cấp token MỚI cho đúng phiên cũ, hạn không
        # dài thêm. Khách đã trả tiền cho phiên này nên phải về đúng chỗ cũ, không mở phiên hai.
        for s in _agent_live(agent_id):
            if s["caller"] != caller or (want and s["profile"] != want):
                continue
            tok = auth.mint_guest_token(s["scope"], max(60, int(s["exp"] - time.time())))
            return _live(s, tok["guest_token"])

        # Hồ sơ khách chọn phải là hồ sơ CHỦ cho thuê, hoặc hồ sơ GIỮ LÂU DÀI của CHÍNH
        # người gọi. Kiểm TRƯỚC cổng quá tải, kẻo bấm sai hồ sơ lại nhận câu «máy đủ tải» và
        # cứ ngồi chờ một chỗ không bao giờ dùng được.
        #
        # RIÊNG TƯ: hồ sơ thuê của người KHÁC trả browser_unavailable y như hồ sơ không tồn
        # tại — không được để ai dò ra rằng cái tên đó có thật trên máy này.
        mine_kept = {k["name"] for k in rentals.owned_by(caller)}
        if want and want not in rentable and want not in mine_kept:
            raise PublicSkillError("browser_unavailable", status=404)
        _sweep_fresh()
        cap = _capacity(agent_id, st, caller)
        if cap["slots"]["free"] <= 0:
            # Hai lý do khác nhau, phải nói khác nhau:
            #   · mọi hồ sơ cho thuê ĐANG CÓ NGƯỜI dùng (và không tạo được hồ sơ sạch) →
            #     browser_busy, «đang có người khác dùng» — đúng cảnh agent một hồ sơ;
            #   · còn hồ sơ rảnh mà vẫn không mở được → trần số chỗ của chủ hoặc RAM cạn →
            #     browser_full, «máy đủ tải, chờ chỗ».
            # Town đã hỏi «info» trước nên thường không tới đây; tới thì khách KHÔNG mất xu —
            # cloud hoàn nguyên khoản đã giữ (lib/browserRent.startRental).
            all_busy = bool(cap["profiles"]) and all(p["busy"] for p in cap["profiles"])
            code = "browser_busy" if all_busy and not cap["can_create"] else "browser_full"
            logger.info("[browser.remote] agent %s hết chỗ (%s): %s", agent_id, code, cap["slots"])
            raise PublicSkillError(code, status=429)

        fresh = req.get("fresh") is True and cap["can_create"]
        # Giữ lâu dài: cloud đã thu phí giữ và gửi xuống số NGÀY. 0/không gửi = thuê 1 lần,
        # hết phiên là xoá hồ sơ.
        try:
            keep_days = int(req.get("keep_days") or 0)
        except (TypeError, ValueError):
            keep_days = 0
        keep_days = max(0, min(365, keep_days))
        label = str(req.get("name") or "")
        busy = {r["name"] for r in cap["profiles"] if r["busy"]}
        if fresh:
            # Trần hồ sơ giữ mỗi khách: chặn một người chiếm hết đĩa của chủ.
            if keep_days > 0 and rentals.count_for(caller) >= rentals.KEEP_PER_CALLER_MAX:
                raise PublicSkillError("browser_keep_full", status=409)
            profile = await _make_fresh(caller, label)
        elif want:
            if want in busy:
                raise PublicSkillError("browser_busy", status=429)
            profile = want
        else:
            # Khách không chọn: hồ sơ rảnh đầu tiên; hết hồ sơ mà chủ cho tạo thì tạo sạch.
            free = [r["name"] for r in cap["profiles"] if not r["busy"]]
            if free:
                profile = free[0]
            elif cap["can_create"]:
                profile, fresh = await _make_fresh(caller, label), True
            else:
                raise PublicSkillError("browser_busy", status=429)

        from tubecli.extensions.browser.routes import launch_public_preview

        r = await launch_public_preview(profile)
        if not r.get("ok"):
            reason = str(r.get("reason") or "")
            msg = str(r.get("message") or "")
            logger.info("[browser.remote] không mở được %s: %s %s", profile, reason, msg[:200])
            if fresh:
                await asyncio.to_thread(_drop_fresh_profile, profile)
            if reason == "in_use":
                raise PublicSkillError("browser_in_use", status=409)
            # RAM cạn ngay giữa lúc mở (đo xong rồi việc khác chiếm): cùng nghĩa «chờ chỗ» với
            # browser_full — đừng bắt khách đọc lỗi kỹ thuật.
            if reason == "low_memory":
                raise PublicSkillError("browser_full", status=429)
            if "ISOLATE_UNSUPPORTED" in msg:
                raise PublicSkillError("browser_unsupported", status=503)
            raise PublicSkillError("browser_failed", status=502)

        minutes = int(st.get("browser_minutes") or 15)
        if str(st.get("browser_mode") or "minute") == "session":
            # Bán theo PHIÊN: độ dài do chủ đặt, khách không chọn (cloud đã giữ đúng giá phiên).
            minutes = int(st.get("browser_session_minutes") or minutes)
        # Khách THUÊ theo phút (cloud đã giữ tiền đúng số phút này): phiên dài đúng số đã trả,
        # không quá trần chủ đặt. Không gửi → cả trần như trước (agent miễn phí, cloud cũ).
        try:
            w = int(req.get("minutes") or 0)
        except (TypeError, ValueError):
            w = 0
        if w > 0:
            minutes = max(1, min(minutes, w))
        # Trần 4 giờ: phiên cố định bán được tới 240 phút (BROWSER_SESSION_MINUTES của lõi).
        ttl = max(60, min(4 * 3600, minutes * 60))
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
        s = {"sid": sid, "agent_id": agent_id, "caller": caller, "profile": profile,
             "port": int(r["port"]), "exp": time.time() + ttl, "workspace": scope["workspace"],
             "scope": scope, "upload": upload, "fresh": bool(fresh)}
        s["task"] = asyncio.create_task(_expire_later(sid, ttl))
        _sessions[sid] = s
        # Giữ lâu dài: ghi sổ NGAY (không chờ hết phiên) — máy tắt giữa phiên thì hồ sơ vẫn
        # được giữ, đúng thứ khách đã trả tiền cho.
        if keep_days > 0 and rentals.is_rental(profile):
            try:
                until = rentals.keep(profile, caller, keep_days, label)
                s["keep_until"] = until
                logger.info("[browser.remote] giữ hồ sơ %s cho %s thêm %d ngày", profile, caller[:8], keep_days)
            except Exception as e:      # noqa: BLE001 — không giữ được thì vẫn cho dùng phiên
                logger.warning("[browser.remote] ghi sổ giữ hồ sơ hỏng: %s", e)
        logger.info("[browser.remote] mở phiên %s cho agent %s trên %s (%d phút%s)",
                    sid, agent_id, profile, minutes, ", hồ sơ sạch" if fresh else "")
        return _live(s, tok["guest_token"])
