# -*- coding: utf-8 -*-
"""Hộp «Chạy lại» chọn được Google Drive + cấp quyền lại khi lỗi xác thực (2/10/2026).

User: «retry thiếu chọn drive, khi lỗi auth không có chỗ để auth để cấp quyền lại» + «chỗ tạo task có chọn auth
cấp quyền drive đó, bạn xem». Cam kết:
  1. retry_info trả khối `drive`: đang bật không, tài khoản nào, quyền thư mục, thư mục lần trước, lỗi lần trước và
     lỗi ấy có phải lỗi QUYỀN (cấp quyền lại mới hết) hay không.
  2. retry_with ghi lựa chọn Drive THẲNG vào options của payload mới; tài khoản phải còn trong Auth Manager và ghi được
     Drive; tắt Drive thì không đòi tài khoản; không đổi gì thì không ghi event.
  3. Tài khoản người bấm chọn (drive_user_token) qua được luật «chỉ tài khoản đã cấp cho agent» — task do AI/lịch
     tạo vẫn chạy lại được bằng tài khoản người dùng chọn; tài khoản khác thì luật vẫn chặn.
  4. Cấp quyền lại sinh token MỚI cho cùng email → lượt sau dùng lại ĐÚNG thư mục cũ.
  5. Giao diện: khối Drive trong hộp, nút «Cấp quyền lại» gọi luồng OAuth của Auth Manager (phạm vi Drive + Sheets),
     cửa sổ mở trong lượt bấm, tự nhận token mới; đủ 9 ngôn ngữ.
Mọi HTTP, codex_manager và Auth Manager đều giả — không gọi Google.
"""
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from tubecli.extensions.content_video import pipeline as P      # noqa: E402
from tubecli.extensions.content_video import drive_export as DX  # noqa: E402
import tubecli.extensions.codex.manager as CM                   # noqa: E402
import tubecli.core.agent as _agmod                             # noqa: E402

PASS = FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  ", label)
    else:
        FAIL += 1
        print("  FAIL", label, "—", str(detail)[:300])


print("── 1. nhận ra lỗi QUYỀN ──")
for txt in ("Saving to Google Drive failed: The Google token has expired and could not be refreshed — authorize the "
            "account again in Auth Manager.",
            "Saving to Google Drive failed: Access for a@b.com was revoked — authorize it again in Auth Manager.",
            "Saving to Google Drive failed: ('invalid_grant: Token has been expired or revoked.', {...})",
            "Saving to Google Drive failed: a@b.com can only read Google Drive — authorize it again",
            "Saving to Google Drive failed: <HttpError 403 … insufficientPermissions>"):
    ok(P.drive_auth_problem(txt), f"lỗi quyền: {txt[29:80]}")
for txt in ("Saving to Google Drive failed: <HttpError 500 … Internal Error>", "Render failed: ffmpeg exited 1", ""):
    ok(not P.drive_auth_problem(txt), f"không phải lỗi quyền: {txt[:60]!r}")


class _Agent:
    id, name, model = "ag1", "Chuyên gia", "ag/gemini-3.8-flash"
    system_prompt = "[[AUTH-ACCESS-GUIDE]] creds: cred_a"

    def to_dict(self):
        return {"id": self.id, "model": self.model}


ERR = ("Saving to Google Drive failed: The Google token has expired and could not be refreshed — authorize the "
       "account again in Auth Manager. — the video is rendered and kept at `x.mp4`; Retry uploads only what is missing.")
TASKS = {"t1": {"id": "t1", "seq": 27, "status": "failed", "assignee_id": "ag1", "error": ERR, "created_by": "agent"},
         "t2": {"id": "t2", "seq": 28, "status": "failed", "assignee_id": "ag1", "error": "Render failed: boom"}}
KINDS = {"t1": P.KIND_AUTO, "t2": P.KIND_AUTO}
EVENTS = {"t1": [{"data": {"kind": P.KIND_AUTO, "task_id": "t1", "agent_id": "ag1",
                           "options": {"preset": "X", "drive": True, "drive_token_id": "tok_old", "drive_public": True}}}],
          "t2": [{"data": {"kind": P.KIND_AUTO, "task_id": "t2", "agent_id": "ag1", "options": {"preset": "X"}}}]}
ORDER = []
CM.codex_manager.get_task = lambda tid: TASKS.get(tid)
CM.codex_manager.kind_of = lambda tid: KINDS.get(tid)
CM.codex_manager.get_events = lambda tid, limit=1000: list(EVENTS.get(tid, []))
CM.codex_manager.append_event = lambda tid, kind, text, actor=None, data=None: (
    ORDER.append(("append", tid, text)), EVENTS.setdefault(tid, []).append({"data": data or {}}))
