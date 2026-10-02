"""Muse (muse.ai — agent AI của Meta) làm một nhà cung cấp AI như 9Router: viết chữ + vẽ ảnh.

User 2/10/2026: «máy dev browser chayagent có phiên đăng nhập muse.ai — tích hợp vào để có thể tạo ảnh,
tạo kịch bản bằng cách chọn model trong cloud api giống 9router».

Muse KHÔNG có API: chat đi qua WebSocket mã hoá (Noise), nên ta lái chính ứng dụng web của nó trong
một hồ sơ trình duyệt TubeCLI đã đăng nhập (extensions/browser/muse_tool.cjs, nối CDP vào phiên đang
chạy — cách làm của github.com/duclm1x1/Muse-Chat-MCP). Mô-đun này là MỘT chỗ biết:

  * hồ sơ nào giữ phiên Muse (cài đặt `muse_profile` trong global_settings.json, chọn ở Cloud API Keys)
  * mở hồ sơ ấy ẨN khi nó đang tắt (như youtube_cookies), rồi để nó sống tối đa HIDDEN_SESSION_MAX
  * mỗi lúc MỘT lượt (một tài khoản Muse = một người gõ) — _LOCK
  * gõ vào chat phụ nào: dùng lại một chat phụ cho `muse_turns_per_chat` lượt rồi mở chat phụ mới
    (mỗi lượt một chat phụ thì danh sách chat của người dùng ngập rác; dồn hết vào một chat thì ngữ
    cảnh cũ lẫn vào câu trả lời và trang nặng dần)
  * dựng prompt từ messages kiểu OpenAI, và dựng lời xin ảnh theo khung hình

Đo thật 2/10/2026 (hồ sơ chayagent): câu ngắn ~8 s cả mở tab; kịch bản JSON 3 cảnh ~10 s; ảnh 16:9 ra
2048×1152, 9:16 ra 1152×2048 (webp) trong ~25–30 s.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger("tubecli.muse")

PROVIDER = "muse"
CHAT_MODEL = "muse-spark"
IMAGE_MODEL = "muse-image"
CHAT_MODELS = [CHAT_MODEL]
IMAGE_MODELS = [IMAGE_MODEL]
DEFAULT_TURNS_PER_CHAT = 10
MAX_TURNS_PER_CHAT = 100
CHAT_TIMEOUT = 300
IMAGE_TIMEOUT = 300
# Chờ tới lượt (lượt khác đang chạy). Lô ảnh của Studio gửi song song: phải XẾP HÀNG, không được hỏng.
QUEUE_WAIT = 1800
LAUNCH_WAIT = 120
LAUNCH_SETTLE = 4
# Hồ sơ tự mở ẩn để phục vụ Muse sống tối đa chừng này (monitor của process_manager tự giết), lượt sau
# thấy nó tắt thì mở lại.
HIDDEN_SESSION_MAX = 1800
ASPECTS = {"16:9": "landscape", "9:16": "vertical portrait", "1:1": "square",
           "4:3": "landscape", "3:4": "portrait"}

_LOCK = threading.Lock()


class MuseError(Exception):
    """kind: config | browser | auth | timeout | approval | refused | busy | error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def is_muse_model(model) -> bool:
    """Model này của Muse? Nhận theo TÊN (như 9Router) — model mặc định của máy không mang provider."""
    return str(model or "").strip().lower().startswith("muse")


# ── cài đặt ───────────────────────────────────────────────────────────────────

def _clamp_turns(v) -> int:
    try:
        n = int(str(v if v is not None else "").strip() or DEFAULT_TURNS_PER_CHAT)
    except (TypeError, ValueError):
        return DEFAULT_TURNS_PER_CHAT
    return max(1, min(MAX_TURNS_PER_CHAT, n))


def settings() -> dict:
    """{profile, turns_per_chat}. profile "" = chưa chọn hồ sơ nào."""
    try:
        from tubecli.config import read_global_settings
        g = read_global_settings()
    except Exception:      # noqa: BLE001
        g = {}
    return {"profile": str(g.get("muse_profile") or "").strip(),
            "turns_per_chat": _clamp_turns(g.get("muse_turns_per_chat"))}


