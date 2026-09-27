"""Skill công khai «đánh cờ tướng với agent» (user 27/9/2026 — nối tiếp cờ vua).

Khác cờ vua một điểm then chốt: máy KHÔNG có engine cờ tướng (AI Arena chỉ có chess +
snake), nên CLOUD — vốn đã là trọng tài — gửi kèm luôn DANH SÁCH NƯỚC HỢP LỆ. Prompt bảo
model «chọn một nước trong danh sách», máy đối chiếu câu trả lời với danh sách rồi mới
trả về; cloud còn kiểm luật lần nữa. Model không cần biết luật cờ tướng vẫn đi đúng.

Đường gọi model: loopback /api/v1/arena/play-turn với game_id 'xiangqi' — Arena không có
engine cho game này nên rơi vào nhánh «raw prompt»: AIPlayer gọi model của agent (đã vá
9Router qua core/ninerouter) và bóc JSON {move, chat} từ câu trả lời hộ mình.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from functools import partial
from typing import Any, Dict, Optional

from tubecli.core.public_agents import PublicSkillError

XQ_BUDGET_SEC = 34             # trong trần 40 s của invoke
_FEN_RE = re.compile(r"^[rnbakcpRNBAKCP1-9]+(/[rnbakcpRNBAKCP1-9]+){9} [wb]( .*)?$")
_MOVE_RE = re.compile(r"^[a-i][0-9][a-i][0-9]$")


def _base() -> str:
    from tubecli.config import get_api_port

    return f"http://127.0.0.1:{get_api_port()}"


def _prompt(fen: str, side: str, history: list, legal: list) -> str:
    # Prompt tiếng Anh (chuẩn của dự án); model chỉ được CHỌN trong danh sách.
    hist = " ".join(history[-16:]) or "(game start)"
    return (
        "You are playing Chinese Chess (Xiangqi) as the "
        + ("RED" if side == "w" else "BLACK")
        + " side.\n"
        f"Position (FEN): {fen}\n"
        f"Recent moves (from-to squares, files a-i, ranks 0-9): {hist}\n"
        "ALL LEGAL MOVES for you right now: " + " ".join(legal) + "\n"
        "Pick the strongest move. You MUST pick exactly one move from the legal list above.\n"
        'Respond with ONLY a JSON object: {"move": "<one move from the list>", '
        '"chat": "<one short strategic comment, max 120 chars>"}'
    )


def _play_turn_blocking(agent_id: str, prompt: str, fen: str, turn: str, history: list,
                        timeout: float) -> Dict[str, Any]:
    import requests

    body = {
        "agent_id": agent_id,
        "game_id": "xiangqi",
        "prompt": prompt,
        "game_state": {"fen": fen, "board": fen, "current_turn": turn, "move_history": history},
    }
    try:
        r = requests.post(_base() + "/api/v1/arena/play-turn", json=body, timeout=timeout)
    except requests.RequestException:
        raise PublicSkillError("chess_failed", status=502)
    if r.status_code == 404:
        raise PublicSkillError("skill_unavailable", status=503)
    if r.status_code >= 400:
        raise PublicSkillError("chess_failed", status=502)
    try:
        return r.json() or {}
    except ValueError:
        raise PublicSkillError("chess_failed", status=502)


async def resolve(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        req = json.loads(str(text or ""))
    except ValueError:
        raise PublicSkillError("bad_input")
    fen = str((req or {}).get("fen") or "")
    turn = str((req or {}).get("turn") or "")
    history = (req or {}).get("history") or []
    legal = (req or {}).get("legal") or []
    if not _FEN_RE.match(fen) or turn not in ("w", "b") or not isinstance(legal, list):
        raise PublicSkillError("bad_input")
    history = [h for h in history if isinstance(h, str) and _MOVE_RE.match(h)][:24]
    legal = [m for m in legal if isinstance(m, str) and _MOVE_RE.match(m)][:140]
    if not legal:
        raise PublicSkillError("bad_input")

    agent_id = str((opts or {}).get("_agent_id") or "")
    if not agent_id:
        raise PublicSkillError("skill_unavailable", status=503)

    allowed = set(legal)
    started = time.monotonic()
    prompt = _prompt(fen, turn, history, legal)
    last_chat = ""
    for attempt in range(3):
        left = XQ_BUDGET_SEC - (time.monotonic() - started)
        if left < 4:
            break
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(partial(
                    _play_turn_blocking, agent_id, prompt, fen, turn, history, min(left - 1, 30))),
                timeout=left,
            )
        except asyncio.TimeoutError:
            raise PublicSkillError("timeout", status=504)
        mv = data.get("move") or {}
        move = str(mv.get("move") or "").strip().lower().replace("-", "")
        last_chat = str(mv.get("chat_message") or mv.get("chat") or "")[:200]
        if move in allowed:
            return {"kind": "xqmove", "move": move, "chat": last_chat}
        # Model chọn ngoài danh sách: nhắc thẳng rồi cho thêm một lượt.
        prompt += f'\n\n[SYSTEM: "{move[:20]}" is NOT in the legal list. Pick one move from the list.]'
    raise PublicSkillError("chess_failed", status=502)
