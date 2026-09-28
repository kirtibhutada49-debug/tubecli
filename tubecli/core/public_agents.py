"""Agent công khai — người xem Agent Town (cloud.tubecreate.com) chat được với agent của máy.

Luồng (user chốt 22/9/2026):
    trình duyệt ──(phiên cloud)──► cloud Worker ──(HMAC town_key, qua tunnel)──► máy này
                                                   POST /api/v1/public/invoke

AI LÀ NGƯỜI LẠ. Đây là cửa DUY NHẤT người ngoài chạm được vào máy, nên nó hẹp có chủ ý:

  1. Không chạy workflow, không chạy agent tự do, không có LLM. Chỉ gọi đúng các HÀM
     trong PUBLIC_SKILLS dưới đây — mỗi hàm được viết riêng cho người lạ (Douyin: chỉ
     phân tích link, không tải file, không ghi lịch sử, không dùng cookie đăng nhập của
     chủ). Thêm skill công khai = viết thêm một hàm như thế, không phải bật cờ trên một
     skill có sẵn.
  2. Chỉ cloud gọi được: chữ ký HMAC bằng khoá riêng của máy (town_key, cloud cấp qua
     cloud_identity), có mốc giờ ±5 phút và nonce dùng một lần. Người xem không bao giờ
     biết URL của máy — cloud đứng giữa.
  3. Chủ bật từng agent, chọn từng skill, đặt trần lượt/ngày; tắt là dừng ngay ở lượt kế.
  4. Cài đặt nằm ở file riêng (public_agents.json), KHÔNG trong đối tượng Agent: AI agent
     sửa được cấu hình của chính nó qua vài đường, còn file này chỉ phiên đăng nhập của
     chủ ghi được (api/public_routes.py).

Hồ sơ đẩy lên cloud chỉ gồm các trường trong _profile_row — danh sách TRẮNG. Thêm trường
ở đó là mở rộng thứ ai cũng đọc được trên trang chủ.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

PROFILE_PATH = "/api/town/agents"
SIG_WINDOW_SEC = 300
PUSH_EVERY_SEC = 120.0          # nhịp sống: cloud coi agent là offline sau ~5 phút im lặng
INVOKE_TIMEOUT_SEC = 40.0       # cloud chờ 50 giây, chừa 10 giây cho đường về
MAX_RUNNING_TOTAL = 4          # cả máy, mọi agent cộng lại — chủ không nới được
DEFAULT_DAILY_CAP = 100
MAX_DAILY_CAP = 1000

# Ngưỡng chủ đặt cho TỪNG agent (user chốt 22/9/2026). Trạng thái người xem thấy trên Town:
#   rảnh · bận (đủ lượt song song) · mệt (máy quá tải → tạm ngừng nhận) ·
#   sắp hết quota (≥ warn_pct % trần/ngày) · hết quota (đủ trần)
# CPU/RAM đặt 100 = không bao giờ «mệt».
THRESHOLDS = {
    # khoá: (mặc định, nhỏ nhất, lớn nhất)
    "warn_pct": (80, 50, 95),
    "max_parallel": (2, 1, 4),
    "cpu_tired": (85, 50, 100),
    "ram_tired": (90, 50, 100),
}

# Ai NHÌN THẤY agent trên Agent Town (user chốt 23/9/2026):
#   public  — ai mở trang chủ cũng thấy và chat được (cần đăng nhập để chat)
#   private — CHỈ tài khoản cloud sở hữu máy này thấy trong danh sách và chat được
# «Tắt» vẫn là enabled=False: không đẩy lên cloud tí nào.
# Cài đặt cũ (chưa có trường này) rơi về 'public' — đó đúng là thứ chủ đã chọn khi bật.
VISIBILITIES = ("public", "private")
DEFAULT_VISIBILITY = "public"

_NAME_RE = re.compile(r"^[\w .\-]{2,32}$", re.UNICODE)
_HASH_RE = re.compile(r"^[a-f0-9]{16}$")
_NONCE_RE = re.compile(r"^[a-f0-9]{16,64}$")
_CALLER_RE = re.compile(r"^[a-f0-9]{8,32}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class PublicSkillError(Exception):
    """Lỗi có MÃ ổn định để cloud dịch ra đủ 9 ngôn ngữ; message chỉ là dự phòng."""

    def __init__(self, code: str, message: str = "", status: int = 422):
        super().__init__(message or code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class PublicSkill:
    id: str                       # «douyin.resolve» — cloud giữ nhãn/mô tả đã dịch theo id này
    extension: str                # nhà trên Agent Town (manifest.name)
    handler: Callable[[str], Awaitable[Dict[str, Any]]] = field(compare=False)
    max_input: int = 1000


async def _douyin_resolve(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # import muộn: extension có thể bị tắt/thiếu thư viện, và lõi không được gãy theo nó
    from tubecli.extensions.douyin_downloader.public_skill import resolve

    return await resolve(text)


async def _youtube_transcript(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from tubecli.extensions.video_downloader.public_skill import resolve

    return await resolve(text)


async def _youtube_download(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from tubecli.extensions.video_downloader.public_skill import resolve_download

    return await resolve_download(text, opts)


async def _capcut_tts(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from tubecli.core.public_tts import resolve

    return await resolve(text, opts)


async def _chess_move(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from tubecli.core.public_chess import resolve

    return await resolve(text, opts)


async def _xiangqi_move(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from tubecli.core.public_xiangqi import resolve

    return await resolve(text, opts)


async def _browser_remote(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from tubecli.core.public_browser import resolve

    return await resolve(text, opts)


async def _douyin_reup(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from tubecli.core.public_reup import resolve

    return await resolve(text, opts)


PUBLIC_SKILLS: Dict[str, PublicSkill] = {
    s.id: s for s in (
        PublicSkill("douyin.resolve", "douyin_downloader", _douyin_resolve),
        # Chỉ NHẬN MÃ VIDEO, trả về CHỮ, không cookie của chủ — xem đầu file
        # extensions/video_downloader/public_skill.py. 300 ký tự là thừa cho một link.
        PublicSkill("youtube.transcript", "video_downloader", _youtube_transcript, max_input=300),
        # Máy tải hộ (link googlevideo khoá theo IP) rồi phát qua đường dẫn /s/<token>
        # TƯƠNG ĐỐI của chính máy — cloud tự ghép tên miền tunnel (resolve_download).
        PublicSkill("youtube.download", "video_downloader", _youtube_download, max_input=300),
        # Văn bản → mp3 bằng giọng CapCut (bể tài khoản của chủ). `opts.voice` do cloud
        # chọn từ danh sách cứng; xem core/public_tts.py.
        PublicSkill("capcut.tts", "capcut_tts", _capcut_tts, max_input=700),
        # MỘT nước cờ mỗi lượt gọi — cloud làm trọng tài, đầu vào là JSON {fen, turn,
        # history} cloud tự dựng; xem core/public_chess.py.
        PublicSkill("chess.move", "ai_arena", _chess_move, max_input=4000),
        # Cờ tướng: engine luật nằm ngay trong Arena (engine/xiangqi_game.py); đầu vào
        # còn kèm danh sách nước hợp lệ do trọng tài cloud dựng — hai tầng kiểm.
        PublicSkill("xiangqi.move", "ai_arena", _xiangqi_move, max_input=6000),
        # Reup Douyin: job hai pha (link → thẻ tiến độ; {"job"} → trạng thái/kết quả) —
        # xem core/public_reup.py. Chuỗi tải→phụ đề→lồng tiếng chạy nền trên máy.
        PublicSkill("douyin.reup", "douyin_downloader", _douyin_reup, max_input=600),
        # Trình duyệt từ xa: người lạ dùng MỘT hồ sơ chủ chọn, trong preview cô lập mạng —
        # xem core/public_browser.py. Input là JSON {"action"} do cloud dựng.
        PublicSkill("browser.remote", "browser", _browser_remote, max_input=200),
    )
}


def _enabled_extensions() -> Optional[set]:
    try:
        from tubecli.core.extension_manager import extension_manager

        return {e.name for e in extension_manager.get_enabled()}
    except Exception:
        return None                # không hỏi được thì đừng giấu skill — lượt gọi tự báo lỗi


def available_skills() -> List[Dict[str, Any]]:
    on = _enabled_extensions()
    return [
        {"id": s.id, "extension": s.extension, "available": on is None or s.extension in on}
        for s in PUBLIC_SKILLS.values()
    ]


# ── Cài đặt ──────────────────────────────────────────────────────────────────
_lock = threading.RLock()


def _path() -> str:
    from tubecli.config import DATA_DIR

    return os.path.join(str(DATA_DIR), "public_agents.json")


def _load_all() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_all(data: Dict[str, Dict[str, Any]]) -> None:
    p = _path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _clean_text(v: Any, limit: int) -> str:
    return _CTRL_RE.sub(" ", str(v or "")).strip()[:limit]


def normalise(raw: Dict[str, Any], agent_name: str = "", old: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Chuẩn hoá thứ chủ gửi lên. Sai dạng → ValueError với mã ổn định để trang dịch."""
    name = _clean_text(raw.get("name"), 32) or _clean_text(agent_name, 32)
    if not _NAME_RE.match(name):
        raise ValueError("bad_name")
    skills = []
    for s in raw.get("skills") or []:
        s = str(s)
        if s in PUBLIC_SKILLS and s not in skills:
            skills.append(s)
    try:
        cap = int(raw.get("daily_cap") or DEFAULT_DAILY_CAP)
    except (TypeError, ValueError):
        cap = DEFAULT_DAILY_CAP
    enabled = bool(raw.get("enabled"))
    # Agent «chỉ cho thuê» hợp lệ: không skill chat nào nhưng bật nhận việc + có mẫu.
    _hire_on_now = raw.get("hire_on") if "hire_on" in raw else (old or {}).get("hire_on")
    if enabled and not skills and not _hire_on_now:
        raise ValueError("no_skills")
    # Không gửi trường → giữ nguyên thứ đang lưu (mặc định cho agent mới là công khai).
    # Gửi chữ lạ → công khai, KHÔNG đoán là riêng tư: đoán sai kiểu đó làm agent đang
    # chạy của người ta biến mất khỏi phố.
    if "visibility" in raw:
        vis = str(raw.get("visibility") or DEFAULT_VISIBILITY).strip().lower()
        if vis not in VISIBILITIES:
            vis = DEFAULT_VISIBILITY
    else:
        vis = str((old or {}).get("visibility") or DEFAULT_VISIBILITY).strip().lower()
        if vis not in VISIBILITIES:
            vis = DEFAULT_VISIBILITY
    out = {
        "enabled": enabled,
        "visibility": vis,
        "name": name,
        "bio": _clean_text(raw.get("bio"), 160),
        "skills": skills,
        "daily_cap": max(1, min(MAX_DAILY_CAP, cap)),
    }
    for k, (dflt, lo, hi) in THRESHOLDS.items():
        try:
            v = int(raw.get(k) if raw.get(k) not in (None, "") else dflt)
        except (TypeError, ValueError):
            v = dflt
        out[k] = max(lo, min(hi, v))
    out.update(_browser_settings(raw, old or {}))
    out.update(_hire_settings(raw, old or {}))
    if enabled and "browser.remote" in skills and not out["browser_profile"]:
        raise ValueError("no_browser_profile")
    return out


