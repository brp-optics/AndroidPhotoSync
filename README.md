# AndroidPhotoSync
A CLI tool for sync of photos, downloads, and recordings from Android phone(s) to Linux via ADB. Supports two phones merging into one photo library with automatic reverse-syncing of file sorting that happens on Linux. File deletions on computer are not synced in either direction, and deleted files aren't filled in again. Requires python, uv, and adb enabled on each device. Test harness requires pillow and pytest.

## Features

- **Automatic sync** when phone is plugged in via USB (via udev + systemd)
- **Multiple phones, one library** — photos merge into date-based folders, deduped by content hash
- **Smart photo organization** — sorted into `YYYY/MM` folders initially using EXIF data or filename dates
- **Move tracking** — sort photos into subfolders on your computer, and the moves propagate to your phone(s)
- **Safe deletes** — deleting from phone(s) doesn't remove from computer; deleting from computer doesn't remove from phones.
- **Collision handling** — files with the same name but different content get device-suffixed names
- **Dry run mode** — preview what would happen without changing anything

## Quick Start

```bash
# Install
git clone <repo> && cd phonesync
./install.sh

# Enable USB debugging on your phone (one-time):
#   Settings → About Phone → tap Build Number 7 times
#   Settings → Developer Options → USB Debugging → ON

# Plug in phone, authorize the connection on the phone screen

# Test
phonesync devices          # verify phone is detected
phonesync sync --dry-run   # preview what would be synced
phonesync sync             # do it for real
```

## Directory Structure

```
~/PhoneSync/
├── photos/                    ← both phones merge here
│   ├── 2025/
│   │   ├── 01/
│   │   │   ├── IMG_20250115_123456.jpg
│   │   │   └── vacation/     ← you can create subfolders to sort!
│   │   └── 02/
│   └── unsorted/              ← photos without parseable dates
├── downloads/
│   ├── pixel-8/               ← separated by device
│   └── galaxy-s24/
├── recordings/
│   ├── pixel-8/
│   └── galaxy-s24/
└── .phonesync/
    ├── config.json
    ├── state-pixel-8.json
    └── state-galaxy-s24.json
```

## Commands

| Command | Description |
|---------|-------------|
| `phonesync sync` | Sync all connected phones |
| `phonesync sync -d SERIAL` | Sync a specific phone |
| `phonesync sync --dry-run` | Preview without changing anything |
| `phonesync status` | Show sync status and stats |
| `phonesync devices` | List connected ADB devices |
| `phonesync config` | Show current config |
| `phonesync config --init` | Initialize config and directories |
| `phonesync reset-state` | Reset state (forces full re-scan) |

## Workflow

### 1. Initial Sync
Plug in phone → phonesync copies all photos/downloads/recordings to your computer.

### 2. Sort on Computer
Move photos into subfolders as you like:
```
photos/2025/01/IMG_001.jpg  →  photos/2025/01/vacation/IMG_001.jpg
```

### 3. Next Sync
PhoneSync detects the move and mirrors it on the phone:
```
/sdcard/DCIM/Camera/IMG_001.jpg  →  /sdcard/DCIM/Camera/vacation/IMG_001.jpg
```

### 4. Phone Cleanup
Delete files from phone to save space — they stay safe on your computer.

## Configuration

Edit `~/PhoneSync/.phonesync/config.json`:

```json
{
  "base_dir": "/home/you/PhoneSync",
  "photo_date_folders": true,
  "delete_from_phone_after_sync": false,
  "propagate_computer_deletes_to_phone": false,
  "conflict_resolution": "prefer_computer",
  "devices": {
    "SERIAL123": {
      "name": "pixel-8",
      "model": "Pixel 8",
      "sources": {
        "photos": [
          "/sdcard/DCIM/Camera",
          "/sdcard/DCIM/Screenshots",
          "/sdcard/Pictures"
        ],
        "downloads": ["/sdcard/Download"],
        "recordings": ["/sdcard/Recordings"]
      }
    }
  }
}
```

### Adding custom source directories

If your phone stores files in non-standard locations, edit the `sources` for that device in the config.

## Auto-Sync Service

The installer sets up:
- **udev rule** (`/etc/udev/rules.d/99-phonesync.rules`) — detects when an Android phone is plugged in
- **systemd service** (`/etc/systemd/system/phonesync.service`) — runs `phonesync sync` automatically

### Check logs
```bash
journalctl -u phonesync -f          # follow live
journalctl -u phonesync --since today  # today's logs
```

### Troubleshoot auto-sync
```bash
# Check if service ran
systemctl status phonesync

# Manually trigger
sudo systemctl start phonesync

# Check udev rule
udevadm monitor --subsystem-match=usb   # then plug in phone

# Verify your phone's vendor ID is in the udev rule
lsusb   # find your phone's ID, add to 99-phonesync.rules if missing
```

## How It Works

### Deduplication
Files are tracked by SHA256 hash. If both phones have the same photo (e.g., shared via messaging), it's only stored once on the computer.

### Move Detection
Each sync, PhoneSync checks if files it previously synced still exist at their expected paths. If a file is missing but found elsewhere (by hash), it's recorded as a move.

### Conflict Resolution
If the same file is moved to different locations on both phone and computer, the computer's location wins (configurable via `conflict_resolution`).

## Dependencies

- `adb` (Android Debug Bridge) — `sudo apt install adb`
- Python 3.8+
- Pillow (optional, for EXIF date parsing) — `pip install pillow`

## Uninstall

```bash
sudo rm /usr/local/bin/phonesync
sudo rm /etc/udev/rules.d/99-phonesync.rules
sudo rm /etc/systemd/system/phonesync.service
sudo udevadm control --reload-rules
sudo systemctl daemon-reload
# Your synced files in ~/PhoneSync remain untouched
```

### Bugs / improvements:
- Configurable location for each source directory?
