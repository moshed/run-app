#!/usr/bin/env python3
"""Quick launcher for iOS apps with live unified log viewer."""

import subprocess
import sys
import os
import tty
import termios
import json
import curses
import threading
import queue
import signal
import time
import select
import glob
import re

# ── App discovery ─────────────────────────────────────────────────────────────
# Apps are auto-discovered by scanning APPS_DIR for iOS Xcode projects. Drop a
# new app folder in /Users/moshe/Apps and it shows up here (and in apps.json,
# which the native menu-bar RunApp reads) — no manual edits needed. OVERRIDES
# only exists for what can't be derived from disk (custom log-file paths).

APPS_DIR = "/Users/moshe/Apps"
LOGS_DIR = "/Users/moshe/Apps/Logs"
APPS_JSON = "/Users/moshe/Apps/run-app/apps.json"

# Folders that contain an Xcode project but aren't launchable iOS apps.
EXCLUDE = {"run-app"}

# Per-app overrides merged OVER discovered values. Keyed by FOLDER name.
# Usually a non-default "log" path (the one thing not on disk), but "name" also
# works when an app is called something other than its folder -- editing
# apps.json for that is pointless, since every scan rewrites it from the folder.
OVERRIDES = {
    "CAR AND DRIVER":      {"log": "/Users/moshe/Apps/CAR AND DRIVER/car and driver log.txt"},
    "COBY Lyrics Speaker": {"log": "/Users/moshe/Apps/COBY Lyrics Speaker/cobylyricsspeaker_log.txt"},
    "COBY Smart":          {"log": "/Users/moshe/Apps/COBY Smart/COBY Smart log.txt"},
    "Intercom":            {"log": "/Users/moshe/Apps/Intercom/intercom.txt"},
    "Jukebox":             {"log": "/Users/moshe/Apps/Jukebox/.claude/jukebox_log.txt"},
    "Pixel":               {"log": "/Users/moshe/Apps/Pixel/ios/pixel_log.txt"},
    "Show Sourcing":       {"log": "/Users/moshe/Apps/Show Sourcing/showsourcinglog.txt"},
    "SLIDE Connect":       {"log": "/Users/moshe/Apps/SLIDE Connect/slideconnect.txt"},
    "Sports Alerts":       {"log": "/Users/moshe/Apps/Sports Alerts/sports alerts.log"},
    # renamed app, same folder on disk
    "World of Words":      {"name": "Wordbox"},
}


def _find_project(folder):
    """Path to the app's .xcodeproj (searches one subdir deep, e.g. Pixel/ios).
    Prefers a project named like the folder over stray/test projects."""
    name = os.path.basename(folder)
    cands = glob.glob(os.path.join(folder, "*.xcodeproj")) + \
            glob.glob(os.path.join(folder, "*", "*.xcodeproj"))
    if not cands:
        return None
    def score(p):
        base = os.path.splitext(os.path.basename(p))[0]
        return (base != name, "test" in base.lower(), len(p))
    return sorted(cands, key=score)[0]


def _is_ios(proj):
    """True if the project targets iOS. Mac-only apps are skipped —
    run-app deploys via simctl/devicectl, so macOS apps can't be launched."""
    try:
        t = open(os.path.join(proj, "project.pbxproj"), encoding="utf-8", errors="ignore").read()
    except OSError:
        return False
    settings = " ".join(re.findall(r'(?:SDKROOT|SUPPORTED_PLATFORMS)\s*=\s*"?([^";\n]+)"?;', t))
    if "iphoneos" in settings:
        return True
    return "auto" in settings and "macosx" not in settings


def _pick_scheme(proj, folder):
    name = os.path.basename(folder)
    proj_base = os.path.splitext(os.path.basename(proj))[0]
    schemes = [os.path.splitext(os.path.basename(s))[0]
               for s in glob.glob(os.path.join(proj, "xcshareddata", "xcschemes", "*.xcscheme"))]
    if schemes:
        def score(s):
            return (s != name and s != proj_base, "test" in s.lower(), len(s))
        return sorted(schemes, key=score)[0]
    return proj_base