BROWSER_MINUTES = (15, 5, 60)          # mặc định, tối thiểu, tối đa — một phiên của người lạ
BROWSER_UPLOADS = ("media", "off")     # media = ảnh/video/PDF từ MÁY NGƯỜI XEM; off = cấm


def _browser_settings(raw: Dict[str, Any], old: Dict[str, Any]) -> Dict[str, Any]:
    """Cài đặt skill browser.remote. Trường không gửi → giữ bản đang lưu (client cũ không
    biết tới chúng thì không được xoá mất hồ sơ chủ đã chọn). Hồ sơ phải CÓ THẬT trên máy —
    tên hồ sơ không bao giờ rời máy (Town chỉ biết agent có trình duyệt để dùng)."""
    if "browser_profile" in raw:
        bp = _clean_text(raw.get("browser_profile"), 64)
        if bp and bp not in _profile_names():
            raise ValueError("bad_browser_profile")
    else:
        bp = str(old.get("browser_profile") or "")
    dflt, lo, hi = BROWSER_MINUTES
    try:
        mins = int(raw.get("browser_minutes") if raw.get("browser_minutes") not in (None, "")
                   else old.get("browser_minutes") or dflt)
    except (TypeError, ValueError):
        mins = dflt
    up = str(raw.get("browser_upload") or old.get("browser_upload") or BROWSER_UPLOADS[0]).strip().lower()
    return {"browser_profile": bp, "browser_minutes": max(lo, min(hi, mins)),
            "browser_upload": up if up in BROWSER_UPLOADS else BROWSER_UPLOADS[0]}