CM.codex_manager.retry = lambda tid, actor="user": ORDER.append(("retry", tid)) or {"id": tid, "status": "queued"}
P._read_checkpoint = lambda tid: ({"language": "vi", "episode_id": 9,
                                   "drive": {"token_id": "tok_old", "email": "a@b.com",
                                             "folder_id": "F1", "folder_url": "https://drive.google.com/drive/folders/F1"}}
                                  if tid == "t1" else {"language": "vi"})
P._load_preset = lambda name: {"language": "vi", "metadata": {}}
_agmod.agent_manager.get = lambda aid: _Agent()
TOKENS = [
    {"token_id": "tok_old", "credential_id": "cred_a", "authorized_email": "a@b.com", "status": "expired",
     "scopes": ["drive", "sheets"]},
    {"token_id": "tok_new", "credential_id": "cred_a", "authorized_email": "a@b.com", "status": "active",
     "scopes": ["drive", "sheets"]},
    {"token_id": "tok_other", "credential_id": "cred_b", "authorized_email": "c@d.com", "status": "active",
     "scopes": ["drive"]},
    {"token_id": "tok_ro", "credential_id": "cred_b", "authorized_email": "e@f.com", "status": "active",
     "scopes": ["drive_readonly"]},
]
DX.google_tokens = lambda: [dict(t) for t in TOKENS]

print("── 2. retry_info ──")
dr = P.retry_info("t1")["drive"]
ok(dr["on"] and dr["token_id"] == "tok_old" and dr["public"] is True and dr["email"] == "a@b.com"
   and dr["folder_url"].endswith("/F1"), "khối drive: bật, tài khoản, quyền, thư mục lần trước", dr)
ok(dr["auth_error"] and "expired" in dr["error"] and dr["granted"] == ["cred_a"] and dr["agent"] == "Chuyên gia",
   "lỗi lần trước là lỗi quyền + tài khoản đã cấp cho agent", dr)
TASKS["t3"] = {"id": "t3", "seq": 29, "status": "failed", "assignee_id": "ag1", "error": ERR}
KINDS["t3"] = P.KIND_AUTO
EVENTS["t3"] = [{"data": {"kind": P.KIND_AUTO, "task_id": "t3", "agent_id": "ag1", "options": {"drive": True}}}]
d3 = P.retry_info("t3")["drive"]
ok(d3["token_id"] == "tok_new" and d3["auth_error"],
   "task không chọn tài khoản, hỏng trước khi ghi sổ → hộp biết tài khoản bước Drive sẽ dùng (đã cấp cho agent)", d3)
d2 = P.retry_info("t2")["drive"]
ok(not d2["on"] and not d2["error"] and not d2["auth_error"], "task không lỗi Drive → không báo lỗi Drive", d2)

print("── 3. retry_with ──")
n = len(EVENTS["t1"])
P.retry_with("t1", drive=True, drive_token_id="tok_new", drive_public=False, actor="user:web")
new = EVENTS["t1"][-1]["data"]
ok(len(EVENTS["t1"]) == n + 1 and ORDER[-1] == ("retry", "t1"), "ghi payload mới rồi mới retry")
ok(new["options"]["drive"] is True and new["options"]["drive_token_id"] == "tok_new"
   and new["options"]["drive_public"] is False and new["options"]["preset"] == "X",
   "lựa chọn Drive nằm trong options, phần còn lại giữ nguyên", new["options"])
ok(new.get("drive_user_token") == "tok_new", "ghi tài khoản người bấm chọn", new)
ok("drive=a@b.com" in ORDER[-2][2], "sổ sự kiện nói đổi sang tài khoản nào", ORDER[-2])
n = len(EVENTS["t1"])
P.retry_with("t1", drive=True, drive_token_id="tok_new", drive_public=False)
ok(len(EVENTS["t1"]) == n, "không đổi gì → không ghi event mới")
for bad, why in (("tok_gone", "no longer in Auth Manager"), ("tok_ro", "can only read")):
    try:
        P.retry_with("t1", drive=True, drive_token_id=bad)
        ok(False, f"{bad} phải bị từ chối")
    except ValueError as e:
        ok(why in str(e), f"{bad}: từ chối ngay, nói lý do", e)
P.retry_with("t1", drive=False)
ok(EVENTS["t1"][-1]["data"]["options"]["drive"] is False, "tắt Drive không đòi tài khoản")
n2 = len(EVENTS["t2"])
P.retry_with("t2", drive=None)
ok(len(EVENTS["t2"]) == n2, "Studio cũ (không gửi drive) → không đụng options")

print("── 4. luật tài khoản + thư mục cũ ──")
GOT = {}
_orig_resolve = DX.resolve_token
DX.resolve_token = lambda token_id, granted, strict: GOT.update(tid=token_id, strict=strict) or {
    "token_id": token_id, "authorized_email": "a@b.com"}
