#!/usr/bin/env bash
# ============================================================================
#  install.sh - Auto-install tokenharbor untuk semua OS & versi
#
#  Mendukung: Linux (Ubuntu, Debian, CentOS, Fedora, Arch, Alpine),
#             macOS (Intel & Apple Silicon), Windows (WSL/Git Bash/Cygwin),
#             arsitektur x86_64, arm64/aarch64.
#
#  Fitur:
#    1. Deteksi OS & arsitektur secara otomatis.
#    2. Install Python 3.10+ (via package manager atau pyenv jika perlu).
#    3. Semua dependency dipasang di virtualenv lokal (.venv) sehingga tidak
#       mengganggu python sistem dan bebas masalah PEP-668.
#    4. Memasang dependency sistem browser & unduh browser camoufox.
#    5. Membuat launcher `./tokenharbor` dan `./grok`.
#    6. Support offline mode (skip browser download).
#
#  Cara pakai:   bash install.sh              (auto-install semua)
#  Opsi:         bash install.sh --skip-browser   # jangan unduh browser
#                bash install.sh --skip-deps       # lewati paket sistem
#                bash install.sh --no-venv         # pasang ke python sistem
#                bash install.sh --python python3.11  # specify python binary
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PIP_PKG="camoufox curl_cffi requests beautifulsoup4 lxml pycryptodome playwright"
VENV_DIR=".venv"
MIN_PY_MAJOR=3
MIN_PY_MINOR=10

SKIP_BROWSER=0
SKIP_DEPS=0
NO_VENV=0
PYTHON_BIN=""

for a in "$@"; do
  case "$a" in
    --skip-browser) SKIP_BROWSER=1 ;;
    --skip-deps)    SKIP_DEPS=1 ;;
    --no-venv)      NO_VENV=1 ;;
    --python)       shift; PYTHON_BIN="$1"; shift ;;
    --python=*)     PYTHON_BIN="${a#*=}" ;;
    -h|--help)      grep '^#' "$0" | head -50 | sed 's/^# \{0,2\}//'; exit 0 ;;
    *) echo "Opsi tak dikenal: $a"; exit 1 ;;
  esac
done

log() { echo -e "$*"; }
err() { echo -e "✗ $*" >&2; }

# ---- 0. Deteksi OS & Arsitektur ------------------------------------------
OS_TYPE="unknown"
ARCH=$(uname -m)

if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS_TYPE="linux"
elif [[ "$OSTYPE" == "darwin"* ]]; then
    OS_TYPE="macos"
elif [[ "$OSTYPE" == "cygwin" ]] || [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "win32" ]]; then
    OS_TYPE="windows"
fi

if [ -f /etc/os-release ]; then
    . /etc/os-release
    DISTRO="${ID:-unknown}"
else
    DISTRO="unknown"
fi

case "$ARCH" in
  x86_64|amd64) ARCH_LBL="x86_64" ;;
  aarch64|arm64) ARCH_LBL="arm64" ;;
  *) log "⚠ Arsitektur $ARCH mungkin tidak didukung penuh"; ARCH_LBL="$ARCH" ;;
esac

log "==> Deteksi: OS=$OS_TYPE | Distro=$DISTRO | Arch=$ARCH_LBL"

# ---- 1. Hak akses --------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
  if ! command -v sudo >/dev/null 2>&1; then
    SUDO=""
  fi
fi

