"""Skill «phụ đề YouTube» cho NGƯỜI LẠ gọi qua Agent Town (core/public_agents.py).

Vì sao chỉ có bấy nhiêu đây (chốt 23/9/2026, sau khi soi extension Web Crawler):
Douyin an toàn được nhờ HAI CÁI NEO — đầu vào ép về đúng tên miền, đầu ra lọc theo đúng
tên miền đó. Một web crawler đúng nghĩa phải gỡ bỏ cả hai cái neo ấy, nên không mở. Phụ
đề YouTube giữ nguyên được cả hai, chặt hơn cả Douyin:

  • ĐẦU VÀO: chỉ móc ra MÃ VIDEO 11 ký tự trong tin nhắn. URL gọi đi do lõi tự ghép
    (`https://www.youtube.com/watch?v=<id>`), KHÔNG lấy một mẩu nào từ chữ người lạ gõ —
    nên không có đường nào trỏ máy sang localhost, sang mạng nội bộ hay sang site khác.
  • ĐẦU RA: chỉ có CHỮ. Không link lạ, không file, không ảnh — cloud không phải tin một
    tên miền nào ngoài chính youtube.com.
  • KHÔNG COOKIE: chạy như một người khách vãng lai. Cookie/tài khoản Google của chủ máy
    không bao giờ dính vào lượt gọi của người lạ, và video riêng tư của chủ cũng không lọt
    ra qua bộ nhớ đệm (lõi nhớ riêng hai chế độ).
  • KHÔNG GHI GÌ: không lưu file, không vào lịch sử tải, không vào kho nội dung.
"""
from __future__ import annotations

import asyncio
import re
from functools import partial
from typing import Any, Dict

from tubecli.core.public_agents import PublicSkillError

_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

MAX_CHARS = 20000          # đủ cho một video ~2 giờ; dài hơn thì cắt và nói rõ là đã cắt
FETCH_TIMEOUT_SEC = 25     # cloud chờ 50 s, lõi cắt ở 40 s — chừa chỗ cho đường về

# Video công khai hoặc «không công khai nhưng ai có link đều xem được». Còn riêng tư /
# chỉ hội viên / trả tiền thì không đụng tới, kể cả khi yt-dlp lấy được.
_OPEN = ("", "public", "unlisted", "unlisted_from_search")


def pick_link(text: str) -> str:
    """Mã video YouTube đầu tiên trong tin nhắn, hay '' nếu không có. Không đoán.

    Dùng đúng bộ dò của lõi (core/youtube_transcript.youtube_ids): nó chỉ nhận
    youtube.com / youtu.be / shorts / embed và mã trần 11 ký tự."""
    from tubecli.core.youtube_transcript import youtube_ids

    t = str(text or "").strip()
    ids = youtube_ids(t)
    if ids:
        return ids[0]
    return t if _BARE_ID_RE.match(t) else ""


def _hms(seconds: int) -> str:
    s = max(0, int(seconds or 0))
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _code_for(message: str) -> str:
    low = (message or "").lower()
    if "sign in" in low or "not a bot" in low:
        return "yt_signin"
    if "no subtitles" in low or "almost empty" in low:
        return "no_subtitles"
    if "private" in low or "unavailable" in low or "removed" in low or "age-restricted" in low:
        return "video_unavailable"
    if "yt-dlp" in low:
        return "skill_unavailable"
    return "skill_failed"


async def resolve(text: str) -> Dict[str, Any]:
    vid = pick_link(text)
    if not vid:
        raise PublicSkillError("need_youtube_link")

    from tubecli.core.youtube_transcript import fetch_transcript

    # fetch_transcript là hàm chặn (yt-dlp + tải file phụ đề) — chạy ở luồng khác để vòng
    # lặp async của máy vẫn phục vụ chủ trong lúc chờ.
    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(partial(fetch_transcript, vid, timeout=20, use_cookies=False)),
            timeout=FETCH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        raise PublicSkillError("timeout", status=504)
    except ImportError:
        raise PublicSkillError("skill_unavailable", status=503)

    if not res.get("ok"):
        code = _code_for(str(res.get("message") or ""))
        raise PublicSkillError(code, status=404 if code == "video_unavailable" else 422)
    if str(res.get("availability") or "") not in _OPEN:
        raise PublicSkillError("video_unavailable", status=404)

    body = str(res.get("text") or "")
    cut = len(body) > MAX_CHARS
    return {
        "kind": "text",
        "title": str(res.get("title") or "")[:300],
        "author": str(res.get("channel") or "")[:80],
        "duration": _hms(res.get("duration") or 0),
        "language": str(res.get("language") or "")[:16],
        "auto": res.get("kind") == "auto",
        "words": int(res.get("words") or 0),
        "text": body[:MAX_CHARS],
        "truncated": cut,
        "source": f"https://www.youtube.com/watch?v={res.get('id') or vid}",
    }
# ── Skill «tải video YouTube» (user 27/9/2026: agent Douyin thêm tải YouTube) ────────
# Giữ nguyên HAI CÁI NEO của phụ đề: ĐẦU VÀO chỉ là mã video 11 ký tự (lõi tự ghép URL);
# ĐẦU RA là một đường dẫn /s/<token> TƯƠNG ĐỐI — cloud tự ghép với tên miền tunnel mà
# CLOUD đã biết của máy này, nên máy bị chiếm cũng không trỏ người xem sang miền lạ.
#
# Vì sao không trả link googlevideo như Douyin trả douyinvod: link googlevideo khoá theo
# IP máy phân tích — người xem ở mạng khác tải là 403. Nên máy tải hộ (KHÔNG cookie,
# ≤720p, có trần thời lượng + dung lượng) rồi phát lại qua link chia sẻ của File
# Manager. File + link sống DL_TTL_SEC rồi lượt gọi sau tự dọn.
import json as _json
import os
import time as _time

