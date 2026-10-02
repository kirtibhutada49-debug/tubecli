# -*- coding: utf-8 -*-
"""Bảng việc nhận pipeline + loại việc do EXTENSION khai (codex/pipelines.py, 3/10/2026).

Trước đó executor viết cứng hai nhà (video_studio.*, content_video.*): extension ngoài (Pod Studio) muốn có một
"pipe" trên Bảng việc là phải sửa lõi.

Kiểm:
  A. register_pipeline / find_pipeline (prefix dài nhất thắng) / run_registered (sync chạy trong thread, async await)
  B. register_task_kind: khai sai bị bỏ qua; task_kinds chỉ trả loại có pipeline; dịch theo lang, thiếu → en
  C. step_extension + manager._step_extension (bước lạ → extension đã khai, không phải "codex")
  D. executor._run_registered_pipeline chạy runner đã đăng ký theo kind trong event log
  E. route GET /api/v1/codex/task-kinds
  F. codex.html/js/css có nút + form chung; 7 câu × 9 ngôn ngữ

Run:  python tests/codex_pipelines_test.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from tubecli.extensions.codex import pipelines as P  # noqa: E402

PASS = FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  ", label)
    else:
        FAIL += 1
        print("  FAIL", label, "—", str(detail)[:300])


print("A. sổ pipeline")
P._PIPELINES.clear(); P._KINDS.clear()
calls = []
def runner(kind, payload, report, is_cancelled):
    calls.append((kind, payload)); report("board", "running", label="Bảng"); return "done:" + kind
async def arunner(kind, payload, report, is_cancelled):
    return "async:" + kind
P.register_pipeline("pod_studio.", runner, steps={"board": "pod_studio", "clips": "pod_studio"}, extension="pod_studio")
P.register_pipeline("pod_studio.special.", arunner)
P.register_pipeline("", runner)                       # prefix rỗng: bỏ qua
ok(P.find_pipeline("pod_studio.video")["runner"] is runner, "khớp theo prefix")
ok(P.find_pipeline("pod_studio.special.x")["runner"] is arunner, "prefix dài nhất thắng")
ok(P.find_pipeline("content_video.plan") is None and P.find_pipeline("") is None, "không ai nhận → None")
reports = []
res = asyncio.run(P.run_registered("pod_studio.video", {"a": 1}, lambda *a, **k: reports.append((a, k)), lambda: False))
ok(res == "done:pod_studio.video" and calls[-1][1] == {"a": 1} and reports, "runner đồng bộ chạy (trong thread) và report được", res)
ok(asyncio.run(P.run_registered("pod_studio.special.x", {}, None, None)) == "async:pod_studio.special.x", "runner async được await")
ok(asyncio.run(P.run_registered("nope.kind", {}, None, None)) is None, "kind lạ → None")

print("B. loại việc")
P.register_task_kind({"id": "", "submit_url": "/x"})
P.register_task_kind({"id": "pod_studio.video", "submit_url": "no-slash"})
P.register_task_kind({"id": "pod_studio.video", "submit_url": "/api/v1/pod_studio/ref-video/run",
                      "fields": [{"key": "x", "type": "hologram"}]})
ok(not P._KINDS, "khai sai (thiếu id / url không bắt đầu bằng / / kiểu trường lạ) → bỏ qua")
SPEC = {
    "id": "pod_studio.video", "label": {"en": "Video from references", "vi": "Video từ ảnh tham chiếu"},
    "hint": {"en": "h-en"}, "icon": "shopping_bag", "submit_url": "/api/v1/pod_studio/ref-video/run",
    "upload_url": "/api/v1/pod_studio/gallery/upload-image", "order": 5,
    "fields": [
        {"key": "model_images", "type": "images", "label": {"en": "Model", "vi": "Người mẫu"}, "required": True, "max": 4},
        {"key": "format", "type": "select", "label": "Format",
         "options": [{"value": "ad", "label": {"en": "Ad", "vi": "Quảng cáo"}}, {"value": "short", "label": "Short"}]},
        {"key": "clips", "type": "number", "default": 3},
    ]}
P.register_task_kind(SPEC)
P.register_task_kind({"id": "other.kind", "submit_url": "/y", "fields": []})       # không có pipeline → ẩn
vi = P.task_kinds("vi")
ok([k["id"] for k in vi] == ["pod_studio.video"], "chỉ loại có pipeline đang đăng ký", [k["id"] for k in vi])
k = vi[0]
ok(k["label"] == "Video từ ảnh tham chiếu" and k["hint"] == "h-en", "dịch theo lang, thiếu → en", k)
ok(k["fields"][0]["label"] == "Người mẫu" and k["fields"][1]["label"] == "Format"
   and k["fields"][1]["options"] == [{"value": "ad", "label": "Quảng cáo"}, {"value": "short", "label": "Short"}],
   "trường + options dịch theo lang", k["fields"][1])
ok(P.task_kinds("zh-TW")[0]["label"] == "Video from references", "zh-TW thiếu → en")
P.unregister_pipeline("pod_studio.")
ok(P.task_kinds("en") == [] and P.find_pipeline("pod_studio.video") is None, "unregister bỏ cả pipeline lẫn loại việc")
P.register_pipeline("pod_studio.", runner, steps={"board": "pod_studio"})
P.register_task_kind(SPEC)

print("C. bước → extension")
from tubecli.extensions.codex.manager import _step_extension  # noqa: E402
ok(P.step_extension("board") == "pod_studio" and P.step_extension("zzz") is None, "step_extension theo sổ")
ok(_step_extension("board") == "pod_studio" and _step_extension("script") == "cloud_api" and _step_extension("weird") == "codex",
   "manager: bảng cứng trước, rồi sổ, rồi codex")

print("D. executor")
from tubecli.extensions.codex import executor as X  # noqa: E402
X._task_kind = lambda task: ("pod_studio.video", {"kind": "pod_studio.video", "format": "ad"})
out = asyncio.run(X._run_registered_pipeline({"id": "t1"}, lambda *a, **k: None, lambda: False))
ok(out == "done:pod_studio.video" and calls[-1][1].get("format") == "ad", "kind đã đăng ký → chạy runner", out)
X._task_kind = lambda task: ("nobody.kind", {})
ok(asyncio.run(X._run_registered_pipeline({"id": "t2"}, lambda *a, **k: None, lambda: False)) is None, "kind lạ → None (về agent)")

print("E. route")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from tubecli.extensions.codex.routes import router  # noqa: E402
app = FastAPI(); app.include_router(router)
c = TestClient(app)
r = c.get("/api/v1/codex/task-kinds?lang=vi").json()
ok(r["kinds"] and r["kinds"][0]["id"] == "pod_studio.video" and r["kinds"][0]["label"] == "Video từ ảnh tham chiếu", "GET /task-kinds", r)

print("F. giao diện + ngôn ngữ")
html = (ROOT / "tubecli/extensions/codex/static/codex.html").read_text(encoding="utf-8")
js = (ROOT / "tubecli/extensions/codex/static/codex.js").read_text(encoding="utf-8")
css = (ROOT / "tubecli/extensions/codex/static/codex.css").read_text(encoding="utf-8")
ok('id="cx-kind-ext"' in html and 'id="cx-new-ext"' in html, "html: chỗ cắm nút + form")
ok("async function loadExtKinds" in js and "function renderExtForm" in js and "async function submitExt" in js
   and "if (state.extKind) return submitExt();" in js and "await loadExtKinds();" in js, "js: nạp loại, vẽ form, gửi")
ok("onExtFiles, onExtField," in js, "js: export handler")
ok("uploadExtImage(ext.upload_url, file)" in js and "request(ext.submit_url" in js, "js: ảnh lên upload_url rồi POST submit_url")
ok(".cx-x-previews" in css, "css: ảnh xem trước")
KEYS = ["ext_images_pick", "ext_images_count", "ext_uploading", "ext_select_pick", "toast_ext_required", "created_ext_title", "created_ext_desc"]
for lang in ["en", "vi", "es", "ja", "ko", "ru", "tr", "zh", "zh-TW"]:
    d = json.loads((ROOT / "tubecli/extensions/codex/locales" / f"{lang}.json").read_text(encoding="utf-8"))
    miss = [k for k in KEYS if not d.get("codex." + k)]
    ok(not miss and "{field}" in d["codex.toast_ext_required"] and "{total}" in d["codex.ext_uploading"], f"locale {lang}", miss)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
