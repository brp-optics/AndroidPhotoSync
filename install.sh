#!/bin/bash
#
# install.sh — Install phonesync CLI tool and auto-sync service
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(whoami)"
CURRENT_HOME="$HOME"

echo "======================================="
echo "  PhoneSync Installer"
echo "======================================="
echo ""

# Check for adb
if ! command -v adb &>/dev/null; then
    echo "Installing adb..."
    sudo apt update && sudo apt install -y adb
else
    echo "✓ adb is installed"
fi

# Check for python3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required but not found."
    exit 1
else
    echo "✓ python3 is installed ($(python3 --version))"
fi

# Check for pillow (optional, for EXIF parsing)
if python3 -c "import PIL" 2>/dev/null; then
    echo "✓ Pillow is installed (EXIF date parsing enabled)"
else
    echo "⚠ Pillow not installed. Installing..."
    pip3 install pillow --break-system-packages 2>/dev/null || pip3 install pillow
    echo "✓ Pillow installed"
fi

# Install the CLI tool
echo ""
echo "Installing phonesync to /usr/local/bin/..."
sudo cp "$SCRIPT_DIR/phonesync" /usr/local/bin/phonesync
sudo chmod +x /usr/local/bin/phonesync
echo "✓ phonesync installed"

# Initialize config
echo ""
echo "Initializing PhoneSync directory..."
phonesync config --init
echo "✓ PhoneSync initialized at ~/PhoneSync"

# Install udev rule
echo ""
echo "Installing udev rule for auto-detection..."
sudo cp "$SCRIPT_DIR/99-phonesync.rules" /etc/udev/rules.d/99-phonesync.rules
sudo udevadm control --reload-rules
echo "✓ udev rule installed"

# Install systemd service (with user substitution)
echo ""
echo "Installing systemd service..."
sed "s/CHANGE_ME/$CURRENT_USER/g" "$SCRIPT_DIR/phonesync.service" | \
    sudo tee /etc/systemd/system/phonesync.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable phonesync.service
echo "✓ systemd service installed and enabled"

# Add user to plugdev group (for USB access)
if groups "$CURRENT_USER" | grep -q plugdev; then
    echo "✓ User already in plugdev group"
else
    sudo usermod -aG plugdev "$CURRENT_USER"
    echo "✓ Added $CURRENT_USER to plugdev group"
fi

echo ""
echo "======================================="
echo "  Installation Complete!"
echo "======================================="
echo ""
echo "Next steps:"
echo "  1. Enable USB debugging on your phone(s):"
echo "     Settings → About Phone → tap Build Number 7 times"
echo "     Settings → Developer Options → USB Debugging → ON"
echo ""
echo "  2. Plug in your phone and authorize the connection"
echo ""
echo "  3. Test with:  phonesync devices"
echo "     Then:       phonesync sync --dry-run"
echo "     Then:       phonesync sync"
echo ""
echo "  4. Sync will now auto-trigger when you plug in a phone!"
echo ""
echo "  5. Check logs:  journalctl -u phonesync -f"
echo ""
echo "Config:    ~/PhoneSync/.phonesync/config.json"
echo "Photos:    ~/PhoneSync/photos/"
echo "Downloads: ~/PhoneSync/downloads/<device-name>/"
echo "Recordings:~/PhoneSync/recordings/<device-name>/"
