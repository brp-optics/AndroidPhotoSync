# AndroidPhotoSync

A single-file CLI tool that syncs photos, downloads, and recordings from one or two Android phones to a Linux computer over ADB (USB debugging). Two phones merge into one photo library, and when you sort photos into subfolders on the computer those moves are mirrored back to the phone. Deletions are never propagated in either direction, and a file you delete on the computer is not re-downloaded.

This is a **one-way ingest** tool: file contents only ever flow **phone → computer**. The tool never deletes anything from a phone, and never deletes from the computer in response to a phone-side change.

## Features

- **Multiple phones, one library** — photos from both phones merge into date-based folders; every copy is kept (duplicates are never silently dropped)
- **Photo organization** — photos sorted into `photos/YYYY/` folders using EXIF data or a date parsed from the filename; photos with no usable date go to `photos/unsorted/`
- **Move tracking** — sort photos into subfolders on your computer and the moves propagate to your phone(s)
- **Safe by default** — deleting from a phone never removes the computer copy; deleting from the computer never removes the phone copy; pulls are hash-verified against the phone to catch corrupt transfers; state is backed up before every write
- **Collision handling** — two different files that need the same name in the same folder get device-suffixed names (e.g. `IMG_001_galaxy-s24.jpg`)
- **Dry-run mode** — preview exactly what would happen, including destination paths, without changing anything

## Requirements

- `adb` (Android Debug Bridge) — `sudo apt install adb`
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) to run it and manage dependencies
- Pillow (for EXIF date parsing) — declared in `pyproject.toml`, installed automatically by `uv`
- USB debugging enabled on each phone

The test suite additionally needs `pytest` (declared as a dev dependency). It can be run with `uv run pytest` or with the bundled `run_tests.py`.

## Quick Start

```bash
git clone <repo> && cd phonesync

# Enable USB debugging on each phone (one-time):
#   Settings → About Phone → tap Build Number 7 times
#   Settings → Developer Options → USB Debugging → ON

# Plug in the phone and authorize the connection on the phone screen.

uv run phonesync.py devices            # verify the phone is detected
uv run phonesync.py config --init      # create config + data directories
uv run phonesync.py detect-paths --apply  # auto-detect media dirs into config
uv run phonesync.py sync --dry-run     # preview what would be synced
uv run phonesync.py sync               # do it for real
```

