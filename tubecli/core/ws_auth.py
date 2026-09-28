"""Authentication for WebSocket routes.

HTTP middleware never sees a WebSocket. Starlette dispatches the "websocket"
scope down a separate path, so every `@app.middleware("http")` — including the
login gate in api/server.py and the cross-origin guard beside it — is skipped
entirely. A WebSocket route is therefore wide open unless it checks for itself,
and this project has two of them, both proxying raw traffic to a local port:

    tubecli/extensions/browser/routes.py            /preview/ws/{port}
    tubecli/extensions/browser_scripts/script_routes.py  /preview/ws/{port}

Both take the destination PORT from the URL, so an unauthenticated caller can
use them to reach any TCP service bound to localhost on the machine — which is
where everything private tends to listen.
"""
from __future__ import annotations


def origin_ok(websocket) -> bool:
    """Cùng luật Origin với HTTP (_guard_cross_origin). Trước 28/9/2026 WebSocket KHÔNG qua
    luật này: trang evil.com mở trong BẤT KỲ trình duyệt nào trên máy (browser của agent lướt
    web, browser chia sẻ) tự mở được ws://127.0.0.1:5295/api/v1/terminal/ws, và loopback
    không qua proxy = chủ máy ⇒ shell. Trình duyệt luôn gửi Origin khi bắt tay WS nên không
    né được; client không phải trình duyệt (không Origin) vẫn qua như HTTP.

    Qua tunnel (có header proxy) thì nhận thêm Origin CÙNG SITE với Host — trang Flow trên
    cloud.tubecreate.com mở WS tới <máy>.tubecreate.com — vì đường đó vẫn phải có cookie
    đăng nhập, còn loopback trần thì không; DNS rebinding (Origin=Host=evil) đi loopback trần
    nên không lọt qua nhánh này."""
    origin = websocket.headers.get("origin") or ""
    if not origin:
        return True
    from tubecli.core import auth, origin_guard as og

    if og.is_origin_allowed(origin):
        return True
    if auth.behind_proxy(websocket.headers):
        return og._same_site(og._host_of(origin), og._host_of(websocket.headers.get("host") or ""))
    return False


async def reject_unless_allowed(websocket) -> bool:
    """Close the socket and return False when the caller may not connect.

    Returns True when the handler should proceed. Call this BEFORE
    websocket.accept(): a socket that has already been accepted has, from the
    browser's point of view, succeeded, and closing it afterwards looks like a
    server fault rather than a refusal.
    """
    try:
        from tubecli.core import auth

        if not origin_ok(websocket):
            await websocket.close(code=1008, reason="cross_origin")
            return False
        client_host = websocket.client.host if websocket.client else ""
        cookie = websocket.cookies.get(auth.SESSION_COOKIE)
        refusal = auth.check_request(client_host, cookie, websocket.headers)
        if refusal is None:
            return True

        # 1008 = policy violation. The reason is capped because some clients
        # drop the frame if it exceeds 123 bytes.
        await websocket.close(code=1008, reason=str(refusal.get("reason", "unauthorised"))[:100])
        return False
    except Exception:
        # A broken check must fail CLOSED here. Unlike the HTTP gate, which
        # degrades to "allow" so a bug cannot take the dashboard down, these two
        # routes are a proxy to arbitrary localhost ports — the safe direction
        # is to refuse.
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return False
