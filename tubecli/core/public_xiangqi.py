"""Skill công khai «đánh cờ tướng với agent» — BA MỨC ĐỘ KHÓ (user chốt 27/9/2026:
«làm dễ vừa và khó đi, dễ thì cho AI đánh cho vui»).

  · easy   — model tự chọn trong danh sách nước hợp lệ (đánh cho vui, hay đi nước ngộ).
  · medium — Fairy-Stockfish nghĩ 4 lớp, lấy các nước kém nước tốt nhất ≤ 1.5 tốt (điểm
               THẬT qua MultiPV), model chọn một (thứ tự xáo) + bình luận.
  · hard   — Fairy-Stockfish sức mạnh đầy đủ 1.5 s/nước, model chỉ viết MỘT câu bình luận.

Engine: core/xq_engine.py (Fairy-Stockfish XQ, tự tải lần đầu). Máy không chạy được nó
(ARM/macOS, tải hỏng) → lùi về engine alpha-beta thuần Python của Arena (/xiangqi/best) —
28/9 đo: engine đó dừng ở lớp 5 cho cả hai mức nên Vừa và Khó từng đánh y hệt nhau.
Cloud vẫn là trọng tài cuối: mọi nước trả về đều phải nằm trong danh sách hợp lệ gửi kèm.

Đường gọi: loopback vào extension AI Arena (external — không import module được):
  /api/v1/arena/xiangqi/best  → top nước theo engine;
  /api/v1/arena/play-turn      → hỏi model (engine-branch cho easy; raw-prompt cho
                                 lượt «chọn trong top-5» và lượt «viết bình luận»).
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from tubecli.core.public_agents import PublicSkillError

XQ_BUDGET_SEC = 34             # trong trần 40 s của invoke
LEVELS = ("easy", "medium", "hard")
HARD_MS = 1500                 # Fairy-Stockfish nghĩ 1.5 s ≈ độ sâu 19 — mạnh hơn người rất xa
MEDIUM_DEPTH = 4               # Vừa: nghĩ 4 lớp…
MEDIUM_MARGIN = 150            # …chọn trong các nước kém nước tốt nhất ≤ 1.5 tốt
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


_PIECE_NAMES = {"r": "chariot", "n": "horse", "b": "elephant", "a": "advisor", "k": "general",
                "c": "cannon", "p": "soldier"}


def describe_move(fen: str, move: str) -> str:
    """«your cannon h7→h5, capturing a soldier» — model chỉ thấy FEN thì hay tả nhầm quân
    (28/9: pháo đi mà bình «đẩy tốt»)."""
    rows = fen.split(" ")[0].split("/")          # hàng 9 (trên cùng) → hàng 0

    def at(sq: str) -> str:
        x, y = "abcdefghi".index(sq[0]), int(sq[1])
        col = 0
        for ch in rows[9 - y]:
            if ch.isdigit():
                col += int(ch)
            else:
                if col == x:
                    return ch
                col += 1
        return ""
    try:
        me, cap = at(move[:2]), at(move[2:])
    except (ValueError, IndexError):
        return "a move"
    s = f"your {_PIECE_NAMES.get(me.lower(), 'piece')} {move[:2]}→{move[2:]}"
    return s + (f", capturing a {_PIECE_NAMES.get(cap.lower(), 'piece')}" if cap else "")


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
    # Fairy-Stockfish (core/xq_engine.py) khi máy có; lần đầu tự tải ~24 MB — chờ tối đa
    # 20 s, quá thì lượt này lùi về engine Python của Arena (lượt sau đã có engine thật).
    from tubecli.core import xq_engine

    top: List[Tuple[str, int]] = []
    if await asyncio.to_thread(xq_engine.ensure, max(0.0, min(left() - 14, 20))):
        if level == "hard":
            # Khó: sức mạnh ĐẦY ĐỦ, nghĩ 1.5 s (đo: độ sâu ~19)
            args = dict(multipv=1, movetime_ms=HARD_MS, timeout=6)
        else:
            # Vừa: nghĩ nông (lớp MEDIUM_DEPTH) nhưng điểm THẬT cho 5 nước (MultiPV) — model
            # chọn trong các nước kém nước tốt nhất ≤ MEDIUM_MARGIN, nên đánh được mà có sơ hở
            args = dict(multipv=5, depth=MEDIUM_DEPTH, timeout=6)
        try:
            top = await asyncio.wait_for(asyncio.to_thread(partial(xq_engine.analyse, fen, **args)),
                                         timeout=min(left(), 10))
        except asyncio.TimeoutError:
            top = []
        top = [(m, s) for m, s in top if m in allowed]
        if level == "medium" and top:
            top = [(m, s) for m, s in top if s >= top[0][1] - MEDIUM_MARGIN]
    if not top:
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
                f"{fen}\nyou just decided to play the move {move} (from-to squares) — "
                f"{describe_move(fen, move)}.\n"
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

    # medium: model chọn MỘT trong các nước ứng viên + bình luận. Xáo thứ tự và KHÔNG ghi
    # «best first»: bản cũ ghi vậy nên model luôn chọn nước đầu = y hệt mức Khó.
    cand = [m for m, _ in top]
    shown = random.sample(cand, len(cand))
    prompt = (
        "You are playing Chinese Chess (Xiangqi) as the "
        + ("RED" if turn == "w" else "BLACK") + " side.\n"
        f"Position (FEN): {fen}\n"
        f"Recent moves: {' '.join(history[-16:]) or '(game start)'}\n"
        "Your engine assistant says these moves are all playable (in no particular order): "
        + " ".join(shown) + "\n"
        "Pick ONE of them that fits your own style and plan.\n"
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
    # Model lề mề/nói bậy: bốc một nước trong nhóm ứng viên (đều đánh được) — không «đi bừa»,
    # cũng không lặng lẽ biến thành mức Khó.
    return {"kind": "xqmove", "move": random.choice(cand), "chat": "", "level": level}