def _profiles_dir() -> str:
    from tubecli.extensions.browser.profile_manager import PROFILES_DIR
    return str(PROFILES_DIR)


def set_settings(profile: Optional[str] = None, turns_per_chat: Optional[int] = None) -> dict:
    """Chỉ ghi khoá được truyền. Hồ sơ phải có thật (tên gõ sai = mọi lượt hỏng mà trông như đã cấu hình)."""
    from tubecli.config import set_global_setting
    if profile is not None:
        p = str(profile or "").strip()
        if p:
            if p in (".", "..") or "/" in p or "\\" in p or not os.path.isdir(os.path.join(_profiles_dir(), p)):
                raise ValueError(f"Browser profile '{p}' does not exist on this machine.")
        # Đổi hồ sơ = đổi tài khoản: pick_thread thấy state["profile"] khác nên tự mở chat phụ mới.
        set_global_setting("muse_profile", p)
    if turns_per_chat is not None:
        set_global_setting("muse_turns_per_chat", _clamp_turns(turns_per_chat))
    return settings()


def local_base_url() -> str:
    """Cổng chuẩn OpenAI do CHÍNH TubeCLI phục vụ (api/muse_routes.py) — cho chỗ nào chỉ biết nói kiểu
    OpenAI qua HTTP (Content Studio đọc PROVIDERS[...]["base_url"])."""
    try:
        from tubecli.config import get_api_port
        port = int(get_api_port())
    except Exception:      # noqa: BLE001
        port = 5295
    return f"http://127.0.0.1:{port}/api/v1/muse/v1"


# ── trạng thái chat phụ đang dùng ─────────────────────────────────────────────

def _state_file() -> str:
    from tubecli.config import DATA_DIR
    return os.path.join(str(DATA_DIR), "muse_state.json")


def _load_state() -> dict:
    try:
        with open(_state_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:      # noqa: BLE001
        return {}


def _save_state(d: dict) -> None:
    try:
        path = _state_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:      # noqa: BLE001
        logger.warning("muse: could not save chat state: %s", e)


def pick_thread(state: dict, profile: str, turns_per_chat: int, fresh: bool = False) -> str:
    """"new" hay id chat phụ để gõ tiếp."""
    tid = str(state.get("thread") or "")
    if fresh or not tid or state.get("profile") != profile:
        return "new"
    if int(state.get("turns") or 0) >= turns_per_chat:
        return "new"
    return tid


def next_state(state: dict, profile: str, thread: str, result: dict) -> dict:
    """Trạng thái sau một lượt: chat phụ mới thì đếm lại từ 1."""
    tid = str(result.get("thread_id") or "")
    if not tid:
        return state
    if thread == "new" or tid != state.get("thread"):
        return {"profile": profile, "thread": tid, "turns": 1, "updated_at": int(time.time())}
    return {**state, "turns": int(state.get("turns") or 0) + 1, "updated_at": int(time.time())}


# ── trình duyệt ───────────────────────────────────────────────────────────────

def _tool_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "extensions", "browser", "muse_tool.cjs")


def _port_alive(port: int) -> bool:
    """Cổng này có Chromium thật đang nghe (GET /json/version) — cùng phép thử với browser/routes."""
    try:
        from tubecli.extensions.browser.routes import _cdp_alive
        return bool(_cdp_alive(int(port)))
    except Exception:      # noqa: BLE001
        return False