HIRE_UNITS = ("job", "minute")
HIRE_PRICE_MAX = 500000          # = PRICE_MAX của cloud (lib/hire.js)
HIRE_MINUTES_MAX = 60
HIRE_PRESETS_MAX = 60            # = HIRE_PRESETS_MAX của cloud (lib/hire.js) — từng là 12


def _hire_settings(raw: Dict[str, Any], old: Dict[str, Any]) -> Dict[str, Any]:
    """Cài đặt NHẬN VIỆC THUÊ (Chợ mẫu). Trường không gửi → giữ bản đang lưu.

    `hire_presets` = TÊN các mẫu (preset Content Studio) agent phục vụ — user chốt 28/9:
    chủ agent cài mẫu, khách không mua, chỉ chọn trong danh sách này. Mã Chợ của từng mẫu
    máy tự tra lúc ĐẨY hồ sơ (market_links.json), không lưu ở đây — đăng lại mẫu lên Chợ
    được mã mới thì hồ sơ đẩy sau tự đúng."""
    on = bool(raw.get("hire_on")) if "hire_on" in raw else bool(old.get("hire_on"))
    try:
        price = int(raw.get("hire_price") if raw.get("hire_price") not in (None, "")
                    else old.get("hire_price") or 0)
    except (TypeError, ValueError):
        price = 0
    unit = str(raw.get("hire_unit") or old.get("hire_unit") or "job").strip().lower()
    try:
        mmax = int(raw.get("hire_minutes_max") if raw.get("hire_minutes_max") not in (None, "")
                   else old.get("hire_minutes_max") or 10)
    except (TypeError, ValueError):
        mmax = 10
    if "hire_presets" in raw:
        presets, seen = [], set()
        for p in (raw.get("hire_presets") or [])[:HIRE_PRESETS_MAX]:
            name = _clean_text(p, 60)
            if name and name not in seen:
                seen.add(name)
                presets.append(name)
    else:
        presets = [str(x) for x in (old.get("hire_presets") or [])][:HIRE_PRESETS_MAX]
    if ("hire_on" in raw and on) and not presets:
        raise ValueError("no_hire_presets")
    return {"hire_on": on, "hire_price": max(0, min(HIRE_PRICE_MAX, price)),
            "hire_unit": unit if unit in HIRE_UNITS else "job",
            "hire_minutes_max": max(1, min(HIRE_MINUTES_MAX, mmax)),
            "hire_presets": presets}


