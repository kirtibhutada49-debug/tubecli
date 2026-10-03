"""JSON-RPC với `codex app-server` qua stdio.

Vì sao luồng đọc + subprocess.Popen thay vì asyncio subprocess: uvicorn trên Windows có lúc chạy
SelectorEventLoop, mà asyncio subprocess ở đó ném NotImplementedError. Luồng đọc chuyển từng dòng
về event loop bằng call_soon_threadsafe — chạy được dưới mọi loop.

Giao thức (đo trên codex-cli 0.160, 3/10/2026): mỗi dòng một JSON. Có `id` + `result|error` = trả lời
yêu cầu của mình; có `method` + `id` = SERVER HỎI (duyệt lệnh, duyệt sửa file…) — phải trả lời đúng
id; có `method` không `id` = thông báo (chữ chạy, item, turn…).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from collections import deque
from typing import Any, Awaitable, Callable, Dict, List, Optional

# Biến môi trường KHÔNG được lọt sang Codex: có OPENAI_API_KEY trong env của TubeCLI thì Codex dùng
# nó thay vì auth.json của tài khoản đang chọn — cả trò đổi tài khoản thành vô nghĩa.
_STRIP_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME")


class RpcError(Exception):
    def __init__(self, message: str, code: int = -32000, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


Notify = Callable[[str, Dict[str, Any]], Awaitable[None]]
Request = Callable[[Dict[str, Any]], Awaitable[None]]


class AppServer:
    """Một tiến trình `codex app-server` với CODEX_HOME riêng."""

    def __init__(self, cmd: List[str], codex_home: str, cwd: str, *, name: str = "main",
                 on_notify: Optional[Notify] = None, on_request: Optional[Request] = None,
                 on_exit: Optional[Callable[[int], Awaitable[None]]] = None):
        self.cmd = list(cmd)
        self.codex_home = codex_home
        self.cwd = cwd
        self.name = name
        self.on_notify = on_notify
        self.on_request = on_request
        self.on_exit = on_exit
        self.proc: Optional[subprocess.Popen] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next = 0
        self._wlock = threading.Lock()
        self.stderr_tail: deque = deque(maxlen=60)
        self.init_info: Dict[str, Any] = {}

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    async def start(self, timeout: float = 60.0) -> Dict[str, Any]:
        self.loop = asyncio.get_running_loop()
        os.makedirs(self.codex_home, exist_ok=True)
        os.makedirs(self.cwd, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env["CODEX_HOME"] = self.codex_home
        env.setdefault("NO_COLOR", "1")
        kw: Dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["start_new_session"] = True
        self.proc = subprocess.Popen(self.cmd + ["app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=self.cwd, env=env, **kw)
        threading.Thread(target=self._read_stdout, daemon=True, name=f"codex-{self.name}-out").start()
        threading.Thread(target=self._read_stderr, daemon=True, name=f"codex-{self.name}-err").start()
        self.init_info = await self.request("initialize", {
            "clientInfo": {"name": "tubecli", "title": "TubeCLI Codex GPT", "version": _tubecli_version()}},
            timeout=timeout)
        await self.notify("initialized")
        return self.init_info

    # ── gửi ──────────────────────────────────────────────────────────────────
    def _write(self, msg: Dict[str, Any]) -> None:
        if not self.alive:
            raise RpcError("codex app-server is not running", -32001)
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._wlock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise RpcError(f"codex app-server pipe closed: {e}", -32001)

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 120.0) -> Any:
        self._next += 1
        rid = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RpcError(f"{method}: no answer from codex in {int(timeout)} s", -32002)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    async def respond(self, rid: Any, result: Any = None, error: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        self._write(msg)

    # ── nhận ─────────────────────────────────────────────────────────────────
    def _read_stdout(self) -> None:
        proc = self.proc
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.stderr_tail.append(line[:300])
                    continue
                if self.loop is not None and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self._dispatch, msg)
        except Exception:      # noqa: BLE001 — ống đóng giữa chừng
            pass
        code = proc.wait()
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._on_closed, code)

    def _read_stderr(self) -> None:
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                s = raw.decode("utf-8", errors="replace").rstrip()
                if s:
                    self.stderr_tail.append(s[:400])
        except Exception:      # noqa: BLE001
            pass

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        if method is None and "id" in msg:
            fut = self._pending.get(msg.get("id"))
            if fut is not None and not fut.done():
                if "error" in msg and msg["error"] is not None:
                    e = msg["error"] or {}
                    fut.set_exception(RpcError(str(e.get("message") or "codex error"), int(e.get("code") or -32000), e.get("data")))
                else:
                    fut.set_result(msg.get("result"))
            return
        if method and "id" in msg:
            if self.on_request is not None:
                asyncio.ensure_future(self.on_request(msg))
            else:
                asyncio.ensure_future(self.respond(msg["id"], error={"code": -32601, "message": "not supported"}))
            return
        if method and self.on_notify is not None:
            asyncio.ensure_future(self.on_notify(method, msg.get("params") or {}))

    def _on_closed(self, code: int) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RpcError(self.exit_reason(code), -32001))
        self._pending.clear()
        if self.on_exit is not None:
            asyncio.ensure_future(self.on_exit(code))

    def exit_reason(self, code: Optional[int] = None) -> str:
        tail = " | ".join(list(self.stderr_tail)[-3:])
        return f"codex app-server exited ({code})" + (f": {tail}" if tail else "")

    async def stop(self, grace: float = 3.0) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
        except Exception:      # noqa: BLE001
            pass
        try:
            await asyncio.get_running_loop().run_in_executor(None, proc.wait, grace)
        except Exception:      # noqa: BLE001
            pass
        if proc.poll() is None:
            _kill_tree(proc)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Codex đẻ tiến trình con (lệnh shell của agent) — giết cả cây, không để mồ côi."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:      # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _tubecli_version() -> str:
    try:
        from tubecli import __version__
        return str(__version__)
    except Exception:      # noqa: BLE001
        return "0"
