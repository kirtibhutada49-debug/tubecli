"""Kho file TẠM cho skill công khai (dùng chung youtube.download / capcut.tts).

File nằm trên máy chủ agent, phát cho người lạ qua link chia sẻ /s/<token> của File
Manager — chủ máy THẤY và THU HỒI được trong tab chia sẻ như mọi link khác. File +
link chỉ sống `ttl` giây; lượt gọi sau quét dọn. Đường dẫn trả về luôn TƯƠNG ĐỐI:
cloud tự ghép tên miền tunnel mà cloud đã biết, máy không tự xưng tên miền được."""
from __future__ import annotations

import os
import time


def sweep(d: str, ttl: int) -> None:
    """Xoá file cũ quá ttl trong thư mục d — hỏng thì thôi, không chặn lượt gọi."""
    now = time.time()
    try:
        for name in os.listdir(d):
            fp = os.path.join(d, name)
            try:
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > ttl:
                    os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass


def fm_enabled() -> bool:
    try:
        from tubecli.core.extension_manager import extension_manager

        return any(e.name == "file_manager" for e in extension_manager.get_enabled())
    except Exception:      # noqa: BLE001 — không hỏi được thì cứ thử, share hỏng sẽ tự lộ
        return True


def share_path(path: str, name: str, ttl: int) -> str:
    """"/s/<token>" cho file này — tái dùng link còn sống, hết hạn thì phát link mới."""
    import secrets

    from tubecli.extensions.file_manager import routes as fmr

    items = fmr._load_shares()
    key = os.path.normcase(os.path.normpath(path))
    now = time.time()
    for it in items:
        if os.path.normcase(os.path.normpath(str(it.get("path") or ""))) == key:
            exp = it.get("expires")
            if not exp or float(exp) > now + 60:
                return "/s/" + it["token"]
    items = [it for it in items if os.path.normcase(os.path.normpath(str(it.get("path") or ""))) != key]
    it = {"token": secrets.token_urlsafe(18), "path": os.path.normpath(path), "name": (name or "")[:120],
          "created": now, "expires": now + ttl, "downloads": 0}
    items.append(it)
    fmr._save_shares(items)
    return "/s/" + it["token"]
