"""Sổ HỒ SƠ TRÌNH DUYỆT khách thuê tạo ra — ai là chủ, tên họ đặt, giữ tới bao giờ.

User 4/10/2026: «thêm cộng profile nếu muốn tạo mới, có thể tự đặt tên, tuỳ biến thuê lâu dài
và thuê 1 lần. thuê lâu dài có thể giữ session đó để thuê lại (thêm phí duy trì) còn thuê 1
lần thì hết phiên là mất» + «profile người dùng tạo chỉ có họ mới xem được người khác không
xem được».

Hai kiểu hồ sơ khách tạo:
  · THUÊ 1 LẦN  — hết phiên là xoá cả hồ sơ (không có dòng nào trong sổ này).
  · GIỮ LÂU DÀI — ghi một dòng ở đây: hồ sơ sống tới `until`, thuê lại thì gia hạn. Cloud thu
    phí giữ MỘT LẦN cho mỗi N ngày (lib/browserRent.js), máy chỉ nhận số ngày và nhớ mốc.

VÌ SAO phải có file, không để trong RAM: hồ sơ giữ lâu dài phải sống qua lần restart máy. Mất
sổ thì hồ sơ thành rác không ai nhận, mà cũng không xoá được vì không biết của ai.

RIÊNG TƯ: khoá theo `caller` — mã người gọi do cloud tính từ phiên đăng nhập (người xem không
tự xưng được, xem public_agents.callerHash). Người khác hỏi `info` thì KHÔNG thấy hồ sơ này,
và xin mở nó thì bị từ chối. Đây là lớp thứ hai; cloud cũng lọc theo người mua.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional

# Tên thư mục hồ sơ: rent_<8 ký tự mã người gọi>_<nhãn khách đặt đã làm sạch>. Tiền tố giữ
# nguyên để bộ quét nhận ra hồ sơ cho thuê; mã người gọi làm hai việc: tránh trùng tên giữa
# hai khách, và nhìn vào tên là biết hồ sơ của ai kể cả khi sổ hỏng.
PREFIX = "rent_"
LABEL_MAX = 24
# Trần số hồ sơ GIỮ LÂU DÀI mỗi khách trên một máy — chặn một người chiếm hết đĩa của chủ.
KEEP_PER_CALLER_MAX = 3
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _path() -> str:
    from tubecli.config import DATA_DIR

    return os.path.join(str(DATA_DIR), "browser_rentals.json")


def _load() -> Dict[str, Any]:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: Dict[str, Any]) -> None:
    """Ghi nguyên khối: mất điện giữa lúc ghi không được để lại file JSON vỡ (mất sổ = hồ sơ
    thành rác không ai nhận)."""
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".rentals-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def slug(label: str) -> str:
    """Nhãn khách gõ → phần tên thư mục an toàn. Rỗng/toàn ký tự lạ thì trả '' (gọi tự đặt tên)."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(label or "").strip()).strip("-_")
    return s[:LABEL_MAX]


def dir_name(caller: str, label: str = "") -> str:
    """Tên thư mục hồ sơ cho một khách. Nhãn rỗng → chỉ có tiền tố + mã người gọi + mốc giờ."""
    who = re.sub(r"[^a-f0-9]", "", str(caller or "").lower())[:8] or "anon0000"
    sl = slug(label)
    base = f"{PREFIX}{who}" + (f"_{sl}" if sl else f"_{int(time.time()) % 100000}")
    return base[:64]


def label_of(name: str) -> str:
    """Nhãn để bày cho khách: bỏ tiền tố + mã người gọi. Không có nhãn thì trả nguyên tên."""
    s = str(name or "")
    if not s.startswith(PREFIX):
        return s
    rest = s[len(PREFIX):]
    return rest.split("_", 1)[1] if "_" in rest else rest


def is_rental(name: str) -> bool:
    return str(name or "").startswith(PREFIX)


def get(name: str) -> Optional[Dict[str, Any]]:
    return _load().get(str(name))


def owned_by(caller: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Hồ sơ GIỮ LÂU DÀI còn hiệu lực của ĐÚNG người gọi này — không ai khác thấy."""
    t = time.time() if now is None else now
    out = []
    for name, r in sorted(_load().items()):
        if not isinstance(r, dict) or str(r.get("caller") or "") != str(caller):
            continue
        if float(r.get("until") or 0) <= t:
            continue
        out.append({"name": name, "label": str(r.get("label") or label_of(name)),
                    "until": float(r.get("until") or 0)})
    return out


def count_for(caller: str, now: Optional[float] = None) -> int:
    return len(owned_by(caller, now))


def keep(name: str, caller: str, days: int, label: str = "", now: Optional[float] = None) -> float:
    """Giữ hồ sơ thêm `days` ngày. Gia hạn thì CỘNG TỪ MỐC CŨ nếu mốc còn ở tương lai — khách
    thuê lại sớm không bị mất phần ngày đã trả."""
    t = time.time() if now is None else now
    d = _load()
    cur = d.get(str(name)) if isinstance(d.get(str(name)), dict) else None
    base = max(t, float(cur.get("until") or 0)) if cur and str(cur.get("caller") or "") == str(caller) else t
    until = base + max(1, int(days)) * 86400
    d[str(name)] = {"caller": str(caller), "label": slug(label) or label_of(str(name)),
                    "until": until, "at": t}
    _save(d)
    return until


def drop(name: str) -> None:
    d = _load()
    if str(name) in d:
        d.pop(str(name), None)
        _save(d)


def expired(now: Optional[float] = None) -> List[str]:
    """Hồ sơ hết hạn giữ — bộ quét của public_browser xoá cả thư mục."""
    t = time.time() if now is None else now
    return [n for n, r in _load().items()
            if isinstance(r, dict) and float(r.get("until") or 0) <= t]


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(str(name or "")))
