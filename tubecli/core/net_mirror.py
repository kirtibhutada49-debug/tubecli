"""Đường tới GitHub cho máy ở mạng chặn raw.githubusercontent.com (Trung Quốc đại lục).

Đo trên ECS Aliyun Bắc Kinh 6/10/2026: raw.githubusercontent.com bị RESET ngay, tải bản phát hành
GitHub ~60 KB/s, còn proxy kiểu «tiền tố» (https://gh-proxy.com/<url gốc>) trả cùng file 0,7 s /
9 MB/s. Lõi chỉ cần MỘT hàm: mirror_url(url) — URL GitHub thì gắn tiền tố proxy, còn lại giữ nguyên.

Proxy lấy theo thứ tự: biến môi trường TUBECLI_GH_PROXY ("" = đi thẳng) → khoá "gh_proxy" trong
data/global_settings.json (install-cn.sh ghi) → KHÔNG tự dò (máy ngoài Trung Quốc không bao giờ
tốn một lượt gọi thừa; máy Trung Quốc cài bằng install-cn.sh đã có khoá).
"""
from __future__ import annotations

import json
import os

GITHUB_HOSTS = (
    "https://raw.githubusercontent.com/",
    "https://github.com/",
    "https://objects.githubusercontent.com/",
    "https://codeload.github.com/",
    "https://gist.githubusercontent.com/",
)

_cache: dict = {"mtime": None, "value": ""}


def _settings_proxy() -> str:
    try:
        from tubecli.config import GLOBAL_SETTINGS_FILE
        path = str(GLOBAL_SETTINGS_FILE)
        mtime = os.path.getmtime(path)
    except Exception:
        return ""
    if _cache["mtime"] == mtime:
        return _cache["value"]
    value = ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            value = str(data.get("gh_proxy") or "").strip()
    except Exception:
        value = ""
    _cache.update(mtime=mtime, value=value)
    return value


def gh_proxy() -> str:
    """Tiền tố proxy đang dùng ("" = đi thẳng). Luôn kết thúc bằng '/' khi khác rỗng."""
    env = os.environ.get("TUBECLI_GH_PROXY")
    p = (env if env is not None else _settings_proxy()).strip()
    if p and not p.startswith(("http://", "https://")):
        return ""
    return (p.rstrip("/") + "/") if p else ""


def mirror_url(url: str) -> str:
    """URL GitHub → qua proxy (nếu có cấu hình); URL khác, hoặc đã qua proxy rồi → giữ nguyên."""
    u = str(url or "")
    p = gh_proxy()
    if not p or u.startswith(p) or not u.startswith(GITHUB_HOSTS):
        return u
    return p + u