def _market_links() -> Dict[str, str]:
    """{tên mẫu: mã Chợ} — Content Studio ghi khi «Bán trên Chợ» (market_links.json)."""
    try:
        from tubecli.config import DATA_DIR

        p = os.path.join(str(DATA_DIR), "content_studio", "market_links.json")
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:      # noqa: BLE001 — chưa bán mẫu nào / Studio chưa cài
        return {}


def _profile_names() -> set:
    try:
        from tubecli.extensions.browser.profile_manager import list_profiles

        return {str(p.get("name")) for p in list_profiles() if isinstance(p, dict) and p.get("name")}
    except Exception:      # noqa: BLE001 — không đọc được danh sách thì không nhận hồ sơ nào
        return set()


def threshold(st: Dict[str, Any], key: str) -> int:
    """Ngưỡng của một agent; cài đặt cũ (trước khi có ngưỡng) rơi về mặc định."""
    dflt, lo, hi = THRESHOLDS[key]
    try:
        return max(lo, min(hi, int(st.get(key, dflt))))
    except (TypeError, ValueError):
        return dflt


# ── Tải của máy («mệt») ─────────────────────────────────────────────────────
_load_cache = {"at": 0.0, "cpu": 0.0, "ram": 0.0}


def machine_load() -> Optional[Dict[str, float]]:
    """{cpu, ram} phần trăm, cache 10 giây. psutil.cpu_percent(None) đo từ lần gọi trước,
    nên gọi thưa thì ra trung bình cả khoảng — đúng thứ cần để nói «máy đang quá tải»,
    không phải một cú nhảy tức thời. Không có psutil → None (không bao giờ «mệt»)."""
    now = time.time()
    if now - _load_cache["at"] < 10:
        return {"cpu": _load_cache["cpu"], "ram": _load_cache["ram"]}
    try:
        import psutil

        _load_cache.update(at=now, cpu=float(psutil.cpu_percent(interval=None)),
                           ram=float(psutil.virtual_memory().percent))
    except Exception:
        return None
    return {"cpu": _load_cache["cpu"], "ram": _load_cache["ram"]}


def is_tired(st: Dict[str, Any], load: Optional[Dict[str, float]]) -> bool:
    if not load:
        return False
    cpu_t, ram_t = threshold(st, "cpu_tired"), threshold(st, "ram_tired")
    return (cpu_t < 100 and load["cpu"] >= cpu_t) or (ram_t < 100 and load["ram"] >= ram_t)


