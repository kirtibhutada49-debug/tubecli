# -*- coding: utf-8 -*-
"""tubecli.core.net_mirror — URL GitHub qua proxy cho máy ở mạng chặn raw.githubusercontent.com.

Vì sao (6/10/2026): ECS Aliyun Bắc Kinh — raw.githubusercontent.com RESET, tải bản phát hành GitHub
~60 KB/s, proxy tiền tố (https://gh-proxy.com/<url>) 0,7 s / 9 MB/s. install-cn.sh ghi "gh_proxy"
vào data/global_settings.json; lõi đọc qua mirror_url() ở manifest ShardX và đường lui kiểm tra cập nhật.

Kiểm (không gọi mạng; settings trỏ sang file tạm):
  A. không cấu hình → URL nguyên vẹn (máy ngoài Trung Quốc không đổi hành vi)
  B. env TUBECLI_GH_PROXY thắng settings; "" = tắt kể cả khi settings có
  C. settings gh_proxy → gắn tiền tố cho raw / github.com / objects; thiếu '/' cuối tự thêm
  D. URL không phải GitHub, URL đã qua proxy, tiền tố không phải http(s) → giữ nguyên
  E. đổi file settings (mtime) → đọc lại; file hỏng → coi như không cấu hình
  F. hai chỗ dùng trong lõi + client ghép nối + install-cn.sh đúng hình dạng
Run:  python tests/net_mirror_test.py     (exit 0 = pass)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


import tubecli.config as cfg  # noqa: E402
from tubecli.core import net_mirror as M  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="netmirror_"))
settings = tmp / "global_settings.json"
cfg.GLOBAL_SETTINGS_FILE = settings
os.environ.pop("TUBECLI_GH_PROXY", None)
RAW = "https://raw.githubusercontent.com/ProxyShard/ShardBrowser/main/runtime.json"
REL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"


def write(d):
    settings.write_text(json.dumps(d), encoding="utf-8")
    t = time.time() + (write.n if hasattr(write, "n") else 0)
    write.n = getattr(write, "n", 0) + 2
    os.utime(settings, (t, t))   # mtime chắc chắn đổi giữa hai lượt ghi liền nhau


print("── A. không cấu hình ─────────────────────────────")
ok(M.mirror_url(RAW) == RAW, "không có settings → giữ nguyên", M.mirror_url(RAW))
write({"language": "vi"})
ok(M.gh_proxy() == "" and M.mirror_url(REL) == REL, "settings không có gh_proxy → giữ nguyên")

print("── B/C. settings + env ───────────────────────────")
write({"gh_proxy": "https://gh-proxy.com"})
ok(M.gh_proxy() == "https://gh-proxy.com/", "thiếu '/' cuối tự thêm", M.gh_proxy())
ok(M.mirror_url(RAW) == "https://gh-proxy.com/" + RAW, "raw → qua proxy", M.mirror_url(RAW))
ok(M.mirror_url(REL) == "https://gh-proxy.com/" + REL, "bản phát hành github.com → qua proxy")
ok(M.mirror_url("https://objects.githubusercontent.com/x") == "https://gh-proxy.com/https://objects.githubusercontent.com/x",
   "objects.githubusercontent → qua proxy")
os.environ["TUBECLI_GH_PROXY"] = "https://ghfast.top/"
ok(M.mirror_url(RAW) == "https://ghfast.top/" + RAW, "env thắng settings")
os.environ["TUBECLI_GH_PROXY"] = ""
ok(M.mirror_url(RAW) == RAW, "env rỗng = tắt kể cả khi settings có")
os.environ.pop("TUBECLI_GH_PROXY")

print("── D. giữ nguyên ─────────────────────────────────")
ok(M.mirror_url("https://pypi.org/simple/") == "https://pypi.org/simple/", "không phải GitHub → giữ nguyên")
once = M.mirror_url(RAW)
ok(M.mirror_url(once) == once, "đã qua proxy → không gắn hai lần")
write({"gh_proxy": "javascript:alert(1)"})
ok(M.mirror_url(RAW) == RAW, "tiền tố không phải http(s) → bỏ qua")

print("── E. đọc lại theo mtime / file hỏng ─────────────")
write({"gh_proxy": "https://ghfast.top/"})
ok(M.mirror_url(RAW).startswith("https://ghfast.top/"), "sửa settings → đọc lại ngay")
settings.write_text("{không phải json", encoding="utf-8")
os.utime(settings, (time.time() + 99, time.time() + 99))
ok(M.mirror_url(RAW) == RAW, "file hỏng → coi như không cấu hình")

print("── F. chỗ dùng ───────────────────────────────────")
shardx = (ROOT / "tubecli" / "extensions" / "browser" / "shardx_runtime.py").read_text(encoding="utf-8")
ok("requests.get(mirror_url(MANIFEST_URL), timeout=timeout)" in shardx, "manifest ShardX qua mirror_url")
server = (ROOT / "tubecli" / "api" / "server.py").read_text(encoding="utf-8")
ok('raw_url = mirror_url("https://raw.githubusercontent.com/tubecreate/tubecli/main/tubecli/__init__.py")' in server,
   "đường lui kiểm tra cập nhật qua mirror_url")
client = (ROOT / "client" / "tubecli_connect.pyw").read_text(encoding="utf-8")
ok('GH_PROXY = (os.environ.get("TUBECLI_GH_PROXY") or "").strip()' in client
   and 'INSTALL_SH = f"{RAW}/install-cn.sh" if GH_PROXY else f"{RAW}/install.sh"' in client
   and 'base = f"{GH_PROXY}https://github.com/cloudflare/cloudflared/' in client,
   "client ghép nối: proxy cho RAW + cloudflared, có proxy thì cài bằng install-cn.sh")
cn = (ROOT / "install-cn.sh").read_text(encoding="utf-8")
ok('d["gh_proxy"] = os.environ["GH_PROXY"]' in cn and "bash ./install.sh" in cn and "--code=*) PAIR_CODE" in cn,
   "install-cn.sh: ghi gh_proxy, chạy install.sh từ bản clone, nhận --code")
ok(cn.index('for p in $GH_PROXIES; do\n        if reach') < cn.index('reach "$RAW/install.sh" ||'),
   "install-cn.sh thử proxy TRƯỚC đi thẳng (raw chập chờn 6/10)")
bash = None
for cand in (os.environ.get("BASH"), r"C:\Program Files\Git\bin\bash.exe", "/bin/bash", "/usr/bin/bash"):
    if cand and Path(cand).exists():
        bash = cand
        break
if bash:
    r = subprocess.run([bash, "-n", str(ROOT / "install-cn.sh")], capture_output=True, text=True)
    ok(r.returncode == 0, "install-cn.sh qua bash -n", r.stderr[:200])

print()
print("=" * 62)
print(f"{PASS}/{PASS + FAIL} PASS" if not FAIL else f"{PASS}/{PASS + FAIL} PASS — {FAIL} HỎNG")
sys.exit(1 if FAIL else 0)