# ---- 2. Install paket sistem dasar ---------------------------------------
install_system_deps() {
    if [ "$SKIP_DEPS" -eq 1 ]; then
        log "==> [1/6] Skip paket sistem (--skip-deps)"
        return
    fi
    
    log "==> [1/6] Install paket sistem dasar"
    
    case "$DISTRO" in
        ubuntu|debian)
            $SUDO apt-get update -qq || true
            $SUDO apt-get install -y -qq \
                ca-certificates curl wget git gnupg software-properties-common \
                python3 python3-pip python3-venv build-essential libssl-dev \
                zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
                libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
                libffi-dev liblzma-dev 2>/dev/null || true
            ;;
        centos|rhel|fedora|rocky|alma)
            if command -v dnf >/dev/null 2>&1; then
                PKG_MGR="dnf"
            else
                PKG_MGR="yum"
            fi
            $SUDO $PKG_MGR install -y -q \
                ca-certificates curl wget git python3 python3-pip python3-devel \
                gcc gcc-c++ make openssl-devel bzip2-devel libffi-devel \
                zlib-devel readline-devel sqlite-devel xz-devel 2>/dev/null || true
            ;;
        arch|manjaro)
            $SUDO pacman -Sy --noconfirm --needed \
                base-devel openssl zlib xz python python-pip git curl wget 2>/dev/null || true
            ;;
        alpine)
            $SUDO apk add --no-cache \
                python3 python3-dev py3-pip gcc musl-dev linux-headers \
                libffi-dev openssl-dev curl wget git bash 2>/dev/null || true
            ;;
        *)
            log "    ⚠ Distro $DISTRO tidak dikenali, lewati install sistem deps"
            ;;
    esac
    
    if [ "$OS_TYPE" = "macos" ]; then
        if ! command -v brew >/dev/null 2>&1; then
            log "    ⚠ Homebrew tidak terinstall. Install via: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        else
            brew install python@3.11 openssl readline sqlite3 xz zlib 2>/dev/null || true
        fi
    fi
}

# ---- 3. Cari/Install Python >= 3.10 --------------------------------------
find_python() {
    log "==> [2/6] Cek versi Python (min. $MIN_PY_MAJOR.$MIN_PY_MINOR)"
    
    if [ -n "$PYTHON_BIN" ]; then
        if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
            if "$PYTHON_BIN" -c "import sys; exit(0 if sys.version_info >= ($MIN_PY_MAJOR,$MIN_PY_MINOR) else 1)" 2>/dev/null; then
                PY="$PYTHON_BIN"
                log "    Memakai: $($PY -V)"
                return
            fi
        fi
        err "Python binary '$PYTHON_BIN' tidak valid atau versi < 3.10"
        exit 1
    fi
    
    for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            if "$cmd" -c "import sys; exit(0 if sys.version_info >= ($MIN_PY_MAJOR,$MIN_PY_MINOR) else 1)" 2>/dev/null; then
                PY="$cmd"
                log "    Ditemukan: $($PY -V)"
                return
            fi
        fi
    done
    
    log "    Python >= 3.10 tidak ditemukan, mencoba install..."
    
    case "$DISTRO" in
        ubuntu|debian)
            if ! command -v add-apt-repository >/dev/null 2>&1; then
                $SUDO apt-get install -y software-properties-common
            fi
            $SUDO add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
            $SUDO apt-get update -qq
            $SUDO apt-get install -y python3.11 python3.11-venv python3.11-dev
            PY=python3.11
            ;;
        centos|rhel|fedora|rocky|alma)
            PKG_MGR=$(command -v dnf >/dev/null 2>&1 && echo "dnf" || echo "yum")
            $SUDO $PKG_MGR install -y python311 python311-devel 2>/dev/null || \
            $SUDO $PKG_MGR install -y python3.11 python3.11-devel 2>/dev/null || true
            PY=python3.11
            ;;
        arch|manjaro)
            $SUDO pacman -Sy --noconfirm python
            PY=python3
            ;;
        alpine)
            $SUDO apk add python3 python3-dev
            PY=python3
            ;;
        *)
            err "Gagal install Python >= 3.10. Install manual lalu jalankan: bash install.sh --python /path/to/python3"
            exit 1
            ;;
    esac
    
    if ! command -v "$PY" >/dev/null 2>&1; then
        err "Gagal install Python. Install manual: https://www.python.org/downloads/"
        exit 1
    fi
    
    log "    Berhasil install: $($PY -V)"
}