def get_settings(agent_id: str) -> Dict[str, Any]:
    with _lock:
        return dict(_load_all().get(str(agent_id)) or {})


def set_settings(agent_id: str, raw: Dict[str, Any], agent_name: str = "") -> Dict[str, Any]:
    with _lock:
        data = _load_all()
        clean = normalise(raw, agent_name, data.get(str(agent_id)))
        data[str(agent_id)] = clean
        _save_all(data)
    _pusher.kick()
    return clean


def _agents_by_id() -> Dict[str, Any]:
    try:
        from tubecli.core.agent import agent_manager

        return {a.id: a for a in agent_manager.get_all()}
    except Exception:
        return {}


def public_entries() -> List[Dict[str, Any]]:
    """[{agent_id, hash, settings}] của agent ĐANG bật, còn tồn tại, còn skill chat HOẶC
    đang nhận việc thuê (hire_on) — agent «chỉ cho thuê» không có skill chat nào vẫn phải
    được đẩy lên Town, không thì trang mẫu không bao giờ thấy nó."""
    from tubecli.core.town_telemetry import agent_hash

    alive = _agents_by_id()
    with _lock:
        data = _load_all()
    out = []
    for aid, st in data.items():
        if aid not in alive or not isinstance(st, dict) or not st.get("enabled"):
            continue
        skills = [s for s in st.get("skills") or [] if s in PUBLIC_SKILLS]
        if not skills and not (st.get("hire_on") and st.get("hire_presets")):
            continue
        out.append({"agent_id": aid, "hash": agent_hash(aid), "settings": {**st, "skills": skills}})
    return out


def owner_caller() -> str:
    """Mã người gọi của chủ tài khoản cloud (cloud ghi qua /api/v1/instance/cloud-identity)."""
    try:
        from tubecli.core import cloud_identity

        return str(cloud_identity.load().get("owner") or "")
    except Exception:      # noqa: BLE001
        return ""


def _visible_to(st: Dict[str, Any], caller: str) -> bool:
    """Agent công khai: ai cũng gọi được. Còn lại: chỉ chủ tài khoản cloud.

    Danh sách CHO PHÉP, cố ý: chỉ đúng chữ "public" mới mở. Viết `!= "private"` thì một
    chữ lạ trong file cài đặt (viết hoa, thừa dấu cách, ai đó sửa tay) lại mở agent ra
    cho người lạ — khoá mà hỏng thì phải đóng."""
    if str(st.get("visibility") or DEFAULT_VISIBILITY).strip().lower() == "public":
        return True
    own = owner_caller()
    return bool(own) and hmac.compare_digest(str(caller or ""), own)