def _cdp_port_from_processes(profile: str) -> int:
    """Cổng CDP đọc từ CHÍNH dòng lệnh Chromium của hồ sơ này: tiến trình chính có `--user-data-dir=<hồ sơ>`
    và `--remote-debugging-port=N` (N = 0 là cổng ngẫu nhiên → xem cổng tiến trình ấy đang nghe), rồi gõ cửa
    /json/version.

    Vì sao cần: preview_cdp.json chỉ được tin khi pid khớp bản ghi trong RAM của server — server khởi động lại là
    mất dấu, lượt mở ẩn kế tiếp xoá luôn file ấy (_STALE_CDP_FILES) rồi mở TRÙNG hồ sơ đang chạy → «Failed to
    launch the browser process» và nút Thử vẽ báo hỏng trong khi trình duyệt vẫn sống (2/10/2026). Đường dẫn hồ
    sơ trong dòng lệnh là bằng chứng cổng thuộc đúng hồ sơ — không cần file nào.
    """
    try:
        import psutil
    except Exception:      # noqa: BLE001
        return 0
    target = os.path.normcase(os.path.realpath(os.path.join(_profiles_dir(), profile)))
    for p in psutil.process_iter(["cmdline"]):
        try:
            cmd = list(p.info.get("cmdline") or [])
        except Exception:      # noqa: BLE001
            continue
        if not cmd or any(a.startswith("--type=") for a in cmd):
            continue        # renderer / gpu… — chỉ tiến trình chính mang cổng
        udd = next((a[len("--user-data-dir="):] for a in cmd if a.startswith("--user-data-dir=")), "")
        if not udd or os.path.normcase(os.path.realpath(udd.strip('"'))) != target:
            continue
        flag = next((a for a in cmd if a.startswith("--remote-debugging-port=")), "")
        try:
            port = int(flag.split("=", 1)[1]) if flag else 0
        except ValueError:
            port = 0
        if port and _port_alive(port):
            return port
        try:
            ports = sorted({c.laddr.port for c in p.net_connections(kind="tcp")
                            if c.status == "LISTEN" and c.laddr and c.laddr.port})
        except Exception:      # noqa: BLE001
            ports = []
        for cand in ports:
            if _port_alive(cand):
                return cand
    return 0


def _cdp_port(profile: str) -> int:
    try:
        from tubecli.extensions.browser.routes import _live_cdp_port
        port = int(_live_cdp_port(profile) or 0)
        if port:
            return port
    except Exception as e:      # noqa: BLE001
        logger.info("muse: cannot read the CDP port of %s: %s", profile, e)
    return _cdp_port_from_processes(profile)


def _launch_hidden(profile: str) -> None:
    """Mở hồ sơ ẨN ở muse.ai (cùng lối với youtube_cookies.refresh_attempt) — ném MuseError nếu không được."""
    try:
        from tubecli.extensions.browser.routes import _is_launching, is_profile_running, launch_refusal
        from tubecli.extensions.browser.process_manager import browser_process_manager
    except Exception as e:      # noqa: BLE001
        raise MuseError("browser", f"The browser extension is unavailable ({e}).")
    ref = launch_refusal(profile)
    if ref:
        raise MuseError("browser", f"Cannot open browser profile '{profile}': "
                                   f"{ref.get('message') or ref.get('code') or 'blocked'}")
    if _is_launching(profile) or is_profile_running(profile):
        return ""                       # đang mở dở — chỉ việc chờ cổng CDP
    logger.info("muse: opening browser profile %s in the background", profile)
    res = browser_process_manager.spawn(profile=profile, url="https://muse.ai/", headless=True, manual=True,
                                        max_duration=HIDDEN_SESSION_MAX)
    if not isinstance(res, dict) or res.get("status") == "error":
        raise MuseError("browser", f"Could not open browser profile '{profile}' "
                                   f"({(res or {}).get('error') or 'launch failed'}).")
    return str(res.get("instance_id") or "")


def _instance_status(instance_id: str) -> Optional[dict]:
    try:
        from tubecli.extensions.browser.process_manager import browser_process_manager
        return browser_process_manager.get_status(instance_id)
    except Exception:      # noqa: BLE001
        return None


def ensure_browser(profile: str, launch: bool = True, sleep=time.sleep) -> int:
    """Cổng CDP của phiên đang chạy; tắt thì mở ẩn rồi chờ (launch=True). Tiến trình mở ẩn chết sớm (hồ sơ đang
    bị một Chromium khác giữ, thiếu RAM…) → báo ngay lý do, không ngồi đợi hết LAUNCH_WAIT."""
    port = _cdp_port(profile)
    if port or not launch:
        if not port:
            raise MuseError("browser", f"Browser profile '{profile}' is not running.")
        return port
    inst = _launch_hidden(profile)
    deadline = time.time() + LAUNCH_WAIT
    while time.time() < deadline:
        port = _cdp_port(profile)
        if port:
            sleep(LAUNCH_SETTLE)        # muse.ai đang nạp trong tab đầu — cho nó chạy xong
            return port
        if inst:
            cur = _instance_status(inst)
            if cur and cur.get("status") not in (None, "running", "starting"):
                why = cur.get("error") or cur.get("message") or cur.get("status")
                raise MuseError("browser", f"Browser profile '{profile}' closed before it was ready ({why}).")
        sleep(1.5)
    raise MuseError("browser", f"Browser profile '{profile}' did not become ready within {LAUNCH_WAIT} s.")