# ---- 4. Setup virtualenv + dependency ------------------------------------
setup_venv() {
    if [ "$NO_VENV" -eq 1 ]; then
        log "==> [3/6] Install dependency ke python sistem"
        if $PY -m pip --version >/dev/null 2>&1; then
            $PY -m pip install --user -q -U pip 2>/dev/null || true
            $PY -m pip install --user -q $PIP_PKG 2>/dev/null || \
            $PY -m pip install --user --break-system-packages -q $PIP_PKG || true
        fi
        PY_RUN="$PY"
    else
        log "==> [3/6] Buat virtualenv (.venv) dengan $($PY -V)"
        rm -rf "$VENV_DIR"
        $PY -m venv "$VENV_DIR" || {
            err "Gagal membuat virtualenv. Coba: $SUDO apt-get install python3-venv"
            exit 1
        }
        
        if [ -f "$VENV_DIR/bin/python" ]; then
            PY_RUN="$VENV_DIR/bin/python"
        elif [ -f "$VENV_DIR/Scripts/python.exe" ]; then
            PY_RUN="$VENV_DIR/Scripts/python.exe"
        else
            err "Virtualenv binary tidak ditemukan"
            exit 1
        fi
        
        $PY_RUN -m pip install -q -U pip
        $PY_RUN -m pip install -q $PIP_PKG || {
            err "Gagal install dependencies. Periksa koneksi internet."
            exit 1
        }
    fi
}

# ---- 5. Install browser dependencies -------------------------------------
install_browser_deps() {
    if [ "$SKIP_DEPS" -eq 0 ]; then
        log "==> [4/6] Install browser dependencies (playwright)"
        $PY_RUN -m playwright install-deps firefox 2>/dev/null || log "    ⚠ Sebagian lib tidak terpasang (lanjut)"
    else
        log "==> [4/6] Skip browser deps (--skip-deps)"
    fi
    
    if [ "$SKIP_BROWSER" -eq 1 ]; then
        log "==> [5/6] Skip unduh browser (--skip-browser)"
    else
        log "==> [5/6] Unduh browser camoufox (~100MB, sekali saja)"
        $PY_RUN -m camoufox fetch || {
            err "Gagal unduh camoufox. Jalankan manual: $PY_RUN -m camoufox fetch"
            log "    Atau jalankan dengan --skip-browser dan unduh nanti"
        }
    fi
}

# ---- 6. Buat launcher ----------------------------------------------------
create_launchers() {
    log "==> [6/6] Buat launcher ./tokenharbor & ./grok"
    
    PYTHON_PATH="$PY_RUN"
    if [ "$NO_VENV" -eq 1 ]; then
        PYTHON_PATH="$PY"
    fi
    
    cat > th <<EOF
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
exec "$PYTHON_PATH" "\$(dirname "\$0")/th-create.py" "\$@"
EOF
    
    chmod +x th th-create.py th_lib.py th-import.py th-verify.py th-freeplan.py 2>/dev/null || true
}

# ---- 7. Verifikasi -------------------------------------------------------
verify_install() {
    log "==> Verifikasi instalasi"
    
    if "$PY_RUN" -c "import camoufox, curl_cffi, requests; print('  ✓ Modul: camoufox curl_cffi requests')" 2>/dev/null; then
        :
    else
        log "  ⚠ Verifikasi import gagal (lihat error di atas)"
    fi
    
    if "$PY_RUN" th-create.py --help >/dev/null 2>&1; then
        log "  ✓ th-create.py siap dipakai"
    fi
    
    if "$PY_RUN" th-import.py --help >/dev/null 2>&1; then
        log "  ✓ th-import.py siap dipakai"
    fi
}

# ---- Main execution ------------------------------------------------------
install_system_deps
find_python
setup_venv
install_browser_deps
create_launchers
verify_install

log ""
log "======================================================================"
log " ✅ Instalasi selesai!"
log ""
log " Cara pakai:"
log "   ./th --count 1 --label router-prod"
log "   ./th --count 5 --fast              # mode cepat"
log "   ./th --count 10 --threads 3        # mode parallel (3x cepat!)"
log "   python3 th-import.py              # import key ke 9router"
log "   python3 th-verify.py       # verifikasi email"
log ""
log " File hasil:"
log "   tokenharbor_keys.txt      - API keys Token Harbor"
log ""
log " ⚡ Speed modes:"
log "   --fast    : jeda 30s antar akun"
log "   --turbo   : jeda 15s (risiko rate-limit)"
log "   --threads : parallel processing (3-5x lebih cepat)"
log ""
log " OS: $OS_TYPE | Distro: $DISTRO | Arch: $ARCH_LBL"
log " Python: $($PY_RUN -V 2>&1)"
log "======================================================================"
