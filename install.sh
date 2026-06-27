#!/usr/bin/env bash
#
# install.sh — Install the phonesync launcher (and, optionally, the
# udev + systemd auto-sync service) using uv for dependency management.
#
# This installs:
#   - the project files to ~/.local/share/phonesync
#   - a launcher at /usr/local/bin/phonesync that runs it via `uv run`
#   - (optional, with --with-service) a udev rule + systemd unit that runs
#     `phonesync sync` when an APPROVED phone is connected
#
# Dependencies are resolved by uv from pyproject.toml (Pillow, etc.).
# No pip, no --break-system-packages, no system Python pollution.
#
set -euo pipefail

WITH_SERVICE=0
for arg in "$@"; do
    case "$arg" in
        --with-service) WITH_SERVICE=1 ;;
        -h|--help)
            echo "Usage: ./install.sh [--with-service]"
            echo "  --with-service   also install the udev + systemd auto-sync (experimental)"
            exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The project root is the parent of contrib/ (where phonesync.py lives).
PROJECT_SRC="$(cd "$SCRIPT_DIR/.." && pwd)"
CURRENT_USER="$(whoami)"
INSTALL_DIR="$HOME/.local/share/phonesync"
LAUNCHER="/usr/local/bin/phonesync"

echo "======================================="
echo "  PhoneSync Installer (uv)"
echo "======================================="
echo ""

# --- Prerequisites -----------------------------------------------------------

# adb
if command -v adb &>/dev/null; then
    echo "✓ adb is installed"
else
    echo "Installing adb..."
    sudo apt-get update && sudo apt-get install -y adb
fi

# uv (per-user install if missing; never sudo)
if command -v uv &>/dev/null; then
    echo "✓ uv is installed ($(uv --version))"
else
    echo "uv not found. Installing it for the current user..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.local/bin; make it available for the rest of this run.
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv &>/dev/null; then
        echo "ERROR: uv installation did not put 'uv' on PATH." >&2
        echo "  Add ~/.local/bin to your PATH and re-run." >&2
        exit 1
    fi
    echo "✓ uv installed"
fi
UV_BIN="$(command -v uv)"

# --- Install the project -----------------------------------------------------

echo ""
echo "Installing project to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$PROJECT_SRC/phonesync.py" "$INSTALL_DIR/"
cp "$PROJECT_SRC/pyproject.toml" "$INSTALL_DIR/"
# Resolve the (empty) base environment first — this always succeeds offline-
# friendly since there are no required runtime deps.
( cd "$INSTALL_DIR" && "$UV_BIN" sync --no-dev )
# Pillow (EXIF date reading) is an OPTIONAL extra. Try to add it, but don't
# fail the install if it can't be fetched — the tool runs without it and
# falls back to filename/mtime dating.
if ( cd "$INSTALL_DIR" && "$UV_BIN" sync --no-dev --extra exif ); then
    echo "✓ Project installed with EXIF support (Pillow)"
else
    echo "⚠ Could not install Pillow; continuing without EXIF date reading."
    echo "  Photo dates will use the filename, then the phone's timestamp."
    echo "  Add it later with: ( cd $INSTALL_DIR && uv sync --extra exif )"
fi

# --- Launcher ----------------------------------------------------------------

echo ""
echo "Installing launcher at $LAUNCHER ..."
TMP_LAUNCHER="$(mktemp)"
cat > "$TMP_LAUNCHER" << LAUNCHEOF
#!/usr/bin/env bash
# phonesync launcher — runs the installed project via uv.
exec "$UV_BIN" run --project "$INSTALL_DIR" "$INSTALL_DIR/phonesync.py" "\$@"
LAUNCHEOF
chmod +x "$TMP_LAUNCHER"
sudo mv "$TMP_LAUNCHER" "$LAUNCHER"
echo "✓ Launcher installed (run: phonesync ...)"

# --- Initialize config -------------------------------------------------------

echo ""
echo "Initializing configuration..."
"$LAUNCHER" config --init
echo "✓ Config initialized at ~/.phonesync/"

# --- Optional: auto-sync service --------------------------------------------

if [[ "$WITH_SERVICE" -eq 1 ]]; then
    echo ""
    echo "--- Installing auto-sync service (experimental) ---"
    echo ""
    echo "NOTE: auto-sync only ever touches devices you've APPROVED."
    echo "      A new device plugged in is ignored until you run:"
    echo "          phonesync devices --approve <SERIAL>"
    echo ""

    # systemd unit, with the user and uv path substituted in.
    TMP_UNIT="$(mktemp)"
    sed -e "s|CHANGE_ME|$CURRENT_USER|g" \
        -e "s|@UV_BIN@|$UV_BIN|g" \
        -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
        "$SCRIPT_DIR/phonesync.service" > "$TMP_UNIT"
    sudo mv "$TMP_UNIT" /etc/systemd/system/phonesync.service
    sudo systemctl daemon-reload
    echo "✓ systemd unit installed"

    sudo cp "$SCRIPT_DIR/99-phonesync.rules" /etc/udev/rules.d/99-phonesync.rules
    sudo udevadm control --reload-rules
    echo "✓ udev rule installed"

    # USB access group
    if groups "$CURRENT_USER" | grep -qw plugdev; then
        echo "✓ User already in plugdev group"
    else
        sudo usermod -aG plugdev "$CURRENT_USER"
        echo "✓ Added $CURRENT_USER to plugdev (log out/in for it to take effect)"
    fi
    echo ""
    echo "Auto-sync installed. Approve each phone you want synced:"
    echo "    phonesync devices            # see serials + approval status"
    echo "    phonesync devices --approve <SERIAL>"
fi

# --- Done --------------------------------------------------------------------

echo ""
echo "======================================="
echo "  Installation Complete"
echo "======================================="
echo ""
echo "Next steps:"
echo "  1. Enable USB debugging on each phone (one-time):"
echo "       Settings → About Phone → tap Build Number 7 times"
echo "       Settings → Developer Options → USB Debugging → ON"
echo "  2. Plug in the phone and authorize the connection on its screen."
echo "  3. phonesync devices          # verify it's detected"
echo "     phonesync sync --dry-run   # preview"
echo "     phonesync sync             # first run asks you to approve the device"
echo ""
echo "Config:     ~/.phonesync/config.json"
echo "Photos:     ~/PhoneSync/photos/"
echo "Downloads:  ~/PhoneSync/downloads/<device-name>/"
echo "Recordings: ~/PhoneSync/recordings/<device-name>/"
if [[ "$WITH_SERVICE" -eq 1 ]]; then
    echo "Logs:       journalctl -u phonesync -f"
fi
