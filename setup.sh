#!/usr/bin/env bash
# setup.sh — Debian/Ubuntu setup for jd2021-map-installer
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { echo "[INFO] $*"; }
ok()      { echo "[OK]   $*"; }
warn()    { echo "[WARNING] $*"; }
err()     { echo "[ERROR] $*" >&2; }
die()     { err "$@"; exit 1; }

# ---------------------------------------------------------------------------
# 1) System dependencies
# ---------------------------------------------------------------------------
info "Installing system dependencies..."
PACKAGES=(git python3 python3-venv python3-pip ffmpeg curl jq unzip tar libicu-dev chromium-browser)

if command -v sudo &>/dev/null; then
    sudo apt-get update -y
    sudo apt-get install -y "${PACKAGES[@]}"
else
    warn "sudo not found — attempting apt-get as current user."
    apt-get update -y
    apt-get install -y "${PACKAGES[@]}"
fi
ok "System dependencies installed."
info "FFmpeg is installed via apt as part of system dependencies."

# ---------------------------------------------------------------------------
# 2) Python virtual environment
# ---------------------------------------------------------------------------
info "Creating Python virtual environment..."
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
ok "Virtual environment created and activated."

info "[1/5] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt
ok "Python dependencies installed."

# ---------------------------------------------------------------------------
# 3) Playwright
# ---------------------------------------------------------------------------
echo
info "[2/5] Installing Chromium for Playwright..."
python3 -m playwright install chromium || warn "playwright install chromium failed (non-fatal, may need system chromium)"
# Install system deps required by Playwright's bundled Chromium.
if command -v sudo &>/dev/null; then
    sudo python3 -m playwright install-deps chromium || warn "playwright install-deps failed (non-fatal)."
else
    python3 -m playwright install-deps chromium || warn "playwright install-deps failed (non-fatal)."
fi
ok "Playwright Chromium installed."

# ---------------------------------------------------------------------------
# 4) AssetStudioModCLI (Linux) & .NET 9
# ---------------------------------------------------------------------------
echo
info "[3/5] Installing .NET 9 & Staging AssetStudioModCLI..."

if command -v dotnet &>/dev/null && dotnet --list-runtimes | grep -q "Microsoft.NETCore.App 9"; then
    ok ".NET 9 Runtime is already installed."
else
    info "Downloading Microsoft dotnet-install.sh..."
    curl -fsSL https://dot.net/v1/dotnet-install.sh -o dotnet-install.sh
    chmod +x dotnet-install.sh
    
    info "Installing .NET 9 Runtime..."
    if command -v sudo &>/dev/null; then
        sudo ./dotnet-install.sh --channel 9.0 --runtime dotnet --install-dir /usr/share/dotnet
        sudo ln -sf /usr/share/dotnet/dotnet /usr/bin/dotnet
    else
        ./dotnet-install.sh --channel 9.0 --runtime dotnet
        warn "Installed .NET without sudo. You may need to add ~/.dotnet to your PATH."
    fi
    rm -f dotnet-install.sh
    ok ".NET 9 Runtime installed."
fi

mkdir -p tools/AssetStudioModCLI

CLI_BIN="tools/AssetStudioModCLI/AssetStudioModCLI"

if [ -x "$CLI_BIN" ]; then
    ok "AssetStudioModCLI already present at $CLI_BIN"
