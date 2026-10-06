#!/usr/bin/env bash
# TubeCLI — bộ cài cho máy ở Trung Quốc đại lục (và mạng nào chặn raw.githubusercontent.com).
#
#   curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/tubecreate/tubecli/main/install-cn.sh | bash
#   (dự phòng)  https://fastly.jsdelivr.net/gh/tubecreate/tubecli@main/install-cn.sh
#   Ghép nối luôn với TubeCLI Cloud:  … | bash -s -- --code=MÃ
#
# Đo trên ECS Aliyun Bắc Kinh 6/10/2026 (Ubuntu 24.04): raw.githubusercontent.com bị RESET ngay
# (curl 35) — chỉ chỗ đó làm install.sh chết. github.com git clone 5 s, PyPI có mirror nội bộ Aliyun
# sẵn, npm vào được. Nhưng tải bản phát hành GitHub (cloudflared 40 MB) ~60 KB/s, Node 18 có sẵn
# (install.sh cần ≥ 20), *.workers.dev bị chặn, cloud.tubecreate.com (Cloudflare) 6–30 s/lượt.
# Nên bộ này KHÔNG viết lại install.sh — chỉ dọn đường rồi chạy nó từ bản clone:
#   1. chọn đường tới GitHub: thẳng nếu raw vào được, không thì proxy (gh-proxy.com, ghfast.top)
#   2. chọn URL clone: github.com nếu `git ls-remote` được, không thì qua proxy
#   3. Node 20 từ mirror Aliyun/npmmirror, npm registry → npmmirror, pip → mirror (nếu chưa có)
#   4. cloudflared qua proxy (9 MB/s thay vì 60 KB/s)
#   5. clone → chạy ./install.sh trong bản clone (install.sh tự nhận «đang đứng trong mã nguồn»)
#   6. ghi gh_proxy vào data/global_settings.json để lõi dùng lúc chạy (manifest ShardX…)
# Ghi đè bằng biến môi trường: TUBECLI_GH_PROXY (rỗng = đi thẳng), TUBECLI_GH_PROXIES,
# TUBECLI_PIP_MIRROR, TUBECLI_NPM_MIRROR, TUBECLI_NODE_VERSION, TUBECLI_INSTALL_DIR.
set -uo pipefail

REPO="https://github.com/tubecreate/tubecli.git"
RAW="https://raw.githubusercontent.com/tubecreate/tubecli/main"
GH_PROXIES="${TUBECLI_GH_PROXIES:-https://gh-proxy.com/ https://ghfast.top/}"
PIP_MIRROR="${TUBECLI_PIP_MIRROR:-https://mirrors.aliyun.com/pypi/simple/}"
NPM_MIRROR="${TUBECLI_NPM_MIRROR:-https://registry.npmmirror.com}"
NODE_VERSION="${TUBECLI_NODE_VERSION:-20.18.0}"
NODE_MIRRORS="${TUBECLI_NODE_MIRRORS:-https://mirrors.aliyun.com/nodejs-release https://registry.npmmirror.com/-/binary/node}"
INSTALL_DIR="${TUBECLI_INSTALL_DIR:-${HOME}/tubecli}"

C='\033[1;36m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
say()  { echo -e "${C}[cn]${N} $*"; }
warn() { echo -e "${Y}[cn] $*${N}"; }
die()  { echo -e "${R}[cn] $*${N}"; exit 1; }
reach() { curl -fsS -o /dev/null --max-time "${2:-8}" "$1" 2>/dev/null; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 && SUDO="sudo" || warn "not root and no sudo — system steps may fail"
fi

# --code=MÃ là việc của client ghép nối, không phải của install.sh — tách ra.
PAIR_CODE=""
ARGS=()
for a in "$@"; do
    case "$a" in
        --code=*) PAIR_CODE="${a#--code=}" ;;
        *) ARGS+=("$a") ;;
    esac
done

