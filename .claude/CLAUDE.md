# run-app — iOS App Launcher

**CLI Source:** `/Users/moshe/Apps/run-app/run.py`
**Menu Bar App:** `/Users/moshe/Apps/run-app/RunApp/` (native Swift macOS app)
**Xcode Project:** `/Users/moshe/Apps/run-app/RunApp.xcodeproj/`
**Symlink:** `/usr/local/bin/run-app` (CLI)
**App list:** `/Users/moshe/Apps/run-app/apps.json` — auto-generated, shared source of truth (see below)
**History:** `~/.cache/run-app-history.json` (shared between CLI and menu bar app)
**Per-app devices:** `~/.cache/run-app-app-devices.json` — `{app: [device_indices]}`, each app's last-used devices (shared; both write it on launch)
**Settings:** `~/.config/run-app/settings.json` (menu bar shortcut settings)

## Per-app device memory

Each app remembers the **device set it last launched on** (`run-app-app-devices.json`,
`{app: [device_indices]}`). Selecting an app defaults to those devices — so
re-running Jukebox after last using "Moshe + Summit" launches to both on a single
Enter, no re-picking. Both interfaces write it on every launch (`save_app_devices`
in the CLI, `AppState.recordDevices` in the app). Lookup falls back to the global
`default_devices` setting, then device 0. In the CLI the device multi-picker opens
**pre-ticked** with the remembered set; in the menu bar, clicking/Enter on an app
launches straight to them (the → device picker also opens pre-selected).

## App discovery (auto — no manual list)

Apps are **auto-discovered**, not hardcoded. `run.py` scans `/Users/moshe/Apps`
for iOS Xcode projects and derives each app's name/path/scheme/bundle from disk:

- **path/project** — first `*.xcodeproj` in the folder or one subdir deep (e.g. `Pixel/ios`), preferring a project named like the folder over stray/test projects.
- **scheme** — shared `.xcscheme` matching the folder/project name, skipping `*Test*`; falls back to the project name.
- **bundle** — `PRODUCT_BUNDLE_IDENTIFIER` from `project.pbxproj`, resolving `$(VAR)` refs and skipping test/extension/widget targets.
- **iOS filter** — Mac-only projects (`SDKROOT=macosx`) are skipped; run-app deploys via simctl/devicectl.
- **`EXCLUDE`** — folders to skip (e.g. `run-app` itself). **`OVERRIDES`** — per-app fixups, almost always just a non-default `log` file path (the one thing not derivable from disk).

`run.py` writes the discovered list to **`apps.json`** on every run. The native
menu-bar app reads that same file (`AppInfo.load()`) — so **dropping a new iOS app
folder in `/Users/moshe/Apps` makes it appear in both the CLI and the menu bar with
zero code edits.** To add an app: just create it. To tweak a derived value or set a
custom log path: edit `OVERRIDES` in `run.py`.

