"""Skill công khai «đánh cờ tướng với agent» — BA MỨC ĐỘ KHÓ (user chốt 27/9/2026:
«làm dễ vừa và khó đi, dễ thì cho AI đánh cho vui»).

  · easy   — model tự chọn trong danh sách nước hợp lệ (đánh cho vui, hay đi nước ngộ).
  · medium — engine alpha-beta của Arena đưa TOP-5, model chọn một + bình luận.
  · hard   — engine đánh thẳng nước mạnh nhất, model chỉ được nhờ viết MỘT câu bình luận.

Sức cờ nằm ở engine (arena /xiangqi/best — alpha-beta thuần Python, không cài gì thêm),
model chỉ còn vai «giọng nói». Cloud vẫn là trọng tài cuối: mọi nước trả về đều phải nằm
trong danh sách hợp lệ cloud gửi kèm.

Đường gọi: loopback vào extension AI Arena (external — không import module được):
  /api/v1/arena/xiangqi/best  → top nước theo engine;
  /api/v1/arena/play-turn      → hỏi model (engine-branch cho easy; raw-prompt cho
                                 lượt «chọn trong top-5» và lượt «viết bình luận»).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from tubecli.core.public_agents import PublicSkillError

XQ_BUDGET_SEC = 34             # trong trần 40 s của invoke
LEVELS = ("easy", "medium", "hard")
_FEN_RE = re.compile(r"^[rnbakcpRNBAKCP1-9]+(/[rnbakcpRNBAKCP1-9]+){9} [wb]( .*)?$")
_MOVE_RE = re.compile(r"^[a-i][0-9][a-i][0-9]$")


def _base() -> str:
    from tubecli.config import get_api_port

    return f"http://127.0.0.1:{get_api_port()}"


def _post_blocking(path: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    import requests

    try:
        r = requests.post(_base() + path, json=body, timeout=timeout)
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


def _engine_top(fen: str, top: int, ms: int, timeout: float) -> List[Tuple[str, int]]:
    data = _post_blocking("/api/v1/arena/xiangqi/best", {"fen": fen, "top": top, "ms": ms}, timeout)
    out = []
    for it in data.get("moves") or []:
        m = str((it or {}).get("move") or "").lower()
        if _MOVE_RE.match(m):
            out.append((m, int((it or {}).get("score") or 0)))
    return out


def _play_turn(agent_id: str, game_id: str, prompt: str, fen: str, turn: str,
               history: list, timeout: float) -> Dict[str, Any]:
    return _post_blocking("/api/v1/arena/play-turn", {
        "agent_id": agent_id,
        "game_id": game_id,
        "prompt": prompt,
        "game_state": {"fen": fen, "board": fen, "current_turn": turn, "move_history": history},
    }, timeout)


def _mv(data: Dict[str, Any]) -> Tuple[str, str]:
    mv = data.get("move") or {}
    move = str(mv.get("move") or "").strip().lower().replace("-", "")
    chat = str(mv.get("chat_message") or mv.get("chat") or "")[:200]
    return move, chat


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
    level = str((opts or {}).get("level") or "medium")
    if level not in LEVELS:
        level = "medium"

    allowed = set(legal)
    started = time.monotonic()

    def left() -> float:
        return XQ_BUDGET_SEC - (time.monotonic() - started)

    # ── DỄ: model tự bơi trong danh sách hợp lệ — «đánh cho vui» ──────────────
    if level == "easy":
        prompt = (
            "You are playing Chinese Chess (Xiangqi) as the "
            + ("RED" if turn == "w" else "BLACK") + " side.\n"
            f"Position (FEN): {fen}\n"
            f"Recent moves: {' '.join(history[-16:]) or '(game start)'}\n"
            "ALL LEGAL MOVES for you right now: " + " ".join(legal) + "\n"
            "Pick the strongest move. You MUST pick exactly one move from the legal list above.\n"
            'Respond with ONLY a JSON object: {"move": "<one move from the list>", '
            '"chat": "<one short strategic comment, max 120 chars>"}'
        )
        for _ in range(3):
            if left() < 4:
                break
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(partial(_play_turn, agent_id, "xiangqi", prompt, fen, turn,
                                              history, min(left() - 1, 30))),
                    timeout=left())
            except asyncio.TimeoutError:
                raise PublicSkillError("timeout", status=504)
            move, chat = _mv(data)
            if move in allowed:
                return {"kind": "xqmove", "move": move, "chat": chat, "level": level}
            prompt += f'\n\n[SYSTEM: "{move[:20]}" is NOT in the legal list. Pick one move from the list.]'
        raise PublicSkillError("chess_failed", status=502)

    # ── VỪA / KHÓ: engine tính trước ──────────────────────────────────────────
    ms = 2800 if level == "hard" else 2200
    try:
        top = await asyncio.wait_for(
            asyncio.to_thread(partial(_engine_top, fen, 1 if level == "hard" else 5, ms, 10)),
            timeout=min(left(), 12))
    except asyncio.TimeoutError:
        raise PublicSkillError("timeout", status=504)
    top = [(m, s) for m, s in top if m in allowed]
    if not top:
        raise PublicSkillError("chess_failed", status=502)

    if level == "hard":
        # Engine đánh; model chỉ được nhờ MỘT câu bình luận — hỏng thì im lặng đi tiếp.
        move = top[0][0]
        chat = ""
        if left() > 6:
            say_prompt = (
                "You are a Xiangqi (Chinese Chess) player. In this position (FEN): "
                f"{fen}\nyou just decided to play the move {move} (from-to squares).\n"
                'Respond with ONLY a JSON object: {"chat": "<one short confident comment '
                'about this move, max 120 chars>"}'
            )
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(partial(_play_turn, agent_id, "xiangqi_say", say_prompt,
                                              fen, turn, history, min(left() - 1, 15))),
                    timeout=min(left(), 16))
                chat = _mv(data)[1]
            except (asyncio.TimeoutError, PublicSkillError):
                chat = ""
        return {"kind": "xqmove", "move": move, "chat": chat, "level": level}

    # medium: model chọn MỘT trong top-5 của engine + bình luận.
    cand = [m for m, _ in top]
    prompt = (
        "You are playing Chinese Chess (Xiangqi) as the "
        + ("RED" if turn == "w" else "BLACK") + " side.\n"
        f"Position (FEN): {fen}\n"
        f"Recent moves: {' '.join(history[-16:]) or '(game start)'}\n"
        "Your engine assistant computed the 5 STRONGEST candidate moves (best first): "
        + " ".join(cand) + "\n"
        "Pick ONE of these candidate moves only.\n"
        'Respond with ONLY a JSON object: {"move": "<one of the candidates>", '
        '"chat": "<one short strategic comment, max 120 chars>"}'
    )
    cand_set = set(cand)
    for _ in range(2):
        if left() < 4:
            break
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(partial(_play_turn, agent_id, "xiangqi_pick", prompt, fen, turn,
                                          history, min(left() - 1, 20))),
                timeout=left())
        except asyncio.TimeoutError:
            break
        except PublicSkillError:
            break
        move, chat = _mv(data)
        if move in cand_set:
            return {"kind": "xqmove", "move": move, "chat": chat, "level": level}
        prompt += f'\n\n[SYSTEM: "{move[:20]}" is not one of the candidates. Pick one candidate.]'
    # Model lề mề/nói bậy: engine tự đi nước tốt nhất, không bao giờ «đi bừa».
    return {"kind": "xqmove", "move": cand[0], "chat": "", "level": level}