def run_tool(port: int, action: str, req: Optional[dict] = None, timeout: int = 60) -> dict:
    """Chạy muse_tool.cjs, trả JSON nó in giữa hai dấu. Không bao giờ trả None."""
    from tubecli.core import proc as _proc
    args = ["node", _tool_path(), "--cdp", str(int(port)), "--action", action]
    tmp = None
    if req is not None:
        fd, tmp = tempfile.mkstemp(prefix="muse_req_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False)
        args += ["--in", tmp]
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout + 45, **_proc.hidden_kwargs())
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "timeout", "error": f"Muse did not answer within {timeout} s."}
    except FileNotFoundError:
        return {"ok": False, "kind": "browser", "error": "Node.js is not installed — the Muse driver needs it."}
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return parse_tool_output(r.stdout or "", r.stderr or "")


def parse_tool_output(stdout: str, stderr: str = "") -> dict:
    m = re.search(r"__MUSE_RESULT__(.*?)__MUSE_END__", stdout or "", re.S)
    if not m:
        why = (stderr or stdout or "").strip().splitlines()
        return {"ok": False, "kind": "error", "error": "Muse driver failed: " + (why[-1] if why else "no output")[:300]}
    try:
        d = json.loads(m.group(1))
    except ValueError as e:
        return {"ok": False, "kind": "error", "error": f"Muse driver returned bad JSON: {e}"}
    return d if isinstance(d, dict) else {"ok": False, "kind": "error", "error": "Muse driver returned no object"}


# ── trạng thái + một lượt hỏi ─────────────────────────────────────────────────

def status() -> dict:
    """Không mở trình duyệt, không gõ gì: {configured, profile, running, logged_in, verified, message}."""
    st = settings()
    profile = st["profile"]
    out = {"provider": PROVIDER, "configured": bool(profile), "profile": profile,
           "turns_per_chat": st["turns_per_chat"], "running": False, "logged_in": None, "verified": False,
           "models": CHAT_MODELS, "image_models": IMAGE_MODELS, "busy": _LOCK.locked(), "message": ""}
    state = _load_state()
    if state.get("profile") == profile and state.get("thread"):
        out["thread"] = state.get("thread")
        out["thread_turns"] = int(state.get("turns") or 0)
    if not profile:
        out["message"] = "Pick the browser profile that is signed in to muse.ai."
        return out
    port = _cdp_port(profile)
    if not port:
        # Tắt KHÔNG phải hỏng: lượt hỏi đầu tiên tự mở nó ẩn.
        out["message"] = f"Browser profile '{profile}' is closed — it opens in the background on the first request."
        return out
    out["running"] = True
    if _LOCK.locked():
        out["message"] = "Muse is answering another request."
        return out
    res = run_tool(port, "status", timeout=20)
    if res.get("ok"):
        out["logged_in"] = bool(res.get("logged_in"))
        out["verified"] = bool(res.get("verified"))
        if not out["logged_in"]:
            out["message"] = f"Browser profile '{profile}' is not signed in to muse.ai — open it and sign in."
    else:
        out["message"] = str(res.get("error") or "status check failed")
    return out