**Refresh timing matters.** The menu bar re-scans on *every popover open*
(`PopupViewModel.refreshApps()` from `showPopover()`): it re-reads `apps.json`
instantly, then runs `python3 run.py --emit-apps` (`AppInfo.rescan()`, ~100ms) on a
background queue and republishes via `@Published appsRevision`. Don't move this back
to launch-only — an app added while RunApp is running would then stay invisible until
restart (this is exactly why `Cards` didn't show up).

## Overview

Two interfaces for building, installing, and launching iOS apps across multiple devices:
1. **CLI** (`run.py`) — Interactive terminal with live log viewer (curses TUI)
2. **Menu Bar App** (`RunApp/`) — Native Swift macOS status bar app with global keyboard shortcuts

## Architecture

### Menu Bar App (Swift — `RunApp/`)

Pure AppKit menu bar app. Three source files:

| File | Purpose |
|------|---------|
| `AppDelegate.swift` | `@main` entry point, NSStatusBar + NSMenu, global hotkey monitor via NSEvent |
| `AppState.swift` | Business logic: history, settings persistence, build/install/launch via Process, notifications via osascript |
| `Models.swift` | Data: `AppInfo` (Codable, loaded from `apps.json` via `load()`/`reload()`), `DeviceInfo`, `GlobalShortcut` |

- **Status item:** `play.fill` SF Symbol in menu bar
- **Menu:** Built dynamically via `NSMenuDelegate.menuNeedsUpdate` — always shows latest state
- **Apps sorted by history**, each with a device submenu
- **Global shortcuts:** Modifier+1-9 launches app N on Moshe's iPhone, Modifier+` opens menu
- **Settings submenu:** Toggle hotkeys, choose modifier key, launch at login (SMAppService)
- **No dock icon:** `NSApp.setActivationPolicy(.accessory)` + `LSUIElement = true`
- **Notifications:** macOS native via osascript `display notification`
- **Build state:** Shows hourglass prefix on building apps, disables device items mid-build
- **Thread safety:** NSLock protects `buildingKeys` set; builds run on `.userInitiated` queue
- Shares history JSON with CLI so both sort apps by recent usage

### CLI (`run.py`)

Single-file Python script. See sections table below.

| Lines | Section | Purpose |
|-------|---------|---------|
| 1-16 | Imports | stdlib only: subprocess, curses, threading, queue, json, etc. |
| 17-40 | Discovery | `EXCLUDE`/`OVERRIDES` + `discover_apps()` → auto-builds `APPS`, writes `apps.json` |
| 36-40 | `DEVICES` | Device registry — name, short tag, CoreDevice UDID, hardware UDID, type |
| 42-56 | Constants | History file path, ANSI escape codes |
| 58-98 | History | Load/save app history + last run for `-last` flag |
| 101-193 | Picker UI | Pre-curses terminal UI — `pick_single()` and `pick_multi()` with keyboard nav |
| 196-218 | Build | `build()` — runs `xcodebuild` for simulator or physical |
| 221-259 | Install & Launch | `install_and_launch()` — per-device install/launch, returns log process |
| 262-278 | Category Regex | Auto-detects `[CATEGORY]` tags from log lines |
| 281-312 | Log Reader | `log_reader()` — thread that reads process stdout, filters syslog noise, writes to log file + queue |
| 315-618 | Log Viewer | `live_log_viewer()` — curses TUI with device/category filters, scroll, copy |
| 621-746 | Stream Mode | `run_stream()` — plain text logs to stdout + file, no curses (for Claude Code / automation) |
| 749-793 | Nolog Mode | `run_nolog()` — detached log processes, script exits immediately |
| 621-793 | Orchestration | `run_with_logs()`, `run_stream()`, and `run_nolog()` — build all, launch all |
| ~800+ | Main / CLI | Arg parsing (`-last`, `-nolog`, `-stream`), interactive picker fallback |

## Device Deployment Pipeline

### Simulator
1. `xcrun simctl boot` → `simctl install` → `simctl launch --console`
2. stdout streams directly into log reader

### Physical Device
1. `xcrun devicectl device install app` (uses CoreDevice UUID)
2. `xcrun devicectl device process launch` (uses CoreDevice UUID)
3. `idevicesyslog -u {hw_udid} --process {app}` for log streaming
4. Log reader filters framework noise (CoreFoundation, RunningBoardServices, etc.) and extracts app messages

### Key Insight: Two UDID Formats
- **CoreDevice UUID** (e.g. `0C449D4B-...`): Used by `xcodebuild`, `devicectl`
- **Hardware UDID** (e.g. `00008150-...`): Used by `ios-deploy`, `idevicesyslog`
- `DEVICES` list stores both as `udid` and `hw_udid`

## Log Viewer (Curses TUI)

### Layout
- **Row 0:** App name + device filter pills (1/2/3 toggle)
- **Row 1:** Category filter summary + pills
- **Row 2:** Separator
- **Rows 3–N-1:** Scrollable log lines with colored device tags
- **Row N:** Footer with keybindings + LIVE/PAUSED indicator

### Keybindings
| Key | Action |
|-----|--------|
| `1`/`2`/`3` | Toggle device filter |
| `f` | Open category filter panel |
| `a` | Toggle all (devices or categories) |
| `c` | Copy visible logs to clipboard |
| `x` | Clear all logs |
| `↑`/`k`, `↓`/`j` | Scroll (pauses auto-scroll) |
| `PgUp`/`PgDn` | Page scroll |
| `G` | Jump to bottom (resume auto-scroll) |
| `q`/`Esc` | Quit |

### Category Filter Panel
- Activated with `f`, replaces log area
- Auto-discovers categories from `[UPPERCASE_TAG]` patterns in logs
- `space` toggles, `a` toggles all, `f`/`enter` closes

### Log Processing
- Framework noise filtered via `SYSLOG_NOISE_RE` (CoreFoundation, UIKitCore, etc.)
- App messages extracted from syslog format via `SYSLOG_MSG_RE`
- Categories auto-detected from `[BRACKET_TAGS]` (skips device tags MOSHE/SUMMIT/SIM/DEV)
- Errors highlighted red (lines containing "error", "fail", "crash")
- Max 10,000 lines in memory buffer

## CLI Usage

```
run-app                    # Interactive: pick app → pick device(s)
run-app -last              # Repeat last run (same app + devices)
run-app -last -nolog       # Repeat last, skip log viewer
run-app -last -stream      # Repeat last, plain text logs to stdout + file
run-app 3 1                # App #3 on device 1 (Moshe's iPhone)
run-app 3 13               # App #3 on devices 1+3 (Moshe + Simulator)
run-app 3 1 -stream        # App #3 on device 1, stream logs (no curses)
```

## Dependencies

- **Xcode CLI tools** (`xcodebuild`, `xcrun simctl`, `xcrun devicectl`)
- **idevicesyslog** (`brew install libimobiledevice`) — physical device log streaming
- **pbcopy** (macOS) — clipboard for `c` key in CLI
- **Python 3** (system) — for CLI only

## Integration with Claude Code

- `/run` skill: Claude Code equivalent, uses same device/app tables
- `/check-logs` skill: Reads the same log files `run-app` writes to
- `/build-phone` skill: Build + install only (no launch/logs)
- DebugLogger in apps tags lines with `[SIM]`/`[MOSHE]`/`[SUMMIT]` for multi-device identification
- **`-stream` mode**: Designed for Claude Code / automation. Prints plain text `[TAG] message` lines to stdout (no curses). Also writes to the app's log file. Other Claude Code instances can:
  - Read the log file directly (e.g. `/check-logs`)
  - Or run `run-app -stream` in background and capture stdout
