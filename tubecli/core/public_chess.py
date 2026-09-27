"""Skill công khai «đánh cờ vua với agent» (user 27/9/2026, chốt hướng B: bàn cờ trực
quan trên Town, đánh như cờ online).

Phân vai — CLOUD LÀ TRỌNG TÀI, máy chỉ là một KỲ THỦ:
  · Cloud giữ bàn cờ (D1) và kiểm luật MỌI nước bằng chess.js — kể cả nước máy trả.
  · Mỗi lượt gọi = MỘT nước đi của agent: đầu vào là JSON {fen, turn, history} do
    CHÍNH CLOUD dựng (không phải chữ người lạ gõ — cùng neo với mã video YouTube);
    đầu ra chỉ có {move: UCI, chat ngắn}. Máy bị chiếm cũng chỉ nói được một nước cờ.
  · Nước đi lấy qua LOOPBACK /api/v1/arena/play-turn của extension AI Arena (external,
    không import module được — cùng cách capcut.tts gọi synthesize): AIPlayer hỏi model
    của agent, tự kiểm luật bằng python-chess và thử lại tới 5 lần trước khi trả.

Agent nào trả lời = agent gắn hồ sơ công khai: core/public_agents.invoke tiêm
opts['_agent_id'] (id agent Flow) trước khi gọi handler — play-turn cần nó để lấy model.
"""
from __future__ import annotations

import asyncio
import json
import re
from functools import partial
from typing import Any, Dict, Optional

from tubecli.core.public_agents import PublicSkillError

CHESS_TIMEOUT_SEC = 34          # trong trần 40 s của invoke; model nghĩ một nước vài giây
_FEN_RE = re.compile(r"^[rnbqkpRNBQKP1-8]+(/[rnbqkpRNBQKP1-8]+){7} [wb] (K?Q?k?q?|-) ([a-h][36]|-) \d{1,3} \d{1,4}$")
_UCI_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")
_SAN_RE = re.compile(r"^[a-h1-8xKQRBNO=+#\-]{2,10}$")


def _base() -> str:
    from tubecli.config import get_api_port

    return f"http://127.0.0.1:{get_api_port()}"


def _play_turn_blocking(agent_id: str, fen: str, turn: str, history: list) -> Dict[str, Any]:
    import requests

    body = {
        "agent_id": agent_id,
        "game_id": "chess",
        "prompt": "",
        "game_state": {"board": fen, "fen": fen, "current_turn": turn, "move_history": history},
    }
    try:
        r = requests.post(_base() + "/api/v1/arena/play-turn", json=body, timeout=CHESS_TIMEOUT_SEC - 2)
    except requests.RequestException:
        raise PublicSkillError("chess_failed", status=502)
    if r.status_code == 404:
        # Extension AI Arena chưa bật / agent không còn — với người lạ đều là «không chơi được».
        raise PublicSkillError("skill_unavailable", status=503)
    if r.status_code >= 400:
        raise PublicSkillError("chess_failed", status=502)
    try:
        data = r.json() or {}
    except ValueError:
        raise PublicSkillError("chess_failed", status=502)
    return data


async def resolve(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # Đầu vào do CLOUD dựng — vẫn kiểm chặt như thể người lạ gõ, vì chữ ký chỉ chứng
    # minh «từ cloud tới», không chứng minh «cloud không bị lừa».
    try:
        req = json.loads(str(text or ""))
    except ValueError:
        raise PublicSkillError("bad_input")
    fen = str((req or {}).get("fen") or "")
    turn = str((req or {}).get("turn") or "")
    history = (req or {}).get("history") or []
    if not _FEN_RE.match(fen) or turn not in ("white", "black") or not isinstance(history, list):
        raise PublicSkillError("bad_input")
    history = [h for h in history if isinstance(h, str) and _SAN_RE.match(h)][:24]

    agent_id = str((opts or {}).get("_agent_id") or "")
    if not agent_id:
        raise PublicSkillError("skill_unavailable", status=503)

    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(partial(_play_turn_blocking, agent_id, fen, turn, history)),
            timeout=CHESS_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        raise PublicSkillError("timeout", status=504)

    mv = data.get("move") or {}
    move = str(mv.get("move") or "").strip().lower()
    if not _UCI_RE.match(move):
        raise PublicSkillError("chess_failed", status=502)
    chat = str(mv.get("chat_message") or "")[:200]
    return {"kind": "chessmove", "move": move, "chat": chat}