# ── 0. git + curl + xz (giải nén Node) ──────────────────────────────────────
need_pkgs=()
command -v git  >/dev/null 2>&1 || need_pkgs+=(git)
command -v curl >/dev/null 2>&1 || need_pkgs+=(curl)
command -v xz   >/dev/null 2>&1 || need_pkgs+=(xz-utils)
# Ubuntu 24.04 của Aliyun có python3 + pip mà THIẾU ensurepip → install.sh không tạo được venv (6/10/2026).
if command -v apt-get >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 && ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    need_pkgs+=(python3-venv "python$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')-venv")
fi
if [ ${#need_pkgs[@]} -gt 0 ]; then
    say "installing ${need_pkgs[*]}…"
    if command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update -qq || true
        $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${need_pkgs[@]}" ca-certificates || true
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y -q git curl xz || true
    elif command -v yum >/dev/null 2>&1; then
        $SUDO yum install -y -q git curl xz || true
    fi
fi
command -v git >/dev/null 2>&1 || die "git is required (apt install git)"

# ── 1. đường tới GitHub ─────────────────────────────────────────────────────
# Proxy TRƯỚC, đi thẳng chỉ khi mọi proxy hỏng: chạy thử 6/10 thì raw.githubusercontent.com lúc RESET lúc
# qua (GFW chập chờn) — một lượt thử «qua» làm bộ cài chọn đi thẳng rồi kẹt tải cloudflared 60 KB/s.
if [ -n "${TUBECLI_GH_PROXY+x}" ]; then
    GH_PROXY="$TUBECLI_GH_PROXY"
else
    GH_PROXY=""
    for p in $GH_PROXIES; do
        if reach "${p}${RAW}/install.sh" 12; then GH_PROXY="$p"; break; fi
    done
    if [ -z "$GH_PROXY" ]; then
        reach "$RAW/install.sh" || warn "raw.githubusercontent.com and every proxy failed — continuing with git only"
    fi
fi
say "GitHub files: ${GH_PROXY:-direct}"

# ── 2. URL clone ────────────────────────────────────────────────────────────
REPO_URL=""
cands=("$REPO")
[ -n "$GH_PROXY" ] && cands+=("${GH_PROXY}${REPO}")
for p in $GH_PROXIES; do [ "$p" != "$GH_PROXY" ] && cands+=("${p}${REPO}"); done
for u in "${cands[@]}"; do
    if timeout 30 git ls-remote --heads "$u" main >/dev/null 2>&1; then REPO_URL="$u"; break; fi
done
[ -z "$REPO_URL" ] && die "cannot reach the TubeCLI repository (github.com and proxies all failed)"
say "repository: $REPO_URL"

# ── 3a. pip mirror — chỉ khi máy CHƯA có (Aliyun/Tencent cài sẵn mirror nội bộ, nhanh hơn) ──
if ! grep -qsi "index-url" /etc/pip.conf /etc/xdg/pip/pip.conf "$HOME/.pip/pip.conf" "$HOME/.config/pip/pip.conf" \
   && [ -z "${PIP_INDEX_URL:-}" ]; then
    say "pip mirror → $PIP_MIRROR"
    printf '[global]\nindex-url = %s\ntimeout = 60\n' "$PIP_MIRROR" | $SUDO tee /etc/pip.conf >/dev/null || \
        export PIP_INDEX_URL="$PIP_MIRROR"
else
    say "pip mirror: already configured"
fi

# ── 3b. Node ≥ 20 từ mirror (install.sh sẽ đi deb.nodesource.com — chậm/không chắc) ──
node_major() { node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }
if ! command -v node >/dev/null 2>&1 || [ "$(node_major || echo 0)" -lt 20 ] 2>/dev/null; then
    case "$(uname -m)" in aarch64|arm64) NA=arm64 ;; armv7l) NA=armv7l ;; *) NA=x64 ;; esac
    tarball="node-v${NODE_VERSION}-linux-${NA}.tar.xz"
    got=""
    for m in $NODE_MIRRORS; do
        say "Node ${NODE_VERSION} ← $m"
        if curl -fsSL --max-time 300 "$m/v${NODE_VERSION}/$tarball" -o "/tmp/$tarball"; then got=1; break; fi
    done
    if [ -n "$got" ]; then
        $SUDO tar -xJf "/tmp/$tarball" -C /usr/local --strip-components=1 \
            --exclude='*/CHANGELOG.md' --exclude='*/LICENSE' --exclude='*/README.md' && rm -f "/tmp/$tarball"
        hash -r
        say "node $(node -v 2>/dev/null || echo '?') at $(command -v node)"
    else
        warn "Node mirror download failed — install.sh will try its own way"
    fi
fi

# ── 3c. npm registry → mirror (9Router, Codex CLI, bộ dựng canvas đều cài qua npm) ──
if command -v npm >/dev/null 2>&1; then
    $SUDO npm config set registry "$NPM_MIRROR" --location=global >/dev/null 2>&1 \
        || npm config set registry "$NPM_MIRROR" >/dev/null 2>&1 || true
    say "npm registry → $(npm config get registry 2>/dev/null)"