def ask(prompt: str, *, want_images: bool = False, files: Optional[List[str]] = None,
        image_dir: str = "", max_images: int = 1, timeout: int = CHAT_TIMEOUT, fresh: bool = False,
        launch: bool = True, want_videos: bool = False, video_dir: str = "", max_videos: int = 1,
        thread_id: str = "") -> dict:
    """Một lượt hỏi Muse → kết quả của muse_tool (text, images, videos, thread_id…). Ném MuseError.

    thread_id: gõ vào ĐÚNG chat phụ này (chuỗi clip nối tiếp phải ở cùng một chat để Muse giữ mạch), bỏ qua
    chat phụ dùng chung; "" = chat phụ dùng chung như thường."""
    st = settings()
    profile = st["profile"]
    if not profile:
        raise MuseError("config", "Muse is not set up: pick the browser profile that is signed in to muse.ai "
                                  "in Cloud API Keys → Muse.")
    if not _LOCK.acquire(timeout=QUEUE_WAIT):
        raise MuseError("busy", "Muse has been busy with other requests for too long.")
    try:
        port = ensure_browser(profile, launch=launch)
        state = _load_state()
        own = bool(thread_id)
        thread = thread_id if own else pick_thread(state, profile, st["turns_per_chat"], fresh)
        req = {"prompt": prompt, "thread": thread, "timeout_ms": int(timeout * 1000),
               "want_images": bool(want_images), "max_images": int(max_images or 1),
               "image_dir": image_dir or "", "files": list(files or []),
               "want_videos": bool(want_videos), "max_videos": int(max_videos or 1), "video_dir": video_dir or ""}
        res = run_tool(port, "ask", req, timeout=timeout)
        if not res.get("ok") and thread != "new" and not own and res.get("kind") == "error" and not res.get("thread_id"):
            # Chat phụ đã bị xoá / không mở được → mở chat phụ mới, MỘT lần.
            logger.warning("muse: chat %s unusable (%s) — starting a new one", thread, res.get("error"))
            thread, req["thread"] = "new", "new"
            res = run_tool(port, "ask", req, timeout=timeout)
        if not own:
            _save_state(next_state(state if thread != "new" else {}, profile, thread, res))
        if not res.get("ok"):
            raise MuseError(str(res.get("kind") or "error"), str(res.get("error") or "Muse request failed."))
        return res
    finally:
        _LOCK.release()


# ── chat kiểu OpenAI ──────────────────────────────────────────────────────────

def text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits = []
        for p in content:
            if isinstance(p, str):
                bits.append(p)
            elif isinstance(p, dict) and p.get("type") in (None, "text", "input_text"):
                bits.append(str(p.get("text") or p.get("content") or ""))
        return "\n".join(b for b in bits if b)
    return str(content)


def images_of(messages: List[Dict], folder: str) -> List[str]:
    """Ảnh data: URI trong content kiểu OpenAI (image_url) → file tạm để đính vào ô soạn của Muse."""
    out = []
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for p in content:
            if not isinstance(p, dict) or p.get("type") not in ("image_url", "input_image"):
                continue
            url = p.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            mm = re.match(r"^data:image/(\w+);base64,(.+)$", str(url or ""), re.S)
            if not mm:
                continue
            ext = {"jpeg": "jpg"}.get(mm.group(1).lower(), mm.group(1).lower())
            path = os.path.join(folder, f"ref_{len(out)}.{ext}")
            with open(path, "wb") as f:
                f.write(base64.b64decode(mm.group(2)))
            out.append(path)
    return out