def _profile_row(entry: Dict[str, Any], load: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Hồ sơ đẩy lên cloud. Chỉ ngưỡng và cờ «mệt» — KHÔNG số CPU/RAM thô của máy."""
    st = entry["settings"]
    vis = str(st.get("visibility") or DEFAULT_VISIBILITY)
    skills = list(st["skills"])
    hire = None
    if st.get("hire_on") and st.get("hire_presets"):
        links = _market_links()
        # Chỉ mẫu ĐÃ CÓ MÃ trên Chợ mới lên trang thuê (cloud cũng bỏ mục thiếu mã).
        hire = {"on": True, "price": int(st.get("hire_price") or 0),
                "unit": str(st.get("hire_unit") or "job"),
                "minutes_max": int(st.get("hire_minutes_max") or 10),
                "presets": [{"n": n, "c": links.get(n, "")} for n in st["hire_presets"]]}
        # cloud đòi skill 'content.video' trong hồ sơ để hiện nút thuê — nó KHÔNG phải
        # skill chat của máy (invoke từ chối như mọi skill lạ), chỉ là cờ trên Town.
        if "content.video" not in skills:
            skills.append("content.video")
    elif "hire_on" in st:
        hire = {"on": False, "price": int(st.get("hire_price") or 0)}
    row = {"a": entry["hash"], "name": st.get("name", ""), "bio": st.get("bio", ""),
           "skills": skills, "cap": int(st.get("daily_cap") or DEFAULT_DAILY_CAP),
           "warn": threshold(st, "warn_pct"), "par": threshold(st, "max_parallel"),
           "tired": is_tired(st, load),
           # Cloud mới mới hiểu trường này; cloud cũ bỏ qua và vẫn coi là công khai —
           # nên KHÔNG được bật «riêng tư» ở máy rồi tin là cloud đã giấu.
           "vis": vis if vis in VISIBILITIES else DEFAULT_VISIBILITY}
    if hire is not None:
        row["hire"] = hire
    return row


# ── Chữ ký ───────────────────────────────────────────────────────────────────
def _identity() -> Optional[Dict[str, str]]:
    try:
        from tubecli.core import cloud_identity

        ident = cloud_identity.load()
    except Exception:
        return None
    code, key = ident.get("server_code"), ident.get("town_key")
    return {"code": code, "key": key} if code and key else None


def cloud_ready() -> bool:
    return _identity() is not None


def sign(key: str, domain: str, ts: str, body: bytes, nonce: str = "") -> str:
    # Tiền tố miền («invoke.», «agents.») để chữ ký của đường này không bao giờ dùng
    # được cho đường kia, dù cùng một khoá — telemetry ký «<ts>.<body>» trần.
    msg = f"{domain}.{ts}.".encode("utf-8") + (f"{nonce}.".encode("utf-8") if nonce else b"") + body
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class _Nonces:
    """Nonce đã thấy trong cửa sổ chữ ký — một request bắt được không phát lại được."""

    def __init__(self, cap: int = 5000):
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._cap = cap
        self._lock = threading.Lock()

    def use(self, nonce: str, now: float) -> bool:
        with self._lock:
            while self._seen and next(iter(self._seen.values())) < now - 2 * SIG_WINDOW_SEC:
                self._seen.popitem(last=False)
            if nonce in self._seen:
                return False
            if len(self._seen) >= self._cap:
                self._seen.popitem(last=False)
            self._seen[nonce] = now
            return True


_nonces = _Nonces()


def verify_invoke(ts: Any, nonce: Any, sig: Any, body: bytes, now: Optional[float] = None,
                  domain: str = "invoke") -> str:
    """'' nếu hợp lệ, còn lại là mã lỗi. Nonce chỉ bị «tiêu» khi chữ ký ĐÚNG — không thì
    ai cũng đốt được nonce của cloud bằng request rác. `domain` tách chữ ký từng route
    («invoke», «catalog»): chữ ký của route này không bao giờ dùng được cho route kia."""
    ident = _identity()
    if not ident:
        return "not_configured"
    now = time.time() if now is None else now
    try:
        at = int(str(ts))
    except (TypeError, ValueError):
        return "bad_signature"
    if abs(now - at) > SIG_WINDOW_SEC:
        return "bad_signature"
    nonce = str(nonce or "")
    if not _NONCE_RE.match(nonce):
        return "bad_signature"
    want = sign(ident["key"], domain, str(at), body, nonce)
    if not hmac.compare_digest(want, str(sig or "")):
        return "bad_signature"
    if not _nonces.use(nonce, now):
        return "replayed"
    return ""


# ── Gọi skill ────────────────────────────────────────────────────────────────
class _Gate:
    """Trần song song + trần lượt/ngày. Trong RAM: khởi động lại thì đếm lại từ 0, cloud
    vẫn giữ trần của nó nên không mở toang được."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running: Dict[str, int] = {}
        self._total = 0
        self._day = ""
        self._count: Dict[str, int] = {}

    def enter(self, agent_id: str, cap: int, parallel: int = 2) -> str:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            if today != self._day:
                self._day, self._count = today, {}
            if self._count.get(agent_id, 0) >= cap:
                return "daily_cap"
            if self._running.get(agent_id, 0) >= parallel or self._total >= MAX_RUNNING_TOTAL:
                return "busy"
            self._running[agent_id] = self._running.get(agent_id, 0) + 1
            self._total += 1
            self._count[agent_id] = self._count.get(agent_id, 0) + 1
            return ""

    def leave(self, agent_id: str) -> None:
        with self._lock:
            self._running[agent_id] = max(0, self._running.get(agent_id, 0) - 1)
            self._total = max(0, self._total - 1)

    def usage(self, agent_id: str) -> Dict[str, int]:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            used = self._count.get(agent_id, 0) if today == self._day else 0
            return {"used": used, "running": self._running.get(agent_id, 0)}


def usage(agent_id: str) -> Dict[str, int]:
    """Số lượt hôm nay (UTC) và số lượt đang chạy — cho tab Công khai của chủ."""
    return _gate.usage(str(agent_id))


_gate = _Gate()


async def catalog(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Danh mục giọng của skill capcut.tts — đọc thẳng extension CapCut TTS trên máy này
    (ngôn ngữ + giọng theo vùng tài khoản, đã lọc giọng đọc sai tiếng) thay cho 6 giọng
    cloud từng viết cứng (user 28/9: «có khá nhiều ngôn ngữ mà sao vẫn load 3 ngôn ngữ»).

    Kèm `default` THEO AGENT (user 28/9: «ngôn ngữ của skill sẽ đi theo ngôn ngữ của agent»):
    giọng mặc định của agent nếu là CapCut → ngôn ngữ đặt cho agent → rỗng (trang tự lấy
    ngôn ngữ giao diện). Chỉ đọc: không qua cổng hạn mức, không báo Town, không gọi CapCut
    đọc gì."""
    if not isinstance(payload, dict):
        raise PublicSkillError("bad_request", status=400)
    h = str(payload.get("agent") or "")
    skill_id = str(payload.get("skill") or "")
    caller = str(payload.get("caller") or "")
    if not _HASH_RE.match(h) or (caller and not _CALLER_RE.match(caller)):
        raise PublicSkillError("bad_request", status=400)
    entry = next((e for e in public_entries() if e["hash"] == h), None)
    if not entry or not _visible_to(entry["settings"], caller):
        raise PublicSkillError("agent_not_public", status=404)
    # Agent đang NHẬN VIỆC thuê (hire_on) cũng được xem danh mục giọng dù không mở skill
    # chat capcut.tts: form thuê trên Town cho khách chọn giọng đọc theo mẫu (user 28/9).
    # Chỉ đọc danh mục — invoke capcut.tts thật vẫn bị chặn như cũ nếu skill không bật.
    st = entry["settings"]
    _hire_ok = bool(st.get("hire_on") and st.get("hire_presets"))
    if skill_id != "capcut.tts" or (skill_id not in st["skills"] and not _hire_ok):
        raise PublicSkillError("skill_not_allowed", status=403)

    from tubecli.core import public_tts

    data = await asyncio.to_thread(public_tts.catalog_blocking)
    langs = {l["code"] for l in data["languages"]}
    lang_of = {v["id"]: v["lang"] for v in data["voices"]}
    default: Dict[str, str] = {}
    try:
        from tubecli.core.agent import agent_manager
        from tubecli.core.agent_media import agent_voice

        engine, voice = agent_voice(entry["agent_id"])
        if engine == "capcut" and voice in lang_of:
            default = {"voice": voice, "lang": lang_of[voice]}
        else:
            a = agent_manager.get(entry["agent_id"])
            base = str(getattr(a, "language", "") or "").split("-")[0].lower()
            if base in langs:
                default = {"lang": base}
    except Exception:      # noqa: BLE001 — không đọc được agent thì để trang tự chọn
        default = {}
    return {**data, "default": default}


async def invoke(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Chạy MỘT skill công khai. Ném PublicSkillError (có mã) khi không chạy được."""
    if not isinstance(payload, dict):
        raise PublicSkillError("bad_request", status=400)
    h = str(payload.get("agent") or "")
    skill_id = str(payload.get("skill") or "")
    text = payload.get("input")
    if not _HASH_RE.match(h) or not isinstance(text, str):
        raise PublicSkillError("bad_request", status=400)
    caller = str(payload.get("caller") or "")
    if caller and not _CALLER_RE.match(caller):
        raise PublicSkillError("bad_request", status=400)

    entry = next((e for e in public_entries() if e["hash"] == h), None)
    if not entry:
        raise PublicSkillError("agent_not_public", status=404)
    # Agent «riêng tư»: cloud đã lọc danh sách, nhưng máy KHÔNG tin cloud. Mã người gọi
    # phải khớp mã chủ tài khoản mà cloud đã ghi vào cloud_identity. Chưa biết mã chủ thì
    # TỪ CHỐI (đóng khi hỏng) — và trả đúng mã lỗi của «không có agent này», để dò mã băm
    # cũng không phân biệt được agent riêng tư với agent không tồn tại.
    if not _visible_to(entry["settings"], caller):
        raise PublicSkillError("agent_not_public", status=404)
    skill = PUBLIC_SKILLS.get(skill_id)
    if not skill or skill_id not in entry["settings"]["skills"]:
        raise PublicSkillError("skill_not_allowed", status=403)
    text = text.strip()
    if not text or len(text) > skill.max_input:
        raise PublicSkillError("bad_input")

    agent_id = entry["agent_id"]
    st = entry["settings"]
    # «Mệt»: máy đang quá ngưỡng chủ đặt → tạm ngừng nhận việc công khai, để việc của
    # chính chủ không bị người lạ làm chậm. Đo ngay lúc gọi, không đợi nhịp đẩy hồ sơ.
    if is_tired(st, machine_load()):
        raise PublicSkillError("tired", status=429)
    why = _gate.enter(agent_id, int(st.get("daily_cap") or DEFAULT_DAILY_CAP), threshold(st, "max_parallel"))
    if why:
        raise PublicSkillError(why, status=429)

    from tubecli.core import town_telemetry

    # Người trên bản đồ đi tới đúng nhà của skill trong lúc chạy — người xem thấy agent
    # mình vừa nhờ đang làm việc.
    #
    # Agent RIÊNG TƯ thì KHÔNG báo gì: bản đồ trang chủ là công khai, và một người đi bộ
    # kèm mốc giờ + nhà đang đứng là bằng chứng «có ai đó vừa nhờ một agent giấu mặt».
    # Giấu agent khỏi danh sách mà vẫn vẽ nó làm việc thì giấu chưa xong. Đổi lại: lượt
    # chat riêng tư không vào số đếm của nhà; số lượt của chủ vẫn đếm đủ trong tab Công khai.
    quiet = str(st.get("visibility") or DEFAULT_VISIBILITY) == "private"

    def report(status: str, secs: float = 0.0) -> None:
        if not quiet:
            town_telemetry.report(skill.extension, status, agent_id, secs)

    report("running")
    started = time.time()
    ok = False
    try:
        opts = payload.get("opts") if isinstance(payload.get("opts"), dict) else {}
        # Skill cần biết agent nào đang trả lời (cờ vua: play-turn lấy model theo agent
        # Flow). Tiêm Ở ĐÂY, sau xác thực — cloud không gửi và không giả được trường này.
        # Mọi khoá "_" của cloud bị bỏ trước: chỉ lõi được đặt chúng.
        opts = {k: v for k, v in opts.items() if not str(k).startswith("_")}
        opts = {**opts, "_agent_id": agent_id, "_caller": caller}
        if skill_id == "browser.remote":
            # phiên gắn với MỘT người xem + hồ sơ/phút/tải lên chủ đã chọn
            opts["_settings"] = {k: st.get(k) for k in ("browser_profile", "browser_minutes", "browser_upload")}
        result = await asyncio.wait_for(skill.handler(text, opts), timeout=INVOKE_TIMEOUT_SEC)
        ok = True
        return result
    except asyncio.TimeoutError:
        raise PublicSkillError("timeout", status=504)
    except PublicSkillError:
        raise
    except Exception as e:
        logger.warning("[public] %s failed: %s", skill_id, e)
        raise PublicSkillError("skill_failed", status=502)
    finally:
        _gate.leave(agent_id)
        report("success" if ok else "error", time.time() - started)


# ── Đẩy hồ sơ lên cloud ──────────────────────────────────────────────────────
class _Pusher:
    """Đẩy danh sách agent công khai lên cloud: ngay khi chủ đổi cài đặt, rồi mỗi 2 phút
    làm nhịp sống. Không có agent công khai nào thì im — trừ MỘT lần đẩy danh sách rỗng
    sau khi tắt hết, để cloud gỡ chúng khỏi trang chủ ngay chứ không đợi hết hạn."""

    def __init__(self):
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._had_agents = False

    def kick(self) -> None:
        self._ensure_thread()
        self._wake.set()

    def start(self) -> None:
        self._ensure_thread()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="public-agents", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(PUSH_EVERY_SEC)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.push_once()
            except Exception as e:                  # không bao giờ nổi lên trên
                logger.debug("[public] push failed: %s", e)

    def push_once(self) -> Optional[int]:
        from tubecli.core.town_telemetry import CLOUD_URL, _enabled

        if not _enabled():
            return None
        entries = public_entries()
        if not entries and not self._had_agents:
            return None
        ident = _identity()
        if not ident:
            return None
        load = machine_load()
        body = json.dumps({"agents": [_profile_row(e, load) for e in entries]},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))
        req = urllib.request.Request(
            CLOUD_URL + PROFILE_PATH, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Town-Server": ident["code"],
                "X-Town-Ts": ts,
                "X-Town-Sig": sign(ident["key"], "agents", ts, body),
                "User-Agent": "TubeCLI-Town/1.0",   # tunnel chặn UA mặc định của urllib
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                res.read()
                self._had_agents = bool(entries)
                return res.status
        except urllib.error.HTTPError as e:
            return e.code


_pusher = _Pusher()


def start() -> None:
    """Gọi lúc khởi động server: đẩy hồ sơ lần đầu nếu có agent công khai."""
    try:
        _pusher.start()
    except Exception:
        pass