P._drive_strict = lambda state: True
src = (ROOT / "tubecli" / "extensions" / "content_video" / "pipeline.py").read_text(encoding="utf-8")
seg = src[src.index("def _drive_save("):src.index("def _drive_save(") + 4000]
ok('strict=_drive_strict(state) and not (want and want == state.get("drive_user_token"))' in seg,
   "tài khoản người bấm chọn qua được luật «chỉ tài khoản đã cấp»; tài khoản khác vẫn bị chặn")
ok('same = rec.get("token_id") == token_id or ("@" in who and str(rec.get("email") or "").lower() == who.lower())' in seg
   and "if rec.get(\"folder_id\") and same" in seg, "cùng email (token mới sau khi cấp quyền lại) → dùng lại thư mục cũ")
prep = src[src.index("def _prepare("):src.index("def _prepare(") + 3500]
ok('"drive_user_token": str(payload.get("drive_user_token") or "")' in prep, "state mang tài khoản người bấm chọn")
DX.resolve_token = _orig_resolve

print("── 5. route + giao diện ──")
routes = (ROOT / "tubecli" / "extensions" / "content_video" / "routes.py").read_text(encoding="utf-8")
ok("drive: Optional[bool] = None" in routes and "req.drive, req.drive_token_id, req.drive_public" in routes,
   "route Chạy lại nhận drive / drive_token_id / drive_public")
html = (ROOT / "tubecli" / "extensions" / "codex" / "static" / "codex.html").read_text(encoding="utf-8")
js = (ROOT / "tubecli" / "extensions" / "codex" / "static" / "codex.js").read_text(encoding="utf-8")
box = html[html.index('id="cx-modal-retry"'):]
ok(all(i in box for i in ('id="cx-rt-drive"', 'id="cx-rt-drive-token"', 'id="cx-rt-drive-share"', 'id="cx-rt-reauth"',
                          'id="cx-rt-drive-alert"')), "hộp Chạy lại có bật Drive, tài khoản, quyền, cấp quyền lại, khung lỗi")
ok("loadGoogleTokens()," in js[js.index("async function openRetry"):js.index("async function openRetry") + 1500],
   "mở hộp là nạp lại tài khoản Google (có thể vừa cấp quyền ở trang Auth)")
rr = js[js.index("async function retryReauth"):js.index("async function retryReauth") + 4000]
ok(rr.index("window.open('about:blank'") < rr.index("await request("), "cửa sổ cấp quyền mở TRONG lượt bấm (trước await)")
ok("/api/v1/auth-manager/credentials/' + encodeURIComponent(tok.credential_id) + '/authorize'" in rr
   and "['drive', 'sheets']" in rr, "cấp quyền lại bằng luồng OAuth của Auth Manager, phạm vi có Drive + Sheets")
ok("!before.has(x.token_id) && x.credential_id === tok.credential_id" in rr and "rb.driveToken = fresh[0].token_id" in rr,
   "tự nhận token MỚI của cùng credential và chọn nó")
sr = js[js.index("async function startRetry"):js.index("async function startRetry") + 2000]
ok("body.drive = !!$('cx-rt-drive').checked" in sr and "body.drive_token_id" in sr and "body.drive_public" in sr,
   "bấm Chạy lại gửi lựa chọn Drive")
ok("onRetryDrive, onRetryDriveToken, retryReauth," in js, "xuất hàm cho nút trong HTML")
LANGS = ["en", "vi", "es", "ja", "ko", "ru", "tr", "zh", "zh-TW"]
loc = {lg: json.load(io.open(ROOT / "tubecli" / "extensions" / "codex" / "locales" / f"{lg}.json", encoding="utf-8"))
       for lg in LANGS}
used = sorted(set(re.findall(r"t\('(codex\.retry_(?:drive|reauth)[a-z_]*)'", js))
              | set(re.findall(r'data-i18n="(codex\.retry_(?:drive|reauth)[a-z_]*)"', html)))
ok(len(used) == 11 and all(k in loc[lg] for lg in LANGS for k in used), "11 câu mới đủ 9 ngôn ngữ", used)
ph = lambda v: sorted(re.findall(r"\{(\w+)\}", v))
ok(all(ph(loc[lg][k]) == ph(loc["en"][k]) for lg in LANGS for k in used), "chỗ giữ {…} khớp tiếng Anh")
ok(all(loc["vi"][k] != loc["en"][k] for k in used), "tiếng Việt dịch thật")

print()
print("=" * 62)
print(f"{PASS}/{PASS + FAIL} PASS" if not FAIL else f"{PASS}/{PASS + FAIL} PASS — {FAIL} HỎNG")
sys.exit(1 if FAIL else 0)