def build_prompt(messages: List[Dict]) -> str:
    """messages kiểu OpenAI → MỘT tin gửi Muse.

    Muse giữ hội thoại của chính nó, và chat phụ được dùng lại nhiều lượt — nên câu mở đầu nói rõ đây
    là yêu cầu độc lập. Các lượt trước (nếu có) đi kèm như NGỮ CẢNH, không giả làm bản ghi hội thoại:
    Muse-Chat-MCP đo được nhãn kiểu "### USER / ### ASSISTANT" làm Muse gạt đi là "bản ghi giả".
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    system = "\n\n".join(text_of(m.get("content")) for m in msgs if m.get("role") in ("system", "developer"))
    rest = [m for m in msgs if m.get("role") not in ("system", "developer")]
    last_i = max((i for i, m in enumerate(rest) if m.get("role") in ("user", "tool")), default=-1)
    request = text_of(rest[last_i].get("content")) if last_i >= 0 else ""
    earlier = rest[:last_i] if last_i > 0 else []
    parts = ["[New independent request — answer only this message; ignore anything earlier in this chat.]"]
    if system.strip():
        parts.append("Instructions:\n" + system.strip())
    if earlier:
        lines = []
        for m in earlier:
            t = text_of(m.get("content")).strip()
            if t:
                who = "Assistant" if m.get("role") == "assistant" else "User"
                lines.append(f"{who} said: {t}")
        if lines:
            parts.append("Earlier messages, for context only:\n" + "\n\n".join(lines))
    parts.append(("Request:\n" if len(parts) > 1 else "") + request.strip())
    return "\n\n".join(p for p in parts if p.strip())


def chat_completion(messages: List[Dict], model: str = CHAT_MODEL, timeout: int = CHAT_TIMEOUT) -> str:
    """Câu trả lời chữ của Muse. Ném MuseError."""
    with tempfile.TemporaryDirectory(prefix="muse_in_") as tmp:
        refs = images_of(messages, tmp)
        prompt = build_prompt(messages)
        if not prompt.strip() and not refs:
            raise MuseError("error", "The request has no text.")
        res = ask(prompt, files=refs, timeout=timeout)
    text = str(res.get("text") or "").strip()
    if not text:
        raise MuseError("error", "Muse returned an empty reply.")
    return text


# ── vẽ ảnh ────────────────────────────────────────────────────────────────────

def image_request(prompt: str, aspect_ratio: str = "16:9", with_refs: bool = False) -> str:
    """Lời xin MỘT ảnh: khung hình nói bằng chữ (Muse không có tham số kích thước — 16:9 ra 2048×1152)."""
    ar = aspect_ratio if aspect_ratio in ASPECTS else "16:9"
    lines = [
        "Generate exactly ONE image now. Do not ask questions, do not explain, and do not write anything "
        "in your reply — reply with the image only.",
        f"Aspect ratio: {ar} ({ASPECTS[ar]}).",
        "Do not put any words, letters, captions, logos or watermarks inside the image unless the description "
        "below explicitly asks for them.",
    ]
    if with_refs:
        lines.append("Use the attached image(s) as the visual reference for the characters and style.")
    lines.append("")
    lines.append("Image description:")
    lines.append(str(prompt or "").strip())
    return "\n".join(lines)


# Muse trả lời bằng chữ thay cho ảnh: chỉ là TỪ CHỐI khi câu chữ nói vậy (bộ vẽ không lùi sang nhà khác với lời
# từ chối nội dung). Hỏi lại / tán chuyện thì là lỗi thường — lô ảnh còn đường lùi.
_REFUSAL_RE = re.compile(r"\b(can'?t|cannot|unable to|won'?t|not able to|sorry|policy|policies|guidelines|"
                         r"not allowed|inappropriate)\b|không thể|xin lỗi|chính sách", re.I)
# Câu BÁO LỖI của chính Muse ("Xin lỗi, tôi đã gặp vấn đề khi phản hồi. Vui lòng thử lại." — Pod Studio #160, 2/10/2026;
# "the generation service is temporarily unavailable") cũng có «xin lỗi»/«sorry» → phải là lỗi THƯỜNG (gọi lại được),
# không phải từ chối nội dung.
_TRANSIENT_RE = re.compile(r"gặp vấn đề|vui lòng thử lại|đã xảy ra lỗi|try again|temporarily unavailable|"
                           r"something went wrong|an error occurred|ran into a problem|technical (?:issue|problem)", re.I)


def _no_output_kind(said: str) -> str:
    """Muse trả chữ thay cho ảnh/video → 'refused' chỉ khi là lời từ chối nội dung; lỗi hệ thống hay tán chuyện → 'error'."""
    if _TRANSIENT_RE.search(said or ""):
        return "error"
    return "refused" if _REFUSAL_RE.search(said or "") else "error"


def _to_jpeg(data: bytes) -> bytes:
    """webp của Muse → JPEG: khâu dựng video (canvas Node, ffmpeg cũ) và tên file .jpg của Studio đều
    chắc ăn với JPEG. Không có PIL thì trả nguyên."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            if im.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                rgba = im.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                im2 = bg
            else:
                im2 = im.convert("RGB")
            buf = io.BytesIO()
            im2.save(buf, "JPEG", quality=93)
            return buf.getvalue()
    except Exception as e:      # noqa: BLE001
        logger.info("muse: keeping the original image bytes (%s)", e)
        return data