def _pick_bundle(proj):
    """Container-app bundle id from project.pbxproj, resolving $(VAR) refs and
    skipping test/extension/widget targets."""
    try:
        t = open(os.path.join(proj, "project.pbxproj"), encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    vars_ = {}
    for m in re.finditer(r'^\s*([A-Z0-9_]+)\s*=\s*"?([^";\n]+)"?;', t, re.M):
        vars_.setdefault(m.group(1), m.group(2).strip())

    def resolve(v):
        for _ in range(5):
            m = re.search(r'\$[({]([A-Z0-9_]+)[)}]', v)
            if not m:
                break
            key = m.group(1)
            if key not in vars_:
                return None
            v = v[:m.start()] + vars_[key] + v[m.end():]
        return None if "$" in v else v

    ids = []
    for m in re.finditer(r'(?<![A-Z_])PRODUCT_BUNDLE_IDENTIFIER\s*=\s*"?([^";\n]+)"?;', t):
        v = resolve(m.group(1).strip())
        if v:
            ids.append(v)
    if not ids:
        return None
    def bscore(v):
        lo = v.lower()
        bad = any(k in lo for k in ("test", "extension", "widget", "clip", "watchkit", "nse", "share"))
        return (bad, v.count("."), len(v))
    return sorted(set(ids), key=bscore)[0]


def discover_apps():
    apps = []
    for name in sorted(os.listdir(APPS_DIR)):
        if name in EXCLUDE or name.startswith("."):
            continue
        folder = os.path.join(APPS_DIR, name)
        if not os.path.isdir(folder):
            continue
        proj = _find_project(folder)
        if not proj or not _is_ios(proj):
            continue
        bundle = _pick_bundle(proj)
        if not bundle:
            continue
        app = {
            "name": name,
            "path": os.path.dirname(proj),
            "scheme": _pick_scheme(proj, folder),
            "bundle": bundle,
            "log": os.path.join(LOGS_DIR, f"{name}.log"),
        }
        app.update(OVERRIDES.get(name, {}))
        apps.append(app)
    return apps


def write_apps_json(apps):
    """Persist the discovered list so the native menu-bar RunApp shares it."""
    try:
        with open(APPS_JSON, "w") as f:
            json.dump(apps, f, indent=2)
    except OSError:
        pass


APPS = discover_apps()
write_apps_json(APPS)

DEVICES = [
    {"name": "Moshe's iPhone",  "short": "MOSHE", "udid": "0C449D4B-C525-5E08-B643-0FEB379A1FFF", "hw_udid": "00008150-001A096E1AF8401C", "type": "physical"},
    {"name": "Summit's iPhone", "short": "SUMMIT", "udid": "49160EDB-AA57-52AA-A592-BE81B2B29D05", "hw_udid": "00008110-001849EA14A1A01E", "type": "physical"},
    {"name": "Simulator",       "short": "SIM",    "udid": "EACEFB3A-1643-4100-82A1-80410DD87344", "hw_udid": None, "type": "simulator"},
]

HISTORY_FILE = os.path.expanduser("~/.cache/run-app-history.json")

# ANSI (for pre-curses screens)
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
REVERSE = "\033[7m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CLEAR_SCREEN = "\033[2J\033[H"


# ── History ──────────────────────────────────────────────────────────────────

def load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_history(app_name, device_indices=None):
    history = load_history()
    if app_name in history:
        history.remove(app_name)
    history.insert(0, app_name)
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)
    # Save last run for quick repeat
    if device_indices is not None:
        last = {"app": app_name, "devices": device_indices}
        with open(HISTORY_FILE + ".last", "w") as f:
            json.dump(last, f)
        # Remember the device set *per app* so re-launching an app defaults to
        # the same devices it last ran on (shared with the menu-bar RunApp).
        save_app_devices(app_name, device_indices)


def load_last_run():
    try:
        with open(HISTORY_FILE + ".last") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# Per-app device memory: {app_name: [device_indices]}. Shared JSON so the CLI
# and the native menu-bar app agree on each app's last-used devices.
APP_DEVICES_FILE = os.path.expanduser("~/.cache/run-app-app-devices.json")


def load_app_devices():
    try:
        with open(APP_DEVICES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_app_devices(app_name, device_indices):
    data = load_app_devices()
    data[app_name] = device_indices
    os.makedirs(os.path.dirname(APP_DEVICES_FILE), exist_ok=True)
    with open(APP_DEVICES_FILE, "w") as f:
        json.dump(data, f)


def devices_for_app(app_name):
    """Device indices this app last ran on, or [] if never launched."""
    devs = load_app_devices().get(app_name, [])
    return [i for i in devs if 0 <= i < len(DEVICES)]


def sorted_apps():
    history = load_history()
    def sort_key(app):
        name = app["name"]
        if name in history:
            return (0, history.index(name))
        return (1, name.lower())
    return sorted(APPS, key=sort_key)


# ── Keyboard (pre-curses) ───────────────────────────────────────────────────

def read_key():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 == "A": return "up"
                if ch3 == "B": return "down"
                return None
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x03" or ch == "q":
            return "quit"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Picker UI (pre-curses) ──────────────────────────────────────────────────

def draw_menu(title, items, cursor, selected=None, multi=False):
    sys.stdout.write(CLEAR_SCREEN)
    sys.stdout.write(f"{BOLD}{title}{RESET}\n\n")
    for i, item in enumerate(items):
        prefix = ""
        if multi and selected is not None:
            prefix = f"{GREEN}●{RESET} " if i in selected else "○ "
        if i == cursor:
            sys.stdout.write(f"  {REVERSE} {prefix}{item} {RESET}\n")
        else:
            sys.stdout.write(f"   {prefix}{item}\n")
    sys.stdout.write(f"\n{DIM}")
    if multi:
        sys.stdout.write("↑↓ move  space select  enter confirm  q quit")
    else:
        sys.stdout.write("↑↓ move  enter=run+logs  space=run  q quit")
    sys.stdout.write(f"{RESET}\n")
    sys.stdout.flush()


def pick_single(title, items):
    """Returns (index, nolog). enter=logs, space=nolog. None on quit."""
    cursor = 0
    sys.stdout.write(HIDE_CURSOR)
    try:
        while True:
            draw_menu(title, items, cursor)
            key = read_key()
            if key == "up":
                cursor = (cursor - 1) % len(items)
            elif key == "down":
                cursor = (cursor + 1) % len(items)
            elif key == "enter":
                sys.stdout.write(SHOW_CURSOR)
                return cursor, False
            elif key == "space":
                sys.stdout.write(SHOW_CURSOR)
                return cursor, True
            elif key == "quit":
                sys.stdout.write(SHOW_CURSOR)
                return None, None
    except KeyboardInterrupt:
        sys.stdout.write(SHOW_CURSOR)
        return None, None


def pick_multi(title, items, preselected=None):
    cursor = 0
    selected = set(preselected) if preselected else set()
    sys.stdout.write(HIDE_CURSOR)
    try:
        while True:
            draw_menu(title, items, cursor, selected, multi=True)
            key = read_key()
            if key == "up":
                cursor = (cursor - 1) % len(items)
            elif key == "down":
                cursor = (cursor + 1) % len(items)
            elif key == "space":
                selected.symmetric_difference_update({cursor})
            elif key == "enter":
                sys.stdout.write(SHOW_CURSOR)
                return sorted(selected) if selected else [cursor]
            elif key == "quit":
                sys.stdout.write(SHOW_CURSOR)
                return []
    except KeyboardInterrupt:
        sys.stdout.write(SHOW_CURSOR)
        return []


# ── Device readiness ────────────────────────────────────────────────────────

NTFY_TOPIC = "ntfy.sh/claude_dnz"


def ntfy(title, message):
    """Push a phone notification (best effort, silent on failure)."""
    try:
        subprocess.run(
            ["curl", "-s", "-H", f"Title: {title}", "-d", message, NTFY_TOPIC],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


def _probe_device(device, timeout=8):
    """Quick probe of a physical device. True if reachable within timeout."""
    if device["type"] != "physical":
        return True
    try:
        result = subprocess.run(
            ["xcrun", "devicectl", "device", "info", "details",
             "--device", device["udid"]],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return True  # fail open on unexpected errors


def ensure_devices_ready(devices):
    """Pre-check physical devices. Alert + wait for user if any are unreachable.

    Interactive (TTY): wait on Enter between retries.
    Non-interactive (e.g. menu bar app subprocess): poll every 10s for ~3 min."""
    phys = [d for d in devices if d["type"] == "physical"]
    if not phys:
        return
    interactive = sys.stdin.isatty()
    max_attempts = 5 if interactive else 18
    for attempt in range(max_attempts):
        print(f"{DIM}Checking {', '.join(d['name'] for d in phys)}...{RESET}", flush=True)
        bad = [d for d in phys if not _probe_device(d)]
        if not bad:
            return
        names = ", ".join(d["name"] for d in bad)
        print(f"\n{RED}{BOLD}PHONE LOCKED?{RESET}{RED} {names} not responding — likely locked, asleep, or disconnected.{RESET}", flush=True)
        if attempt == 0:  # only notify once to avoid spam
            ntfy(f"Unlock {names}", "run-app is waiting — unlock your phone to continue")
        if interactive:
            print(f"{YELLOW}Unlock your phone, then press Enter to retry. (Ctrl+C to abort.){RESET}", flush=True)
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                print(f"{DIM}Aborted.{RESET}")
                sys.exit(1)
        else:
            print(f"{YELLOW}Waiting for phone to unlock... retry in 10s{RESET}", flush=True)
            time.sleep(10)
    print(f"{RED}Still can't reach device after retries. Continuing anyway...{RESET}", flush=True)


def show_error_alert(title, summary, full_output):
    """Pop a macOS alert with a Copy Output button. Stalls the script until
    dismissed so the user actually sees it before scrolling logs.

    `summary` is the short one-liner shown in the alert body.
    `full_output` is the multi-line stdout/stderr; copied to clipboard if the
    user hits "Copy Output".
    """
    # AppleScript chokes on quotes and backslashes in literals — escape them.
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    safe_title = esc(title)
    safe_msg = esc(summary)[:1000]  # alerts get truncated past a certain length
    script = (
        f'display alert "{safe_title}" message "{safe_msg}" '
        f'as critical buttons {{"Copy Output", "OK"}} default button "OK"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120
        )
        choice = (result.stdout or "").strip()
    except Exception:
        choice = ""

    if "Copy Output" in choice:
        try:
            subprocess.run(["pbcopy"], input=full_output, text=True, check=False)
            print(f"{YELLOW}Error output copied to clipboard.{RESET}")
        except Exception:
            print(f"{RED}Failed to copy output to clipboard.{RESET}")


def _is_error_output(output, return_code):
    """Heuristic: did this devicectl/simctl command actually fail?
    Exit code is the strongest signal but isn't always reliable when output
    was piped; check for known failure markers too.
    """
    if return_code != 0:
        return True
    markers = (
        "ERROR:", "Error Domain=", "unable to locate", "Could not connect",
        "FBSOpenApplicationServiceErrorDomain", "device was not, or could not be, unlocked",
        "Connection reset by peer", "Connection was invalidated",
    )
    return any(m in output for m in markers)


def _run_capture(cmd, *, timeout=120):
    """Run a shell command, capture combined stdout+stderr, return (output, code)."""
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired as e:
        return f"Command timed out after {timeout}s\n{e.output or ''}", -1
    except Exception as e:
        return f"Command failed to start: {e}", -1


class _SlowWarn:
    """Context manager: prints a warning + ntfy if the wrapped block takes >delay seconds."""
    def __init__(self, device_name, step, delay=12):
        self.device_name = device_name
        self.step = step
        self.delay = delay
        self.timer = None

    def _fire(self):
        print(f"\n{RED}Still waiting on {self.device_name} ({self.step}) — is the phone locked?{RESET}")
        ntfy(f"Unlock {self.device_name}", f"run-app stuck on {self.step} — unlock phone")

    def __enter__(self):
        self.timer = threading.Timer(self.delay, self._fire)
        self.timer.daemon = True
        self.timer.start()
        return self

    def __exit__(self, *exc):
        if self.timer:
            self.timer.cancel()


# ── Build ────────────────────────────────────────────────────────────────────

def build(app, device_type, release=False):
    config = "Release" if release else "Debug"
    if device_type == "simulator":
        dest = f"platform=iOS Simulator,id={DEVICES[2]['udid']}"
    else:
        dest = "generic/platform=iOS"
    cmd = f'cd "{app["path"]}" && xcodebuild -scheme "{app["scheme"]}" -destination "{dest}" -configuration {config} -derivedDataPath build -allowProvisioningUpdates build 2>&1'
    print(f"{YELLOW}Building {app['name']} ({config}) for {device_type}...{RESET}")
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    last_lines = []          # rolling tail, for the success path
    error_lines = []         # real compiler/linker/codesign errors, for the failure path
    all_output = []          # full log, copied to clipboard on failure
    # Lines that actually tell you *why* a build failed. xcodebuild buries these
    # hundreds of lines above the generic "BUILD FAILED" footer, so a naive tail
    # never shows them.
    err_markers = ("error:", "fatal error:", "error generated", "Undefined symbol",
                   "ld: ", "clang: error", "Command CompileSwift failed",
                   "Code Signing Error", "No such module", "cannot find",
                   "linker command failed", "does not contain a valid")
    for line in proc.stdout:
        line = line.rstrip()
        all_output.append(line)
        last_lines.append(line)
        if len(last_lines) > 5:
            last_lines.pop(0)
        if any(m in line for m in err_markers):
            error_lines.append(line)
    proc.wait()

    if proc.returncode != 0:
        print(f"{RED}BUILD FAILED{RESET}")
        # Show the real errors (deduped, capped) instead of the useless footer.
        seen = set()
        shown = []
        for l in error_lines:
            if l not in seen:
                seen.add(l)
                shown.append(l)
        shown = shown[:15]
        if shown:
            print(f"{RED}{BOLD}Errors:{RESET}")
            for l in shown:
                print(f"  {RED}{l}{RESET}")
        else:
            # No recognizable error markers — fall back to the tail so there's
            # at least something to go on.
            for l in last_lines:
                print(f"  {DIM}{l}{RESET}")
        show_error_alert(
            title=f"Build failed: {app['name']} ({config})",
            summary=("\n".join(shown) if shown else "\n".join(last_lines))
                    or "Build failed with no captured error output.",
            full_output="\n".join(all_output),
        )
        return False
    print(f"{GREEN}BUILD SUCCEEDED{RESET}")
    return True


# ── Install & Launch (returns log streaming process) ─────────────────────────

def install_and_launch(app, device, release=False):
    """Install, launch, and return the log-streaming subprocess.

    Returns `(proc, tag, log_file)` on success, or `None` if install or launch
    failed. On failure, a macOS alert pops with a "Copy Output" button so the
    user can capture the full error log without copy-pasting from the terminal.
    """
    config = "Release" if release else "Debug"
    log_file = app["log"]
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    open(log_file, "w").close()

    tag = device["short"]

    if device["type"] == "simulator":
        boot_cmd = f'xcrun simctl boot {device["udid"]} 2>/dev/null'
        subprocess.run(boot_cmd, shell=True)
        app_path = f'{app["path"]}/build/Build/Products/{config}-iphonesimulator/{app["scheme"]}.app'
        print(f"{YELLOW}Installing on {device['name']}...{RESET}")
        install_cmd = f'xcrun simctl install {device["udid"]} "{app_path}"'
        out, code = _run_capture(install_cmd, timeout=120)
        if _is_error_output(out, code):
            print(f"{RED}Install failed on {device['name']} (exit {code}).{RESET}")
            print(out.strip())
            show_error_alert(
                title=f"Install failed: {app['name']} → {device['name']}",
                summary=f"Simulator install failed (exit {code}). The simulator may not be booted, or the .app may not exist at the expected path.",
                full_output=f"$ {install_cmd}\n{out}",
            )
            return None
        print(f"{GREEN}Launched on {device['name']}{RESET}")
        cmd = f'xcrun simctl launch --console {device["udid"]} {app["bundle"]} 2>&1'
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        return proc, tag, log_file
    else:
        app_path = f'{app["path"]}/build/Build/Products/{config}-iphoneos/{app["scheme"]}.app'
        # Step 1: Install via devicectl. Capture output so we can show a real
        # error alert instead of silently piping through `tail -3`.
        print(f"{YELLOW}Installing on {device['name']}...{RESET}")
        install_cmd = f'xcrun devicectl device install app --device {device["udid"]} "{app_path}"'
        with _SlowWarn(device["name"], "install"):
            install_out, install_code = _run_capture(install_cmd, timeout=180)

        if _is_error_output(install_out, install_code):
            print(f"{RED}Install failed on {device['name']} (exit {install_code}).{RESET}")
            # Print the tail so terminal users see what went wrong.
            for line in install_out.strip().splitlines()[-8:]:
                print(f"  {DIM}{line}{RESET}")
            # Most-common failure: phone unavailable / locked / out of range.
            short = "The device is unavailable. Unlock the phone, plug it in via USB, or check Wi-Fi pairing."
            if "unable to locate" in install_out.lower() or "unavailable" in install_out.lower():
                short = "The device is unavailable. Unlock the phone, plug it in via USB, or check the Devices list in Xcode."
            elif "Connection reset by peer" in install_out:
                short = "Connection to the device dropped mid-install. Try again — usually fine on retry."
            elif "could not be installed" in install_out.lower():
                short = "iOS rejected the install. Check provisioning, signing, or that the .app was archived for the device."
            show_error_alert(
                title=f"Install failed: {app['name']} → {device['name']}",
                summary=short,
                full_output=f"$ {install_cmd}\n\n{install_out}",
            )
            return None

        # Step 2: Launch with --console to capture print() output (idevicesyslog broken on iOS 26+).
        # We use Popen here because we want to keep streaming logs after launch
        # — but we also want to detect immediate launch failures (exit < 1s).
        print(f"{YELLOW}Launching on {device['name']}...{RESET}")
        launch_cmd = (f'xcrun devicectl device process launch --device {device["udid"]}'
                      f' --terminate-existing --console {app["bundle"]} 2>&1')
        proc = subprocess.Popen(launch_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        # Sniff the first 2.5s of output. If devicectl exits non-zero in that
        # window (locked phone, app missing, etc.), grab whatever it printed
        # and surface it. Otherwise the process continues streaming logs.
        import time
        time.sleep(2.5)
        if proc.poll() is not None and proc.returncode != 0:
            launch_out = (proc.stdout.read() if proc.stdout else "") or ""
            print(f"{RED}Launch failed on {device['name']} (exit {proc.returncode}).{RESET}")
            for line in launch_out.strip().splitlines()[-8:]:
                print(f"  {DIM}{line}{RESET}")
            short = "App couldn't launch."
            low = launch_out.lower()
            if "could not be, unlocked" in low or "fbsopenapplicationerrordomain" in low:
                short = "Device is locked. Unlock the phone and try again."
            elif "unable to locate" in low or "unavailable" in low:
                short = "The device dropped between install and launch. Reconnect and re-run."
            elif "process did not launch" in low or "process not found" in low:
                short = "iOS refused to launch the process. Check entitlements and bundle ID."
            show_error_alert(
                title=f"Launch failed: {app['name']} → {device['name']}",
                summary=short,
                full_output=f"$ {launch_cmd}\n\n{launch_out}",
            )
            return None
        print(f"{GREEN}Launched on {device['name']}{RESET}")
        return proc, tag, log_file


# ── Live Log Viewer (curses) ────────────────────────────────────────────────

import re

# Regex to extract category from log lines like [AUTH], [NETWORK], [SYNC], etc.
# Matches bracketed uppercase words that look like categories (not timestamps, not device tags)
CATEGORY_RE = re.compile(r'\[([A-Z][A-Z_]{1,20})\]')
DEVICE_TAGS_SET = {"MOSHE", "SUMMIT", "SIM", "DEV"}  # skip these as categories


def extract_category(line):
    """Extract the first category-like [TAG] from a log line."""
    for m in CATEGORY_RE.finditer(line):
        cat = m.group(1)
        if cat not in DEVICE_TAGS_SET:
            return cat
    return None


# Regex to detect idevicesyslog framework noise (e.g. "App(CoreFoundation)", "App(RunningBoardServices)")
# We keep lines from the app's own dylib or lines without framework tags
SYSLOG_NOISE_RE = re.compile(r'\w+\((CoreFoundation|RunningBoardServices|UIKitCore|CFNetwork|Foundation|libnetwork|Network|Security|CoreBluetooth|AVFAudio|libswiftCore)\)')
# Regex to extract the actual log message from idevicesyslog format
# Format: "Mar  8 01:59:40.161355 Pixel(Pixel.debug.dylib)[5172] <Info>: actual message"
SYSLOG_MSG_RE = re.compile(r'^\w+\s+\d+\s+[\d:.]+\s+\w+\([^)]+\)\[\d+\]\s+<\w+>:\s*(.*)')

def log_reader(proc, tag, log_file, log_queue, stop_event):
    """Thread that reads from a log process and pushes tagged lines to queue."""
    try:
        fh = open(log_file, "a")
        for line in proc.stdout:
            if stop_event.is_set():
                break
            line = line.rstrip()
            if not line or line.startswith("[connected:"):
                continue
            # Filter idevicesyslog framework noise
            if SYSLOG_NOISE_RE.search(line):
                continue
            # Extract actual message from syslog format
            m = SYSLOG_MSG_RE.match(line)
            if m:
                line = m.group(1)
            tagged = f"[{tag}] {line}"
            fh.write(tagged + "\n")
            fh.flush()
            category = extract_category(line)
            log_queue.put((tag, line, category))
        fh.close()
    except Exception:
        pass


def live_log_viewer(stdscr, app_name, device_tags, log_queue, stop_event):
    """Curses-based live log viewer with device + category filters."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)    # header
    curses.init_pair(2, curses.COLOR_CYAN, -1)     # MOSHE tag
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # SUMMIT tag
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)  # SIM tag
    curses.init_pair(5, curses.COLOR_WHITE, -1)    # normal log
    curses.init_pair(6, curses.COLOR_RED, -1)      # error
    curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE)  # filter active
    curses.init_pair(8, curses.COLOR_WHITE, -1)    # filter inactive
    curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_GREEN)  # category cursor active
    curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_RED)   # category cursor inactive
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(50)

    TAG_COLORS = {"MOSHE": 2, "SUMMIT": 3, "SIM": 4}

    all_logs = []              # (tag, line, category) tuples
    max_logs = 10000
    dev_filters = {t: True for t in device_tags}
    cat_filters = {}           # category -> bool, auto-populated
    categories_order = []      # insertion order
    auto_scroll = True
    scroll_offset = 0
    copy_flash_time = 0

    # Filter mode: None = normal log view, "categories" = category filter panel
    filter_mode = None
    cat_cursor = 0
    cat_scroll = 0  # scroll offset for category list

    while not stop_event.is_set():
        # Drain queue
        new_lines = False
        while True:
            try:
                tag, line, category = log_queue.get_nowait()
                all_logs.append((tag, line, category))
                if len(all_logs) > max_logs:
                    all_logs.pop(0)
                # Auto-discover categories
                if category and category not in cat_filters:
                    cat_filters[category] = True
                    categories_order.append(category)
                new_lines = True
            except queue.Empty:
                break

        if new_lines and auto_scroll and filter_mode is None:
            scroll_offset = 0

        # Handle input
        try:
            key = stdscr.getch()
        except Exception:
            key = -1

        if filter_mode == "categories":
            # ── Category filter mode ─────────────────────────────────
            if key == ord("f") or key == 27 or key == ord("\n") or key == ord("\r"):
                filter_mode = None
            elif key == curses.KEY_UP or key == ord("k"):
                cat_cursor = max(0, cat_cursor - 1)
            elif key == curses.KEY_DOWN or key == ord("j"):
                cat_cursor = min(len(categories_order) - 1, cat_cursor)
                if cat_cursor < len(categories_order) - 1:
                    cat_cursor += 1
            elif key == ord(" "):
                if 0 <= cat_cursor < len(categories_order):
                    c = categories_order[cat_cursor]
                    cat_filters[c] = not cat_filters[c]
            elif key == ord("a"):
                all_on = all(cat_filters.get(c, True) for c in categories_order)
                for c in categories_order:
                    cat_filters[c] = not all_on
            elif key == ord("q"):
                filter_mode = None
        else:
            # ── Normal log mode ──────────────────────────────────────
            if key == ord("q") or key == 27:
                stop_event.set()
                break
            elif key == ord("f"):
                filter_mode = "categories"
                cat_cursor = 0
            elif key == ord("1") and len(device_tags) >= 1:
                t = device_tags[0]
                dev_filters[t] = not dev_filters[t]
            elif key == ord("2") and len(device_tags) >= 2:
                t = device_tags[1]
                dev_filters[t] = not dev_filters[t]
            elif key == ord("3") and len(device_tags) >= 3:
                t = device_tags[2]
                dev_filters[t] = not dev_filters[t]
            elif key == ord("a"):
                all_on = all(dev_filters.values())
                for t in dev_filters:
                    dev_filters[t] = not all_on
            elif key == curses.KEY_UP or key == ord("k"):
                auto_scroll = False
                scroll_offset += 1
            elif key == curses.KEY_DOWN or key == ord("j"):
                if scroll_offset > 0:
                    scroll_offset -= 1
                if scroll_offset == 0:
                    auto_scroll = True
            elif key == curses.KEY_PPAGE:
                auto_scroll = False
                h = stdscr.getmaxyx()[0]
                scroll_offset += h - 4
            elif key == curses.KEY_NPAGE:
                h = stdscr.getmaxyx()[0]
                scroll_offset = max(0, scroll_offset - (h - 4))
                if scroll_offset == 0:
                    auto_scroll = True
            elif key == ord("G") or key == curses.KEY_END:
                scroll_offset = 0
                auto_scroll = True
            elif key == ord("c"):
                # Copy all visible logs to clipboard (inline filter)
                copy_lines = []
                for _tag, _line, _cat in all_logs:
                    if not dev_filters.get(_tag, True):
                        continue
                    if _cat and not cat_filters.get(_cat, True):
                        continue
                    copy_lines.append(_line)
                if copy_lines:
                    try:
                        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                        p.communicate("\n".join(copy_lines).encode())
                        copy_flash_time = time.time()
                    except Exception:
                        pass
            elif key == ord("x"):
                all_logs.clear()
                scroll_offset = 0

        # Filter logs by device + category
        def passes_filter(entry):
            tag, line, category = entry
            if not dev_filters.get(tag, True):
                return False
            if category and not cat_filters.get(category, True):
                return False
            return True

        visible = [e for e in all_logs if passes_filter(e)]

        # Count active category filters
        active_cats = sum(1 for c in categories_order if cat_filters.get(c, True))
        total_cats = len(categories_order)

        # Draw
        try:
            stdscr.erase()
            rows, cols = stdscr.getmaxyx()
            if rows < 5 or cols < 20:
                continue

            # ── Row 0: App name + device filters ─────────────────────
            header = f" {app_name} "
            stdscr.addstr(0, 0, header, curses.A_BOLD | curses.color_pair(1))

            x = len(header) + 2
            for i, t in enumerate(device_tags):
                label = f" {i+1}:{t} "
                pair = 7 if dev_filters[t] else 8
                attr = curses.color_pair(pair)
                if not dev_filters[t]:
                    attr |= curses.A_DIM
                if x + len(label) < cols:
                    stdscr.addstr(0, x, label, attr)
                x += len(label) + 1

            count_str = f" {len(visible)}/{len(all_logs)} "
            if cols - len(count_str) - 1 > x:
                stdscr.addstr(0, cols - len(count_str) - 1, count_str, curses.A_DIM)

            # ── Row 1: Category filters ──────────────────────────────
            cat_label = " f:categories "
            if total_cats > 0:
                cat_label = f" f:categories ({active_cats}/{total_cats}) "
            cat_attr = curses.A_BOLD if filter_mode == "categories" else curses.A_DIM
            stdscr.addstr(1, 0, cat_label, cat_attr)

            # Show category pills on row 1
            x = len(cat_label) + 1
            for c in categories_order:
                pill = f" {c} "
                if x + len(pill) + 1 >= cols:
                    break
                if cat_filters.get(c, True):
                    stdscr.addstr(1, x, pill, curses.color_pair(7))
                else:
                    stdscr.addstr(1, x, pill, curses.A_DIM)
                x += len(pill) + 1

            # ── Row 2: Separator ─────────────────────────────────────
            stdscr.addstr(2, 0, "─" * (cols - 1), curses.A_DIM)

            if filter_mode == "categories":
                # ── Category filter panel (replaces log area) ────────
                panel_rows = rows - 4  # header rows + separator + footer
                if panel_rows < 1:
                    continue

                # Ensure cursor is scrolled into view
                if cat_cursor < cat_scroll:
                    cat_scroll = cat_cursor
                if cat_cursor >= cat_scroll + panel_rows:
                    cat_scroll = cat_cursor - panel_rows + 1

                stdscr.addstr(3, 1, "Toggle categories (space=toggle  a=all  f/enter=done)", curses.A_DIM)

                for row_idx in range(min(panel_rows - 1, len(categories_order) - cat_scroll)):
                    ci = cat_scroll + row_idx
                    if ci >= len(categories_order):
                        break
                    c = categories_order[ci]
                    screen_row = row_idx + 4
                    is_on = cat_filters.get(c, True)
                    marker = "●" if is_on else "○"

                    if ci == cat_cursor:
                        pair = 9 if is_on else 10
                        try:
                            stdscr.addstr(screen_row, 2, f" {marker} {c} ", curses.color_pair(pair) | curses.A_BOLD)
                        except curses.error:
                            pass
                    else:
                        attr = curses.A_NORMAL if is_on else curses.A_DIM
                        try:
                            stdscr.addstr(screen_row, 2, f" {marker} ", curses.color_pair(1) if is_on else curses.color_pair(8))
                            stdscr.addstr(screen_row, 5, c, attr)
                        except curses.error:
                            pass

                # Footer for filter mode
                footer_row = rows - 1
                footer = " ↑↓:move  space:toggle  a:all  f/enter:done "
                try:
                    stdscr.addstr(footer_row, 0, footer[:cols-1], curses.A_DIM | curses.A_REVERSE)
                    pad = cols - 1 - len(footer)
                    if pad > 0:
                        stdscr.addstr(footer_row, len(footer), " " * pad, curses.A_DIM | curses.A_REVERSE)
                except curses.error:
                    pass
            else:
                # ── Log area (rows 3 to rows-2) ─────────────────────
                log_rows = rows - 4  # 3 header rows + footer
                if log_rows < 1:
                    continue

                total = len(visible)
                end = max(0, total - scroll_offset)
                start = max(0, end - log_rows)

                for row_idx, log_idx in enumerate(range(start, end)):
                    if row_idx >= log_rows:
                        break
                    tag, line, category = visible[log_idx]
                    screen_row = row_idx + 3

                    # Tag prefix
                    tag_color = TAG_COLORS.get(tag, 5)
                    tag_str = f"[{tag}] "
                    try:
                        stdscr.addstr(screen_row, 0, tag_str, curses.color_pair(tag_color) | curses.A_BOLD)
                    except curses.error:
                        pass

                    remaining = cols - len(tag_str) - 1
                    display_line = line[:remaining] if remaining > 0 else ""

                    line_color = 5
                    ll = line.lower()
                    if "error" in ll or "fail" in ll or "crash" in ll:
                        line_color = 6

                    try:
                        stdscr.addstr(screen_row, len(tag_str), display_line, curses.color_pair(line_color))
                    except curses.error:
                        pass

                # Footer
                footer_row = rows - 1
                scroll_indicator = " LIVE" if auto_scroll else f" PAUSED (↑{scroll_offset})"
                copy_msg = "  ✓ COPIED" if (time.time() - copy_flash_time) < 2 else ""
                footer = f" 1-3:device  f:categories  a:all  c:copy  x:clear  ↑↓/jk  G:bottom  q:quit{scroll_indicator}{copy_msg}"
                try:
                    stdscr.addstr(footer_row, 0, footer[:cols-1], curses.A_DIM | curses.A_REVERSE)
                    pad = cols - 1 - len(footer)
                    if pad > 0:
                        stdscr.addstr(footer_row, len(footer), " " * pad, curses.A_DIM | curses.A_REVERSE)
                except curses.error:
                    pass

            stdscr.refresh()
        except curses.error:
            pass


# ── Main ─────────────────────────────────────────────────────────────────────

def run_with_logs(app, devices, release=False):
    """Build, install, launch on all devices, then show live unified log viewer."""
    sys.stdout.write(CLEAR_SCREEN)
    print(f"{BOLD}{app['name']}{RESET} → {', '.join(d['name'] for d in devices)}\n")

    ensure_devices_ready(devices)

    need_sim = any(d["type"] == "simulator" for d in devices)
    need_phy = any(d["type"] == "physical" for d in devices)

    if need_phy and not build(app, "physical", release=release):
        sys.exit(1)
    if need_sim and not build(app, "simulator", release=release):
        sys.exit(1)

    # Launch all devices and collect log processes. install_and_launch
    # returns None on failure; skip those so the log viewer still starts
    # for the devices that did succeed.
    log_procs = []
    device_tags = []
    for d in devices:
        result = install_and_launch(app, d, release=release)
        if result is None:
            continue
        proc, tag, log_file = result
        log_procs.append((proc, tag, log_file))
        device_tags.append(tag)

    if not log_procs:
        print(f"{RED}No devices launched successfully. Aborting.{RESET}")
        sys.exit(1)

    print(f"\n{GREEN}All devices launched. Starting log viewer...{RESET}")
    time.sleep(1)

    # Start log reader threads
    log_q = queue.Queue()
    stop = threading.Event()
    threads = []
    for proc, tag, log_file in log_procs:
        t = threading.Thread(target=log_reader, args=(proc, tag, log_file, log_q, stop), daemon=True)
        t.start()
        threads.append(t)

    # Run curses log viewer
    try:
        curses.wrapper(lambda stdscr: live_log_viewer(stdscr, app["name"], device_tags, log_q, stop))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        # Kill log streaming processes
        for proc, _, _ in log_procs:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for t in threads:
            t.join(timeout=1)
        print(f"\n{DIM}Log viewer closed.{RESET}")


def run_stream(app, devices, release=False):
    """Build, install, launch — plain text log stream to stdout + log file. No curses."""
    sys.stdout.write(CLEAR_SCREEN)
    print(f"{BOLD}{app['name']}{RESET} → {', '.join(d['name'] for d in devices)}\n")

    ensure_devices_ready(devices)

    need_sim = any(d["type"] == "simulator" for d in devices)
    need_phy = any(d["type"] == "physical" for d in devices)

    if need_phy and not build(app, "physical", release=release):
        sys.exit(1)
    if need_sim and not build(app, "simulator", release=release):
        sys.exit(1)

    # Launch all devices and collect log processes. install_and_launch
    # returns None on failure; skip dead devices.
    log_procs = []
    device_tags = []
    for d in devices:
        result = install_and_launch(app, d, release=release)
        if result is None:
            continue
        proc, tag, log_file = result
        log_procs.append((proc, tag, log_file))
        device_tags.append(tag)

    if not log_procs:
        print(f"{RED}No devices launched successfully. Aborting.{RESET}")
        sys.exit(1)

    print(f"\n{GREEN}All devices launched. Streaming logs...{RESET}\n")

    # Start log reader threads that write to stdout + file
    log_q = queue.Queue()
    stop = threading.Event()
    threads = []
    for proc, tag, log_file in log_procs:
        t = threading.Thread(target=log_reader, args=(proc, tag, log_file, log_q, stop), daemon=True)
        t.start()
        threads.append(t)

    # Stream logs to stdout as plain text
    try:
        while not stop.is_set():
            try:
                tag, line, category = log_q.get(timeout=0.5)
                print(f"[{tag}] {line}", flush=True)
            except queue.Empty:
                # Check if all log processes are dead
                all_dead = all(proc.poll() is not None for proc, _, _ in log_procs)
                if all_dead:
                    # Drain remaining items
                    while True:
                        try:
                            tag, line, category = log_q.get_nowait()
                            print(f"[{tag}] {line}", flush=True)
                        except queue.Empty:
                            break
                    break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for proc, _, _ in log_procs:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for t in threads:
            t.join(timeout=1)
        print(f"\n{DIM}Log stream ended.{RESET}")


def run_nolog(app, devices, release=False):
    """Build, install, launch — no log viewer. Logs stream to file via detached processes."""
    config = "Release" if release else "Debug"
    sys.stdout.write(CLEAR_SCREEN)
    print(f"{BOLD}{app['name']}{RESET} → {', '.join(d['name'] for d in devices)}\n")

    ensure_devices_ready(devices)

    need_sim = any(d["type"] == "simulator" for d in devices)
    need_phy = any(d["type"] == "physical" for d in devices)

    if need_phy and not build(app, "physical", release=release):
        sys.exit(1)
    if need_sim and not build(app, "simulator", release=release):
        sys.exit(1)

    log_file = app["log"]
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    open(log_file, "w").close()

    for d in devices:
        tag = d["short"]
        if d["type"] == "simulator":
            subprocess.run(f'xcrun simctl boot {d["udid"]} 2>/dev/null', shell=True)
            app_path = f'{app["path"]}/build/Build/Products/{config}-iphonesimulator/{app["scheme"]}.app'
            print(f"{YELLOW}Installing on {d['name']}...{RESET}")
            subprocess.run(f'xcrun simctl install {d["udid"]} "{app_path}"', shell=True)
            # Launch with --console, tag lines, append to log file — detached
            cmd = f'xcrun simctl launch --console {d["udid"]} {app["bundle"]} 2>&1 | sed "s/^/[{tag}] /" >> "{log_file}"'
            subprocess.Popen(cmd, shell=True, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"{GREEN}Launched on {d['name']}{RESET}")
        else:
            app_path = f'{app["path"]}/build/Build/Products/{config}-iphoneos/{app["scheme"]}.app'
            print(f"{YELLOW}Installing on {d['name']}...{RESET}")
            with _SlowWarn(d["name"], "install"):
                subprocess.run(f'xcrun devicectl device install app --device {d["udid"]} "{app_path}" 2>&1 | tail -3', shell=True)
            print(f"{YELLOW}Launching on {d['name']}...{RESET}")
            # Launch with --console to capture print() output (idevicesyslog broken on iOS 26+)
            cmd = (f'xcrun devicectl device process launch --device {d["udid"]}'
                   f' --terminate-existing --console {app["bundle"]} 2>&1'
                   f' | grep --line-buffered -v -E "Waiting for the application|Acquired tunnel|Enabling developer|Acquired usage|Launched application"'
                   f' | sed -u "s/^/[{tag}] /"'
                   f' >> "{log_file}"')
            subprocess.Popen(cmd, shell=True, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import time; time.sleep(2)  # Brief wait for launch
            print(f"{GREEN}Launched on {d['name']}{RESET}")

    print(f"\n{GREEN}Done. Logs at: {app['log']}{RESET}")


def main():
    args = sys.argv[1:]
    # Fast path: just (re)discover apps + rewrite apps.json and exit. The native
    # menu-bar RunApp runs this on launch so its list stays in sync with disk.
    if "--emit-apps" in args:
        write_apps_json(APPS)
        print(APPS_JSON)
        return
    flag_last = "-last" in args
    flag_nolog = "-nolog" in args
    flag_stream = "-stream" in args
    flag_release = "-release" in args
    # Parse --name "App Name" flag (used by menu bar app)
    flag_name = None
    if "--name" in args:
        ni = args.index("--name")
        if ni + 1 < len(args):
            flag_name = args[ni + 1]
            args = args[:ni] + args[ni+2:]
    positional = [a for a in args if not a.startswith("-")]

    def launcher(app, devices):
        if flag_stream:
            fn = run_stream
        elif flag_nolog:
            fn = run_nolog
        else:
            fn = run_with_logs
        fn(app, devices, release=flag_release)

    # --name "App Name" <device_num> (used by menu bar app)
    if flag_name and positional:
        app = next((a for a in APPS if a["name"] == flag_name), None)
        if app:
            device_indices = [int(ch) - 1 for ch in positional[0] if ch.isdigit() and 0 < int(ch) <= len(DEVICES)]
            if device_indices:
                devices = [DEVICES[i] for i in device_indices]
                save_history(app["name"], device_indices)
                launcher(app, devices)
                return
        print(f"{RED}App not found: {flag_name}{RESET}")
        return

    # Quick repeat: run-app -last [-nolog]
    if flag_last:
        last = load_last_run()
        if last:
            app = next((a for a in APPS if a["name"] == last["app"]), None)
            if app:
                devices = [DEVICES[i] for i in last["devices"] if i < len(DEVICES)]
                if devices:
                    launcher(app, devices)
                    return
        print(f"{RED}No previous run to repeat.{RESET}")
        return

    # Quick args: run-app [app_num] [device_nums] [-nolog]
    if len(positional) >= 2:
        apps = sorted_apps()
        try:
            app_idx = int(positional[0]) - 1
        except ValueError:
            app_idx = -1
        device_indices = [int(ch) - 1 for ch in positional[1] if ch.isdigit() and 0 < int(ch) <= len(DEVICES)]
        if 0 <= app_idx < len(apps) and device_indices:
            app = apps[app_idx]
            devices = [DEVICES[i] for i in device_indices]
            save_history(app["name"], device_indices)
            launcher(app, devices)
            return

    # Interactive mode
    apps = sorted_apps()
    app_names = [a["name"] for a in apps]

    # Add "Repeat last" option if there's a previous run
    last = load_last_run()
    if last:
        last_app = last["app"]
        last_devs = ", ".join(DEVICES[i]["name"] for i in last["devices"] if i < len(DEVICES))
        app_names.insert(0, f"↻ {last_app} → {last_devs}")

    app_idx, nolog = pick_single("Which app?", app_names)
    if app_idx is None:
        sys.stdout.write(CLEAR_SCREEN)
        return

    def launcher(app, devices):
        if flag_stream:
            fn = run_stream
        elif flag_nolog or nolog:
            fn = run_nolog
        else:
            fn = run_with_logs
        fn(app, devices, release=flag_release)

    # Handle "Repeat last" selection
    if last and app_idx == 0:
        app = next((a for a in APPS if a["name"] == last["app"]), None)
        if app:
            devices = [DEVICES[i] for i in last["devices"] if i < len(DEVICES)]
            if devices:
                launcher(app, devices)
                return

    # Adjust index if repeat option was inserted
    if last:
        app_idx -= 1

    app = apps[app_idx]

    device_names = [d["name"] for d in DEVICES]
    # Pre-tick the devices this app last ran on, so a single Enter re-launches
    # to the same set without re-selecting.
    device_picks = pick_multi("Run on:", device_names, preselected=devices_for_app(app["name"]))
    if not device_picks:
        sys.stdout.write(CLEAR_SCREEN)
        return

    devices = [DEVICES[i] for i in device_picks]
    save_history(app["name"], device_picks)
    launcher(app, devices)


if __name__ == "__main__":
    main()
