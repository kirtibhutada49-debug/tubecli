# -*- coding: utf-8 -*-
"""Content Studio ↔ kho MẪU chung của lõi (studio_db/json_store.py dùng tubecli.core.templates).

Kiểm:
  A. lần đầu: mẫu cũ trong presets.json chuyển sang kho chung (một lần, có dấu), CS đọc y nguyên
  B. mẫu Pod Studio tạo (chỉ phần ref_video) hiện trong danh sách CS với khoá wiz* dựng từ phần chung
  C. CS lưu → vào kho chung (phần wizard) VÀ presets.json (dự phòng); xoá → mất ở cả hai
  D. kho của Studio nằm NGOÀI DATA_DIR (kiểu test) → không đụng kho chung, dùng presets.json
  E. lõi cũ không có tubecli.core.templates → dùng presets.json như trước

Run:  python tests/templates_cs_bridge_test.py   (SKIP khi máy không có extension content_studio)
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
EXT = ROOT / "data" / "extensions_external" / "content_studio"
if not (EXT / "studio_db" / "json_store.py").exists():
    print(f"SKIP: không có extension content_studio tại {EXT}")
    sys.exit(0)
sys.path.insert(0, str(EXT))

TMP = tempfile.mkdtemp(prefix="tpl_cs_")
import tubecli.config as cfg  # noqa: E402
cfg.DATA_DIR = Path(TMP)
from tubecli.core import templates as T  # noqa: E402
from studio_db.json_store import JsonStore  # noqa: E402

PASS = FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  ", label)
    else:
        FAIL += 1
        print("  FAIL", label, "—", str(detail)[:300])


CS_DIR = os.path.join(TMP, "content_studio")
os.makedirs(CS_DIR)
OLD = [{"id": 1, "name": "Tin Công Nghệ", "data": json.dumps({"wizAspectRatio": "16:9", "wizStyle": "Pixar 3D", "wizSceneKit": "t2:tech_news"}),
        "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-01T00:00:00"},
       {"id": 2, "name": "Edo", "data": json.dumps({"wizAspectRatio": "9:16", "wizStyle": "Japanese Edo Watercolor"})}]
with open(os.path.join(CS_DIR, "presets.json"), "w", encoding="utf-8") as f:
    json.dump(OLD, f, ensure_ascii=False)
db = JsonStore(CS_DIR); db._init_meta()

print("A. chuyển mẫu cũ một lần")
names = sorted(p["name"] for p in db.list_presets())
ok(names == ["Edo", "Tin Công Nghệ"] and len(T.list_templates()) == 2, "2 mẫu cũ vào kho chung", names)
p = db.get_preset("Tin Công Nghệ")
ok(json.loads(p["data"]) == json.loads(OLD[0]["data"]) and p["id"] == 1 and p["created_at"] == "2026-09-01T00:00:00",
   "CS đọc y nguyên dữ liệu + id cũ + mốc", p)
ok(os.path.isfile(os.path.join(CS_DIR, "presets.core_migrated.json")), "có dấu đã chuyển")
T.delete_template("Edo")                        # xoá ở kho chung rồi tạo JsonStore mới: dấu còn → KHÔNG chuyển lại
db2 = JsonStore(CS_DIR); db2._init_meta()
ok(db2.get_preset("Edo") is None and len(T.list_templates()) == 1, "dấu đã chuyển → không chép lại mẫu đã xoá", [t["name"] for t in T.list_templates()])

print("B. mẫu Pod Studio hiện trong CS")
T.save_section("Pod 3D", "ref_video", {"format": "short", "clips": 3, "aspect": "9:16", "style": "3d"}, origin="pod_studio")
lst = {p["name"]: p for p in db.list_presets()}
d = json.loads(lst["Pod 3D"]["data"])
ok("Pod 3D" in lst and d.get("wizAspectRatio") == "9:16" and d.get("wizStyle") == "Semi-realistic 3D CG render"
   and lst["Pod 3D"]["origin"] == "pod_studio" and lst["Pod 3D"]["sections"] == ["ref_video"], "mẫu Pod có khoá wiz* dựng sẵn", lst.get("Pod 3D"))

print("C. CS lưu / xoá")
db.save_preset("Pod 3D", {**d, "wizSceneKit": "stick"})
t = T.get_template("Pod 3D")
ok(t["sections"]["wizard"]["data"]["wizSceneKit"] == "stick" and T.section_view(t, "ref_video")["clips"] == 3, "CS lưu phần wizard, phần Pod giữ")
legacy = json.load(open(os.path.join(CS_DIR, "presets.json"), encoding="utf-8"))
ok(any(x["name"] == "Pod 3D" for x in legacy), "presets.json được ghi song song (dự phòng)")
db.delete_preset("Pod 3D")
legacy = json.load(open(os.path.join(CS_DIR, "presets.json"), encoding="utf-8"))
ok(T.get_template("Pod 3D") is None and not any(x["name"] == "Pod 3D" for x in legacy), "xoá ở cả hai")
ok(db.get_preset("tin công nghệ") is None and db.get_preset("Tin Công Nghệ") is not None, "get_preset vẫn so tên ĐÚNG như cũ")

print("D. kho Studio ngoài DATA_DIR → không đụng kho chung")
OUT = tempfile.mkdtemp(prefix="tpl_cs_out_")
db_out = JsonStore(OUT); db_out._init_meta()
n = len(T.list_templates())
db_out.save_preset("Mẫu thử", {"wizAspectRatio": "1:1"})
ok(len(T.list_templates()) == n and [p["name"] for p in db_out.list_presets()] == ["Mẫu thử"], "ghi presets.json riêng, kho chung không đổi")

print("E. lõi cũ (không có tubecli.core.templates)")
saved_mod = sys.modules.get("tubecli.core.templates")
import tubecli.core as _core  # noqa: E402
saved_attr = getattr(_core, "templates", None)
sys.modules["tubecli.core.templates"] = None          # import → ImportError
if saved_attr is not None:
    delattr(_core, "templates")
db_old = JsonStore(CS_DIR); db_old._init_meta()
ok(sorted(p["name"] for p in db_old.list_presets()) == ["Edo", "Tin Công Nghệ"], "đọc presets.json (dự phòng còn đủ)",
   [p["name"] for p in db_old.list_presets()])
sys.modules["tubecli.core.templates"] = saved_mod
if saved_attr is not None:
    _core.templates = saved_attr

print(f"\n{PASS} passed, {FAIL} failed")
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
sys.exit(1 if FAIL else 0)