def generate_image_bytes(prompt: str, aspect_ratio: str = "16:9", reference_images: Optional[list] = None,
                         timeout: int = IMAGE_TIMEOUT) -> bytes:
    """Bytes JPEG của MỘT ảnh Muse vẽ. Ném MuseError (kind refused khi Muse trả lời bằng chữ)."""
    refs = [p for p in (reference_images or []) if p and os.path.isfile(str(p))][:3]
    with tempfile.TemporaryDirectory(prefix="muse_img_") as tmp:
        res = ask(image_request(prompt, aspect_ratio, bool(refs)), want_images=True, files=refs,
                  image_dir=tmp, max_images=1, timeout=timeout)
        imgs = [i for i in (res.get("images") or []) if isinstance(i, dict) and i.get("path")]
        if not imgs:
            said = " ".join(str(res.get("text") or "").split())[:240]
            raise MuseError(_no_output_kind(said), f"Muse did not draw an image{': ' + said if said else '.'}")
        with open(imgs[0]["path"], "rb") as f:
            data = f.read()
    return _to_jpeg(data)


# ── video ─────────────────────────────────────────────────────────────────────
# Đo 2/10/2026: image→video 9:16 → 704×1104, 10 s cố định, h264 + aac, ~6 MB, ~90 s. Muse không nhận độ dài khác.
VIDEO_TIMEOUT = 600


def video_request(prompt: str, aspect_ratio: str = "9:16", continue_from: bool = False) -> str:
    """Lời xin MỘT clip 10 s. continue_from: ảnh đính kèm ĐẦU là khung cuối của clip trước → khung đầu phải trùng."""
    ar = aspect_ratio if aspect_ratio in ASPECTS else "9:16"
    lines = [
        "Create exactly ONE 10-second video now. Do not ask questions, do not explain, and do not write anything in "
        "your reply — reply with the video only.",
        f"Aspect ratio: {ar} ({ASPECTS[ar]}).",
        "No text, captions, logos or watermarks inside the video.",
    ]
    if continue_from:
        lines.append("The FIRST attached image is the final frame of the previous shot: the video must START from "
                     "exactly that frame (same person, pose, framing, lighting and background) and continue the "
                     "action seamlessly. The other attached image(s) are the character's reference portrait: the "
                     "person must stay the SAME individual as in that portrait — same face shape, eyes, hair color "
                     "and style, skin tone — throughout the whole video. Keep the outfit identical.")
    else:
        lines.append("Use the attached image(s) as the reference for the person, outfit and setting — keep the face, "
                     "hair and outfit identical.")
    lines.append("")
    lines.append("Shot description:")
    lines.append(str(prompt or "").strip())
    return "\n".join(lines)


def generate_video_clip(prompt: str, out_dir: str, reference_images: Optional[list] = None,
                        aspect_ratio: str = "9:16", continue_from: bool = False, thread_id: str = "",
                        timeout: int = VIDEO_TIMEOUT) -> dict:
    """MỘT clip Muse → {path, poster, width, height, duration, thread_id}. Ném MuseError."""
    refs = [p for p in (reference_images or []) if p and os.path.isfile(str(p))][:3]
    os.makedirs(out_dir, exist_ok=True)
    res = ask(video_request(prompt, aspect_ratio, continue_from), want_videos=True, files=refs, video_dir=out_dir,
              max_videos=1, timeout=timeout, thread_id=thread_id)
    vids = [v for v in (res.get("videos") or []) if isinstance(v, dict) and v.get("path")]
    if not vids:
        said = " ".join(str(res.get("text") or "").split())[:240]
        raise MuseError(_no_output_kind(said), f"Muse did not make a video{': ' + said if said else '.'}")
    return {**vids[0], "thread_id": res.get("thread_id", "")}


def test_chat(timeout: int = 90) -> dict:
    """Gọi thử MỘT câu ngắn: {ok, seconds, reply|message, kind}."""
    t0 = time.time()
    try:
        text = chat_completion([{"role": "user", "content": "Reply with the single word OK."}], timeout=timeout)
        return {"ok": True, "seconds": round(time.time() - t0, 1), "reply": text[:80]}
    except MuseError as e:
        return {"ok": False, "seconds": round(time.time() - t0, 1), "message": str(e), "kind": e.kind}