else
    ASSET_API="https://api.github.com/repos/aelurum/AssetStudio/releases/latest"
    info "Fetching latest release from $ASSET_API ..."

    RELEASE_JSON=$(curl -fsSL -H "User-Agent: jd2021-map-installer-setup" "$ASSET_API") \
        || die "Failed to fetch AssetStudio release metadata."

    # Look for a Linux CLI asset (tar.gz or zip)
    ASSET_URL=$(echo "$RELEASE_JSON" | jq -r \
        '[.assets[] | select(.name | test("AssetStudio.*(CLI|cmd|console).*linux.*(tar\\.gz|zip)$"; "i"))] | first // empty | .browser_download_url') \
        || true

    if [ -z "$ASSET_URL" ]; then
        # Broader fallback
        ASSET_URL=$(echo "$RELEASE_JSON" | jq -r \
            '[.assets[] | select(.name | test("AssetStudio.*CLI.*linux"; "i"))] | first // empty | .browser_download_url') \
            || true
    fi

    if [ -z "$ASSET_URL" ]; then
        warn "Could not find a Linux AssetStudio CLI release asset."
        warn "JDNext mapPackage extraction may fail until AssetStudioModCLI is staged in tools/AssetStudioModCLI."
    else
        ASSET_NAME=$(basename "$ASSET_URL")
        TMP_DIR="tools/AssetStudioModCLI/_tmp"
        mkdir -p "$TMP_DIR"

        info "Downloading $ASSET_NAME ..."
        curl -fsSL -o "$TMP_DIR/$ASSET_NAME" "$ASSET_URL" \
            || die "Failed to download AssetStudioModCLI archive."

        info "Extracting..."
        case "$ASSET_NAME" in
            *.tar.gz|*.tgz)
                tar -xzf "$TMP_DIR/$ASSET_NAME" -C "$TMP_DIR" \
                    || die "Failed to extract tar.gz archive."
                ;;
            *.zip)
                unzip -o "$TMP_DIR/$ASSET_NAME" -d "$TMP_DIR" \
                    || die "Failed to extract zip archive."
                ;;
            *)
                die "Unsupported archive format: $ASSET_NAME"
                ;;
        esac

        # Locate the binary and copy its sibling files to the target directory
        FOUND_BIN=$(find "$TMP_DIR" -type f -name "AssetStudioModCLI" | head -n1)
        if [ -z "$FOUND_BIN" ]; then
            # Fallback: look for any file with AssetStudioModCLI (case-insensitive)
            FOUND_BIN=$(find "$TMP_DIR" -type f -iname "AssetStudioModCLI" | head -n1)
        fi

        if [ -z "$FOUND_BIN" ]; then
            warn "AssetStudioModCLI binary not found in downloaded archive."
        else
            BIN_DIR=$(dirname "$FOUND_BIN")
            cp -a "$BIN_DIR"/. tools/AssetStudioModCLI/
            chmod +x "tools/AssetStudioModCLI/AssetStudioModCLI"
            ok "AssetStudioModCLI staged at tools/AssetStudioModCLI/AssetStudioModCLI"
        fi

        rm -rf "$TMP_DIR"
    fi
fi

# ---------------------------------------------------------------------------
# 5) vgmstream (Linux)
# ---------------------------------------------------------------------------
echo
info "[4/5] Installing vgmstream toolchain..."
info "[5/5] FFmpeg — already handled via apt in step 1."
mkdir -p tools/vgmstream

VGMSTREAM_BIN="tools/vgmstream/vgmstream-cli"
VGMSTREAM_URL="https://github.com/vgmstream/vgmstream-releases/releases/download/nightly/vgmstream-linux-cli.tar.gz"

if [ -x "$VGMSTREAM_BIN" ]; then
    ok "vgmstream-cli already present at $VGMSTREAM_BIN"
else
    TMP_DIR="tools/vgmstream/_extract"
    mkdir -p "$TMP_DIR"

    info "Downloading vgmstream Linux CLI..."
    curl -fsSL -o "$TMP_DIR/vgmstream-linux-cli.tar.gz" "$VGMSTREAM_URL" \
        || { warn "vgmstream download failed. IPK X360 audio decode may fail."; TMP_DIR=""; }

    if [ -n "$TMP_DIR" ]; then
        tar -xzf "$TMP_DIR/vgmstream-linux-cli.tar.gz" -C "$TMP_DIR" \
            || die "Failed to extract vgmstream archive."

        FOUND_CLI=$(find "$TMP_DIR" -type f -name "vgmstream-cli" | head -n1)
        if [ -z "$FOUND_CLI" ]; then
            warn "vgmstream-cli binary not found in archive."
        else
            CLI_DIR=$(dirname "$FOUND_CLI")
            cp -a "$CLI_DIR"/. tools/vgmstream/
            chmod +x "tools/vgmstream/vgmstream-cli"
            ok "vgmstream installed in tools/vgmstream"
        fi

        rm -rf "$TMP_DIR"
    fi
fi

# ---------------------------------------------------------------------------
# 6) Generate run.sh
# ---------------------------------------------------------------------------
echo
info "Generating run.sh..."
cat > run.sh << 'RUNEOF'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source venv/bin/activate
python3 -m jd2021_installer.main
RUNEOF
chmod +x run.sh
ok "run.sh generated."

echo
echo "Setup complete!"