DL_MAX_SEC = 20 * 60                  # video dài quá 20 phút: từ chối TRƯỚC khi tải
DL_MAX_BYTES = 300 * 1024 * 1024      # yt-dlp bỏ tải nếu file vượt trần
DL_TTL_SEC = 6 * 3600
DL_BUDGET_SEC = 36                    # tổng dò + tải, nằm trong trần 40 s của lõi


def _town_dir() -> str:
    from tubecli.extensions.video_downloader.routes import _get_download_dir

    d = os.path.join(_get_download_dir(), "town")
    os.makedirs(d, exist_ok=True)
    return d


def _sweep(d: str) -> None:
    from tubecli.core import public_share as _ps

    _ps.sweep(d, DL_TTL_SEC)


def _existing_file(d: str, vid: str):
    for ext in ("mp4", "webm", "mkv"):
        fp = os.path.join(d, f"{vid}.{ext}")
        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
            return fp
    return None


def _meta_path(d: str, vid: str) -> str:
    return os.path.join(d, f"{vid}.json")


def _fm_enabled() -> bool:
    from tubecli.core import public_share as _ps

    return _ps.fm_enabled()


def _share_path(path: str, name: str) -> str:
    from tubecli.core import public_share as _ps

    return _ps.share_path(path, name, DL_TTL_SEC)


def _download_blocking(url: str, vid: str, dest: str) -> str:
    import yt_dlp

    from tubecli.core import ytdlp_manager
    from tubecli.extensions.video_downloader.routes import (
        _ffmpeg_location_for_ytdlp, _get_ffmpeg_path, _ytdlp_can_merge,
    )

    ff = _get_ffmpeg_path()
    loc = _ffmpeg_location_for_ytdlp(ff)
    merge = _ytdlp_can_merge(ff)
    # Ghép được thì lấy tới 720p; không thì đành dòng mp4 liền tiếng (thường 360p) —
    # còn hơn là "requested merging but ffmpeg is not installed" (bài học routes.py).
    fmt = ("bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[ext=mp4][height<=720]/b[ext=mp4]/b"
           if merge else "b[ext=mp4][height<=720]/b[ext=mp4]/b")
    opts = {"quiet": True, "noprogress": True, "no_warnings": True, "noplaylist": True, "retries": 2,
            "socket_timeout": 15, "outtmpl": os.path.join(dest, f"{vid}.%(ext)s"),
            "max_filesize": DL_MAX_BYTES, "format": fmt}
    if merge:
        opts["merge_output_format"] = "mp4"
    if loc:
        opts["ffmpeg_location"] = loc
    opts.update(ytdlp_manager.js_runtime_opts())
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    got = _existing_file(dest, vid)
    if not got:
        # ydl.download xong mà không có file = max_filesize đã bỏ tải giữa chừng.
        raise PublicSkillError("video_too_big")
    return got


async def resolve_download(text: str) -> Dict[str, Any]:
    vid = pick_link(text)
    if not vid:
        raise PublicSkillError("need_youtube_link")
    if not _fm_enabled():
        raise PublicSkillError("skill_unavailable", status=503)

    started = _time.monotonic()
    url = f"https://www.youtube.com/watch?v={vid}"
    d = _town_dir()
    _sweep(d)

    got = _existing_file(d, vid)
    meta = {}
    if got:
        try:
            with open(_meta_path(d, vid), encoding="utf-8") as f:
                meta = _json.load(f) or {}
        except (OSError, ValueError):
            meta = {}
    else:
        from tubecli.core.youtube_transcript import _ydl_extract

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(partial(_ydl_extract, url, 15)), timeout=16)
        except asyncio.TimeoutError:
            raise PublicSkillError("timeout", status=504)
        except ImportError:
            raise PublicSkillError("skill_unavailable", status=503)
        except Exception as e:      # noqa: BLE001
            code = _code_for(str(e))
            raise PublicSkillError(code, status=404 if code == "video_unavailable" else 422)

        if str(info.get("availability") or "") not in _OPEN or info.get("is_live"):
            raise PublicSkillError("video_unavailable", status=404)
        dur = int(info.get("duration") or 0)
        if not dur or dur > DL_MAX_SEC:
            raise PublicSkillError("video_too_long")

        left = DL_BUDGET_SEC - (_time.monotonic() - started)
        if left < 5:
            raise PublicSkillError("timeout", status=504)
        try:
            got = await asyncio.wait_for(
                asyncio.to_thread(partial(_download_blocking, url, vid, d)), timeout=left)
        except asyncio.TimeoutError:
            raise PublicSkillError("timeout", status=504)

        meta = {"title": str(info.get("title") or "")[:300],
                "author": str(info.get("channel") or info.get("uploader") or "")[:80],
                "dur": dur}
        try:
            with open(_meta_path(d, vid), "w", encoding="utf-8") as f:
                _json.dump(meta, f, ensure_ascii=False)
        except OSError:
            pass

    share = _share_path(got, (str(meta.get("title") or "youtube"))[:60] + ".mp4")
    return {
        "kind": "ytfile",
        "title": str(meta.get("title") or ""),
        "author": str(meta.get("author") or ""),
        "duration": _hms(int(meta.get("dur") or 0)),
        "size": int(os.path.getsize(got)),
        "share": share + "/download",
        "page": share,
        "cover": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "source": f"https://www.youtube.com/watch?v={vid}",
        "expires_h": int(DL_TTL_SEC // 3600),
    }