fi
export npm_config_registry="$NPM_MIRROR"

# ── 4. cloudflared qua proxy (tunnel tới TubeCLI Cloud) ─────────────────────
case "$(uname -m)" in aarch64|arm64) CA=arm64 ;; armv7l|armhf) CA=arm ;; *) CA=amd64 ;; esac
CF_REL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CA}"
if ! command -v cloudflared >/dev/null 2>&1; then
    tried=" "
    for p in "$GH_PROXY" $GH_PROXIES ""; do
        case "$tried" in *" ${p:-direct} "*) continue ;; esac
        tried="$tried${p:-direct} "
        say "cloudflared ← ${p:-github.com}"
        if curl -fsSL --max-time 240 "${p}${CF_REL}" -o /tmp/cloudflared && [ -s /tmp/cloudflared ]; then
            $SUDO install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared && rm -f /tmp/cloudflared && break
        fi
    done
    command -v cloudflared >/dev/null 2>&1 && say "$(cloudflared --version 2>/dev/null | head -1)" \
        || warn "cloudflared not installed — the cloud tunnel will retry later"
fi
# Client ghép nối tìm cloudflared ở thư mục cấu hình của nó trước khi tự tải (chậm) — đặt sẵn.
if command -v cloudflared >/dev/null 2>&1; then
    CONN_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/tubecli"
    mkdir -p "$CONN_HOME" && [ -f "$CONN_HOME/cloudflared" ] || cp "$(command -v cloudflared)" "$CONN_HOME/cloudflared" 2>/dev/null || true
fi

# ── 5. clone rồi chạy install.sh từ bản clone ───────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    say "existing checkout at $INSTALL_DIR — pointing it at $REPO_URL"
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
    git -C "$INSTALL_DIR" fetch -q origin main && git -C "$INSTALL_DIR" reset -q --hard origin/main \
        || warn "could not update the existing checkout — installing what is there"
else
    say "cloning into $INSTALL_DIR…"
    git clone -q -b main "$REPO_URL" "$INSTALL_DIR" || die "git clone failed"
fi

cd "$INSTALL_DIR" || die "cannot enter $INSTALL_DIR"
say "running install.sh…"
# Chạy qua `curl | bash` thì stdin là chính script — trả bàn phím cho `tubecli init` bằng /dev/tty. Không có
# terminal (nohup, provisioner SSH không pty) thì /dev/tty TỒN TẠI mà mở ra lỗi «No such device» (6/10) —
# phải thử MỞ, không chỉ kiểm -r; mở không được thì stdin rỗng (install.sh tự chạy không tương tác).
run_install() { TUBECLI_REPO_URL="$REPO_URL" TUBECLI_INSTALL_DIR="$INSTALL_DIR" bash ./install.sh "${ARGS[@]+"${ARGS[@]}"}"; }
if [ -t 0 ]; then
    run_install
elif { exec 3</dev/tty; } 2>/dev/null; then
    run_install <&3; RC_TTY=$?; exec 3<&-; (exit $RC_TTY)
else
    run_install </dev/null
fi
RC=$?

# ── 6. lõi dùng proxy lúc chạy (manifest ShardX, đường lui kiểm tra cập nhật…) ──
if [ -n "$GH_PROXY" ] && [ -d "$INSTALL_DIR/data" ] && command -v python3 >/dev/null 2>&1; then
    GH_PROXY="$GH_PROXY" python3 - "$INSTALL_DIR/data/global_settings.json" <<'PY' && say "runtime gh_proxy saved"
import json, os, sys
p = sys.argv[1]
try:
    d = json.load(open(p, encoding="utf-8"))
    if not isinstance(d, dict):
        d = {}
except Exception:
    d = {}
d["gh_proxy"] = os.environ["GH_PROXY"]
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
fi

[ "$RC" -ne 0 ] && die "install.sh failed (exit $RC) — see the output above"

# ── 7. ghép nối với TubeCLI Cloud (tuỳ chọn) ────────────────────────────────
if [ -n "$PAIR_CODE" ]; then
    say "pairing with TubeCLI Cloud (code $PAIR_CODE)…"
    if curl -fsSL --max-time 60 "${GH_PROXY}${RAW}/client/tubecli_connect.pyw" -o "$HOME/tubecli_connect.py"; then
        TUBECLI_GH_PROXY="$GH_PROXY" python3 "$HOME/tubecli_connect.py" --code="$PAIR_CODE"
    else
        die "could not download the pairing client"
    fi
fi
echo -e "${G}[cn] done.${N}"
