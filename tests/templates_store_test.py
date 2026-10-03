# -*- coding: utf-8 -*-
"""Kho MẪU chung (tubecli/core/templates.py + /api/v1/templates) — Content Studio và Pod Studio dùng chung một mẫu.

Kiểm:
  A. lưu phần "wizard" kiểu Content Studio → đọc lại Y NGUYÊN; phần chung rút đúng (khung, ngôn ngữ, kiểu, độ dài…)
  B. mẫu chỉ có phần wizard → Pod Studio (ref_video) nhận bản dựng từ phần chung
  C. Pod Studio lưu phần ref_video cùng tên → khung hình/kiểu hình đổi sang Content Studio; khoá riêng CS giữ nguyên
  D. hai trình hướng dẫn cùng ghi "wizard": gộp khoá, không xoá khoá của bên kia
  E. tìm theo id/tên (hoa thường), đổi tên (trùng → lỗi), xoá, chuyển kho cũ một lần (bỏ tên đã có), file hỏng để riêng
  F. route /api/v1/templates: liệt kê có view, lưu, 400 phần lạ, 404, khách workspace bị chặn ghi

Run:  python tests/templates_store_test.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

TMP = tempfile.mkdtemp(prefix="tpl_store_")
import tubecli.config as cfg  # noqa: E402
cfg.DATA_DIR = Path(TMP)
from tubecli.core import templates as T  # noqa: E402

PASS = FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  ", label)
    else:
        FAIL += 1
        print("  FAIL", label, "—", str(detail)[:300])


CS = {"wizContentFormat": "Educational / Learning", "wizEpisodes": "1", "wizStyle": "Japanese Edo Watercolor",
      "wizStyleCustom": "", "wizCharacterStyle": "Default", "wizAspectRatio": "9:16", "wizLanguage": "ja",
      "wizPipelineTemplate": "explainer", "wizVideoLength": "short_3m", "wizSubtitleStyle": "jp_telop",
      "wizSceneKit": "edo", "wizSpriteLibrary": "edo_kit", "wizTtsPreset": "capcut||ICL_jp_female_tt_you"}

print("A. phần wizard đọc lại y nguyên + phần chung")
t = T.save_section("Edo Nhật", "wizard", CS, origin="content_studio")
ok(t["id"].startswith("tpl_") and t["origin"] == "content_studio", "tạo mẫu có id + nguồn", t.get("id"))
ok(T.section_view(T.get_template("Edo Nhật"), "wizard") == CS, "wizard view == dữ liệu đã lưu", T.section_view(t, "wizard"))
sh = T.get_template("Edo Nhật")["shared"]
ok(sh == {"aspect": "9:16", "language": "ja", "style": "Japanese Edo Watercolor", "style_preset": "painting",
          "genre": "explainer", "length_sec": 180, "subtitles": "jp_telop"}, "phần chung rút đúng", sh)

print("B. Pod Studio đọc mẫu chỉ có phần wizard")
rv = T.section_view(T.get_template("edo nhật"), "ref_video")
ok(rv.get("aspect") == "9:16" and rv.get("style") == "painting" and rv.get("style_custom") == "Japanese Edo Watercolor"
   and rv.get("format") == "short" and rv.get("clips") == 12 and rv.get("subtitles") is True and rv.get("language") == "ja",
   "ref_video dựng từ phần chung", rv)

print("C. Pod Studio lưu ref_video cùng tên → CS thấy khung/kiểu mới")
T.save_section("Edo Nhật", "ref_video", {"format": "ad", "clips": 3, "aspect": "16:9", "style": "3d", "subtitles": False},
               origin="pod_studio")
w = T.section_view(T.get_template("Edo Nhật"), "wizard")
ok(w["wizAspectRatio"] == "16:9" and w["wizStyle"] == "Semi-realistic 3D CG render" and w["wizVideoLength"] == "short_60s"
   and w["wizSubtitleStyle"] == "", "CS thấy khung 16:9, kiểu 3D, độ dài 30 s → short_60s, tắt phụ đề", w)
ok(w["wizSceneKit"] == "edo" and w["wizTtsPreset"] == CS["wizTtsPreset"] and w["wizLanguage"] == "ja" and w["wizPipelineTemplate"] == "explainer",
   "khoá riêng CS (bộ cảnh, giọng) + trường Pod không đổi (ngôn ngữ, thể loại ad≠drama) giữ nguyên", w)
rv = T.section_view(T.get_template("Edo Nhật"), "ref_video")
ok(rv == {"format": "ad", "clips": 3, "aspect": "16:9", "style": "3d", "subtitles": False}, "Pod đọc lại y nguyên phần mình", rv)
# CS lưu lại (đổi khung về 9:16) → Pod thấy 9:16, giữ format/clips của mình
T.save_section("Edo Nhật", "wizard", {**w, "wizAspectRatio": "9:16"}, origin="content_studio")
rv = T.section_view(T.get_template("Edo Nhật"), "ref_video")
ok(rv["aspect"] == "9:16" and rv["format"] == "ad" and rv["clips"] == 3 and rv["style"] == "3d", "CS đổi khung → Pod thấy, phần riêng Pod giữ", rv)
ok(T.get_template("Edo Nhật")["origin"] == "content_studio" and T.get_template("Edo Nhật")["sections"].keys() >= {"wizard", "ref_video"},
   "nguồn = nơi tạo đầu tiên; có cả hai phần riêng")

print("D. hai trình hướng dẫn cùng ghi wizard → gộp khoá")
T.save_section("Gộp", "wizard", {"wizStyle": "Anime", "wizVideoEngine": "grok", "wizYtChannel": "kenh1"}, origin="pod_studio")
T.save_section("Gộp", "wizard", {"wizStyle": "Pixar 3D", "wizSceneKit": "stick"}, origin="content_studio")
g = T.section_view(T.get_template("Gộp"), "wizard")
ok(g == {"wizStyle": "Pixar 3D", "wizVideoEngine": "grok", "wizYtChannel": "kenh1", "wizSceneKit": "stick"}, "khoá Pod (wizVideoEngine) còn sau khi CS lưu", g)
ok(T.get_template("Gộp")["shared"]["style_preset"] == "3d", "«Pixar 3D» → khoá 3d")
T.save_section("Gộp", "wizard", {"wizStyle": "X"}, replace=True)
ok(T.section_view(T.get_template("Gộp"), "wizard") == {"wizStyle": "X"}, "replace=True thay cả phần")

print("E. tìm / đổi tên / xoá / chuyển kho cũ / file hỏng")
tid = T.get_template("Gộp")["id"]
ok(T.get_template(tid)["name"] == "Gộp" and T.get_template("  gỘP ") is not None and T.get_template("") is None, "theo id, theo tên không phân biệt hoa thường")
try:
    T.rename_template("Gộp", "edo NHẬT"); ok(False, "đổi tên trùng → lỗi")
except ValueError:
    ok(True, "đổi tên trùng mẫu khác → ValueError")
ok(T.rename_template("Gộp", "Gộp 2")["name"] == "Gộp 2" and T.get_template(tid)["name"] == "Gộp 2", "đổi tên giữ id")
for bad in (("", "wizard", {}), ("x", "nope", {}), ("x", "wizard", "str")):
    try:
        T.save_section(*bad); ok(False, f"lỗi đầu vào {bad[:2]}")
    except ValueError:
        ok(True, f"lỗi đầu vào bị chặn: {bad[:2]}")
rows = [{"id": 7, "name": "edo nhật", "data": "{}"}, {"id": 8, "name": "Cũ 1", "data": json.dumps({"wizAspectRatio": "1:1"}),
         "created_at": "2026-09-01T10:00:00", "updated_at": "2026-09-02T10:00:00"}, {"id": 9, "name": "Hỏng", "data": "{not json"}]
parse = lambda raw: json.loads(raw) if isinstance(raw, str) else raw
def safe_parse(raw):
    try:
        return parse(raw)
    except ValueError:
        return None
added, skipped = T.import_legacy(rows, "wizard", "content_studio", safe_parse)
c1 = T.get_template("Cũ 1")
ok((added, skipped) == (1, 2) and c1["legacy_id"] == 8 and c1["created_at"] == "2026-09-01T10:00:00" and c1["shared"]["aspect"] == "1:1",
   "chuyển kho cũ: bỏ tên đã có + dữ liệu hỏng, giữ id cũ + mốc", (added, skipped, c1))
ok(T.import_legacy(rows, "wizard", "content_studio", safe_parse) == (0, 3), "chạy lại không nhân đôi")
ok(T.delete_template("Gộp 2") and T.get_template("Gộp 2") is None and not T.delete_template("Gộp 2"), "xoá; xoá lần hai → False")
with open(T._store_path(), "w", encoding="utf-8") as f:
    f.write("{hỏng")
ok(T.list_templates() == [] and any(n.startswith("templates.json.broken-") for n in os.listdir(os.path.dirname(T._store_path()))),
   "file hỏng → dời sang .broken-*, không ghi đè im lặng")

print("F. route /api/v1/templates")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from tubecli.api.templates_routes import router  # noqa: E402
app = FastAPI()
GUEST = {"on": False}


@app.middleware("http")
async def _guest(request, call_next):
    if GUEST["on"]:
        request.state.guest_scope = {"id": "g"}
    return await call_next(request)
app.include_router(router)
c = TestClient(app)
r = c.post("/api/v1/templates", json={"name": "Mẫu API", "section": "ref_video", "data": {"aspect": "1:1", "style": "anime", "clips": 2, "format": "ad"}, "origin": "pod_studio"})
ok(r.status_code == 200 and r.json()["template"]["view"]["style"] == "anime", "POST lưu ref_video", r.text[:200])
r = c.get("/api/v1/templates", params={"section": "wizard"})
v = r.json()["templates"][0]
ok(r.status_code == 200 and v["name"] == "Mẫu API" and v["view"]["wizAspectRatio"] == "1:1" and v["view"]["wizStyle"] == "2D anime illustration"
   and v["view"]["wizPipelineTemplate"] == "explainer" and v["view"]["wizContentFormat"] == "Educational / Learning"
   and v["sections"] == ["ref_video"], "GET ?section=wizard → CS thấy mẫu Pod (có cả thể loại)", v)
ok(c.get("/api/v1/templates/mẫu api").json()["template"]["shared"]["length_sec"] == 20, "GET một mẫu theo tên")
ok(c.get("/api/v1/templates", params={"section": "lạ"}).status_code == 400 and c.get("/api/v1/templates/không-có").status_code == 404, "400 phần lạ, 404 không có")
ok(c.post("/api/v1/templates/Mẫu API/rename", json={"name": "Mẫu API 2"}).json()["template"]["name"] == "Mẫu API 2", "đổi tên qua route")
GUEST["on"] = True
ok(c.post("/api/v1/templates", json={"name": "x", "section": "wizard", "data": {}}).status_code == 403
   and c.delete("/api/v1/templates/Mẫu API 2").status_code == 403, "khách workspace: ghi/xoá → 403")
GUEST["on"] = False
ok(c.delete("/api/v1/templates/Mẫu API 2").status_code == 200 and c.get("/api/v1/templates").json()["templates"] == [], "xoá qua route")

print(f"\n{PASS} passed, {FAIL} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