> The examples above invoke `phonesync.py` directly with `uv run`. To get a real `phonesync` command on your `PATH` (so you can drop the `uv run` prefix and the `.py`), run `./contrib/install.sh` — it installs the project with uv and drops a launcher at `/usr/local/bin/phonesync`. Add `--with-service` to also set up the experimental auto-sync (see [Planned](#planned--not-yet-implemented)).

## Directory Structure

Data and configuration live in **two separate directories**:

```
~/PhoneSync/                       ← data directory (your files)
├── photos/                        ← both phones merge here
│   ├── 2025/                      ← sorted by year (EXIF or filename date)
│   │   ├── IMG_20250115_123456.jpg
│   │   └── vacation/              ← create subfolders to sort; moves sync back
│   └── unsorted/                  ← photos without a parseable date
|       └── IMG_001.jpg            ← (that means no date in filename, EXIF, or mtime)
├── downloads/
│   ├── pixel-8/                   ← separated by device
│   └── galaxy-s24/
└── recordings/
    ├── pixel-8/
    └── galaxy-s24/

~/.phonesync/                      ← config directory
├── config.json
├── library-index.json             ← cached content hashes of the library
├── known-devices.json             ← devices you've approved for syncing
├── state-pixel-8.json             ← per-device sync state
├── state-galaxy-s24.json
└── state-backups/                 ← timestamped state backups (last 10)
```

Photos are sorted by **year only** (`photos/YYYY/`), not `YYYY/MM`.

## Commands

| Command | Description |
|---------|-------------|
| `phonesync sync` | Sync all connected phones |
| `phonesync sync -d SERIAL` | Sync a specific phone by ADB serial |
| `phonesync sync -n` / `--dry-run` | Preview without changing anything |
| `phonesync sync --read-only` | Sync but never write to the phone (skip move propagation) |
| `phonesync sync --overwrite-policy {ask,never,always}` | How to handle a file edited on both sides (overrides config for this run) |
| `phonesync status` | Show config/data directories and per-device sync stats |
| `phonesync devices` | List connected ADB devices, storage volumes, and approval status |
| `phonesync devices --approve SERIAL` | Pre-approve a device for syncing (needed for unattended/auto-sync) |
| `phonesync devices --forget SERIAL` | Remove a device from the approved list |
| `phonesync detect-paths` | Auto-detect media directories on connected phone(s) |
| `phonesync detect-paths --apply` | Apply detected paths to the config |
| `phonesync config` | Show current config |
| `phonesync config --init` | Initialize config and directories |
| `phonesync config --config-dir DIR` / `--data-dir DIR` | Use non-default locations |
| `phonesync reset-state` | Reset sync state (forces a full re-scan next sync) |
| `phonesync prune-state` | Remove stale state entries (keeps tombstones) |
| `phonesync prune-state --clear-tombstones` | Also drop tombstones (deleted files will re-download) |
| `phonesync prune-state --rehash` | Recompute file hashes (slow but thorough) |

Add `-v` / `--verbose` for debug logging. `sync` exits non-zero if a device scan fails mid-run.

## Workflow

### 1. Initial sync
Plug in the phone and run `sync`; PhoneSync copies all photos, downloads, and recordings to the computer.

The **first** time you sync a given device, PhoneSync pauses and asks you to confirm before pulling anything — showing the device and roughly how many files (and how much data) it's about to copy. This is a guard against accidentally ingesting a huge library from the wrong device. Once you confirm, the device is remembered (in `known-devices.json`) and won't ask again. If there's no terminal to prompt at (an unattended/automated run), an unapproved device is **skipped rather than synced** — approve it ahead of time with `phonesync devices --approve SERIAL`.

### 2. Sort on the computer
Move photos into subfolders however you like:
```
photos/2025/IMG_20250115_123456.jpg  →  photos/2025/vacation/IMG_20250115_123456_Yosemite.jpg
```

Delete photos you don't want to keep:
```
rm photos/unsorted/IMG_001.jpg
```

### 3. Next sync
PhoneSync detects the move and mirrors it on the phone:
```
/sdcard/DCIM/Camera/IMG_20250115_123456.jpg  →  /sdcard/DCIM/Camera/vacation/IMG_20250115_123456_Yosemite.jpg
```
PhoneSync detects the deletion and marks the file as "not to sync again", but does not delete on the phone.

### 4. Phone cleanup
Delete files from the phone to free space — they stay on the computer, and the tool will not re-download them.

Sort files on the phone, and their movements will be synced to the computer next sync.

## Configuration

Edit `~/.phonesync/config.json`:

```json
{
  "config_dir": "/home/you/.phonesync",
  "data_dir": "/home/you/PhoneSync",
  "photo_date_folders": true,
  "recursive_scan": true,
  "preserve_phone_subdirs": true,
  "verify_pulls": true,
  "use_library_index": true,
  "read_only": false,
  "check_free_space": true,
  "free_space_margin_bytes": 104857600,
  "overwrite_policy": "ask",
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

`phonesync config --init` writes a starting config, and `phonesync detect-paths --apply` fills in sensible `sources` for each connected device. If your phone stores files in non-standard locations, edit `sources` for that device.

### What the options mean

| Option | Default | Effect |
|--------|---------|--------|
| `photo_date_folders` | `true` | Sort photos into `photos/YYYY/` by EXIF/filename date. `false` puts them flat under `photos/`. |
| `recursive_scan` | `true` | Scan subdirectories of each source. `false` scans only the top level. |
| `preserve_phone_subdirs` | `true` | Mirror the phone's subfolder structure under `downloads/`/`recordings/` and under the photo year folder. |
| `verify_pulls` | `true` | After each pull, compare the copied bytes against the phone's own hash and discard the copy if they differ. Catches truncated transfers. Leave on. |
| `use_library_index` | `true` | Cache content hashes of the library so syncs are faster and an interrupted move is recognized instead of re-pulled. |
| `read_only` | `false` | Never write to the phone — skips the move-propagation step entirely. Reads/ingest still happen. Useful for a first cautious sync. |
| `check_free_space` | `true` | Before pulling, estimate the bytes to copy and abort (without copying anything) if they wouldn't fit. |
| `free_space_margin_bytes` | `104857600` (100 MB) | Headroom required on top of the estimate before a sync proceeds. |
| `overwrite_policy` | `"ask"` | What to do when a file was edited on **both** the phone and the computer since the last sync. `ask` prompts (and keeps local if there's no terminal); `never` always keeps your local edits; `always` always takes the phone version. Can be set per-file (see below). |
| `exclude_dirs` | (built-in list) | Folder names to skip everywhere (`.thumbnails`, `.trash`, caches…). **This is how you tell PhoneSync to ignore files**. |
| `exclude_files` | (built-in list) | Filename patterns to skip (`.nomedia`, `Thumbs.db`…). |

`exclude_dirs` and `exclude_files` default to built-in lists; add entries in the config to extend them.

### Edited on both sides: overwrite_policy and overwrite protection

Normally, if you edit a photo on the phone, the next sync re-pulls it and updates the computer copy. But if you *also* edited that file **on the computer** since the last sync (cropped it, fixed metadata, etc.), re-pulling would throw away your local edits. PhoneSync detects this — it compares the file's current on-disk hash against the hash it recorded at the last sync — and applies `overwrite_policy` only in that both-sides-changed case:

- `ask` (default) — prompt you. The prompt offers *overwrite once*, *keep local once*, *always overwrite this file*, or *never overwrite this file*; the last two are remembered for that specific file. **If there's no terminal** (a cron/udev-triggered run), `ask` keeps your local edits rather than guessing — automation never silently destroys local work.
- `never` — always keep your local edits; the phone version is not applied.
- `always` — always take the phone version.

Per-file choices ("always"/"never" from a prompt) are stored in that file's state entry as `overwrite_policy` and take precedence over the global setting. Files edited on only one side are unaffected — a phone-only change updates the computer copy as usual, and a computer-only change is just left alone.

### Reserved keys (not yet implemented)

These keys may appear in a config and are accepted, but the current code does **not** act on them. They are placeholders for possible future features:

| Key | Intended (future) meaning |
|-----|---------------------------|
| `delete_from_phone_after_sync` | Delete a file from the phone once it's safely on the computer. |
| `propagate_computer_deletes_to_phone` | Mirror a computer-side deletion back to the phone. |
| `conflict_resolution` | Choose the winner when a file is moved on both sides. |

Today the tool **never deletes from the phone** regardless of the first two, and a move-on-both-sides conflict always resolves to the **computer's** location.

## How It Works

### Duplicates: everything is kept

Because content only flows phone → computer, every duplicate that exists is one *you* created on purpose: the same photo saved to two albums, a picture that landed on both phones (they're backups of each other), an app-state backup, and so on. PhoneSync treats all of these as intentional and **keeps every copy**. It never deletes or silently skips a file because its contents match another file.

To *ignore* certain files, do it by **folder**, not by content — add the folder to `exclude_dirs` (this is how `.thumbnails`, `.trash`, and other caches are already skipped). "Ignore this location" is meaningful; "ignore these bytes because they repeat" is not, since the repetition is deliberate.

Content hashes (SHA256) are still recorded, but only to do safe work that never loses data:
- **Move detection** — recognizing that a file you sorted into a subfolder is the same file, so it isn't re-copied.
- **Pull verification** — confirming each pulled file matches the phone's own hash, catching truncated transfers.
- **Completing an interrupted move** — if a move to the phone half-finished on a previous run, the leftover copy is recognized instead of being re-pulled as a "new" file.

### Move detection
Each sync, PhoneSync checks whether files it previously synced still exist at their expected phone paths. If a tracked file is gone from its path but its content is found at another path, that's recorded as a move (the file isn't re-downloaded). When several untracked copies share the same content, the tool uses filename and modification-time hints to pick the move target, and refuses to guess if they're genuinely ambiguous.

### Conflict resolution
If the same file is moved to different locations on both the phone and the computer, the computer's location wins. (This is currently fixed behavior; see the reserved `conflict_resolution` key above.)

### Safety
- Pulls are verified against the phone's hash; a corrupt/truncated transfer is discarded and retried next sync.
- Each device's state file is backed up (timestamped, last 10 kept) before every write.
- If a phone becomes unreachable mid-scan, the run aborts loudly without saving state, rather than acting on a partial view that could mistreat files it couldn't see.
- Before pulling, a free-space check estimates the copy and aborts up front if it wouldn't fit, instead of failing partway with files half-copied.
- A file edited on both the phone and the computer is never silently overwritten; see [overwrite protection](#edited-on-both-sides-overwrite-protection). Automated (non-interactive) runs keep the local copy by default.
- A brand-new device must be explicitly approved before its first sync; unattended runs refuse to sync an unapproved device (which also scopes any auto-sync to devices you've OK'd).
- `read_only` / `--read-only` disables every phone-side write for a cautious sync.

## Running the tests

```bash
uv run pytest                 # via pytest
python3 run_tests.py          # or the bundled runner (must NOT be run as root)
```

The tests use a local-filesystem fake for ADB, so no phone is needed. The runner refuses to run as root because root bypasses the file-permission bits that some tests rely on.

## Planned / not yet implemented

These are described here as goals, not current behavior:

- **Auto-sync on plug-in** — a udev rule + systemd service to run `sync` automatically when a phone is connected. An **experimental** uv-based installer is included (`./install.sh --with-service`); it installs the launcher, a systemd unit, and a udev rule. It's opt-in and still rough. The approved-devices registry provides the key safety scoping regardless: an unattended sync only touches devices you've approved with `phonesync devices --approve`, so an unknown phone plugged in is skipped, not synced. Setup is manual — see below.

### Setting up auto-sync (experimental)

The udev rule does **not** match all phones by default — that would let a sync fire on any Android device anyone plugs in. You scope it to your own phone. Steps:

1. **Install with the service:**
   ```bash
   ./contrib/install.sh --with-service
   ```
   This installs the launcher, the systemd unit, and `/etc/udev/rules.d/99-phonesync.rules` — but every match line in that rule starts out commented, so nothing triggers yet.

2. **Approve the phone** (so an unattended sync is allowed to touch it at all):
   ```bash
   phonesync devices                 # lists each connected device's serial + approval status
   phonesync devices --approve SERIAL
   ```

3. **Find the phone's USB serial.** This is the `iSerial` the udev rule matches on (usually the same string shown by `phonesync devices`, but confirm at the USB layer):
   ```bash
   lsusb                             # find your phone's "Bus 00X Device 0YY: ID vvvv:pppp"
   udevadm info -a -n /dev/bus/usb/00X/0YY | grep -m1 ATTRS{serial}
   ```
   The `ID vvvv:pppp` part is the vendor:product (`idVendor`:`idProduct`); the `ATTRS{serial}` line is the USB serial.

4. **Edit the rule** at `/etc/udev/rules.d/99-phonesync.rules`. Uncomment the serial line and fill in your value (preferred — most specific):
   ```
   ACTION=="add", SUBSYSTEM=="usb", ATTRS{serial}=="YOUR_PHONE_SERIAL", TAG+="systemd", ENV{SYSTEMD_WANTS}="phonesync.service"
   ```
   If your phone doesn't expose a stable USB serial, uncomment the vendor+product line instead (matches every unit of that exact model, still far narrower than vendor-only):
   ```
   ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", ATTR{idProduct}=="YOUR_PRODUCT_ID", TAG+="systemd", ENV{SYSTEMD_WANTS}="phonesync.service"
   ```

5. **Reload udev and test:**
   ```bash
   sudo udevadm control --reload-rules
   udevadm monitor --subsystem-match=usb   # then plug in the phone and watch
   journalctl -u phonesync -f               # follow the sync's logs
   ```

Notes: the rule matches on the **USB hardware serial**, which is set by `--approve` independently — they're separate lists, so a device must be in *both* (udev rule + approved registry) for an unattended sync to run. Multiple phones each need their own uncommented match line. The first sync of a device still has to be done by hand (an unapproved device is refused non-interactively), so approve and do one manual `phonesync sync` before relying on auto-sync.

- **`phonesync doctor`** — verify that the phone's `find`/`stat`/`printf` scan command behaves as expected on the device's toybox before trusting a large sync.
- The reserved config keys listed above.

## Notes / ideas / future features

- Per-source-directory configurable destinations.
- Multiple destination directories (currently all phones map to subfolders of the same PhoneSync directory)
- Progress bar / time estimation (and early abort?)

