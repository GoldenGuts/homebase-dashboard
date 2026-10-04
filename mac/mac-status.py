#!/usr/bin/env python3
"""Prints one JSON line with Mac status. Polled by Home Assistant (sensor.mac_status) over SSH.

Installed as ~/bin/mac-status.py (see install.sh). Keys marked "compat" are read by the Home
dashboard (packages/mac.yaml template sensors) and must keep their names.
"""
import glob
import json
import os
import re
import subprocess
import time

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, "projects")
CACHE = os.path.join(HOME, "Library/Caches/mac-dash")
LOG = os.path.join(HOME, "Library/Logs/claude-rc.log")
APPS = ["Antigravity", "Cursor", "Zed", "Google Chrome", "Ghostty"]   # shown as tiles; keep in sync with mac-action.sh
DEV_CMDS = ("node", "python", "bun", "deno", "ruby", "java", "go", "cargo", "uvicorn", "gunicorn", "php", "dotnet",
            "ollama", "litellm", "caddy", "vite", "next", "flask", "rails", "docker", "com.docker", "esphome", "npm", "pnpm")
LINK_RE = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")
os.environ["PATH"] = HOME + "/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def sh(cmd, timeout=5):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def num(s, cast=int, default=0):
    try:
        return cast(s)
    except Exception:
        return default


# --- Claude hosts (tmux "claude-<project>", one `claude remote-control` per folder) ------------------
# Claude Code allows ONE remote-control process per folder; that host serves up to 32 sessions.
# Extra sessions in the same folder are created from the app through the environment link (env_link).
ENV_RE = re.compile(r"https://claude\.ai/code\?environment=env_[A-Za-z0-9]+")
OSC8_RE = re.compile(r"\x1b\]8;;(https://claude\.ai/code/session_[A-Za-z0-9]+)[^\x1b]*\x1b\\([^\x1b]*)\x1b\]8;;")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\]8;;[^\x1b]*\x1b\\")
sessions = []
for line in run(["tmux", "ls", "-F", "#{session_name}|#{session_created}|#{session_path}"]).splitlines():
    parts = line.split("|")
    if len(parts) < 3 or not parts[0].startswith("claude-"):
        continue
    sess, created, path = parts[0], num(parts[1]), parts[2]
    name = sess[len("claude-"):]
    raw = run(["tmux", "capture-pane", "-p", "-e", "-J", "-S", "-2000", "-t", sess])
    pane = ANSI_RE.sub("", raw)
    envs = ENV_RE.findall(pane)
    env_link = envs[-1] if envs else None
    # Last screen block: "Capacity: N/32 ..." then one OSC-8 hyperlink line per served session
    # (\x1b]8;;<url>\x1b\\<title>\x1b]8;;\x1b\\), then "Continue coding ...".
    cap_used, cap_max, served = 0, 32, []
    caps = list(re.finditer(r"Capacity: (\d+)/(\d+)", raw))
    if caps:
        m = caps[-1]
        cap_used, cap_max = num(m.group(1)), num(m.group(2))
        block = raw[m.end():]
        cut = block.find("Continue coding")
        if cut > 0:
            block = block[:cut]
        for u, t in OSC8_RE.findall(block):
            served.append({"title": t.strip()[:60], "link": u.split("?")[0]})
    link = served[0]["link"] if served else None
    if not link:
        links = LINK_RE.findall(pane)
        link = links[-1] if links else None
    if not link:  # fall back to the log (pane may have been cleared)
        try:
            with open(LOG, errors="ignore") as f:
                tail = f.read()[-200000:]
            i = tail.rfind("Connected \u00b7 " + name + " \u00b7")
            if i >= 0:
                m = LINK_RE.findall(tail[i:i + 2000])
                link = m[0] if m else None
                m = ENV_RE.findall(tail[i:i + 2000])
                env_link = env_link or (m[0] if m else None)
        except OSError:
            pass
    tail_txt = pane[-800:]
    if "already served" in tail_txt:
        status = "duplicate"
    elif "Error" in tail_txt and not caps:
        status = "error"
    elif caps or link:
        status = "connected"
    else:
        status = "starting"
    sessions.append({"name": name, "folder": path, "project": os.path.basename(path.rstrip("/")),
                     "link": link, "env_link": env_link, "started": created, "status": status,
                     "sessions": cap_used, "capacity": cap_max, "served": served[:8]})
sessions.sort(key=lambda s: s["started"])
active_sessions = sum(s["sessions"] or (1 if s["status"] == "connected" else 0) for s in sessions)

# --- apps ------------------------------------------------------------------------------------
procs = run(["ps", "-A", "-o", "comm="])
apps = {}
for app in APPS:
    apps[app] = ("/%s.app/" % app) in procs or ("/%s.app/" % app.lower()) in procs

caffeinate = bool(run(["pgrep", "-x", "caffeinate"]).strip())
caff_until = None
if caffeinate:
    try:
        caff_until = num(open(os.path.join(CACHE, "caffeinate_until")).read().strip())
    except OSError:
        pass

# --- system ------------------------------------------------------------------------------------
boot = num(re.search(r"sec = (\d+)", run(["sysctl", "-n", "kern.boottime"]) or "sec = 0").group(1))
up_h = int((time.time() - boot) / 3600) if boot else 0
load = num((run(["sysctl", "-n", "vm.loadavg"]).split() + ["0", "0"])[1], float, 0.0)
df = run(["df", "-k", "/System/Volumes/Data"]).splitlines()
disk_free, disk_pct = "?", 0
if len(df) > 1:
    f = df[1].split()
    disk_free = "%.0fG" % (num(f[3]) / 1048576.0)
    disk_pct = num(f[4].rstrip("%"))
free_pages = num((re.search(r"Pages free:\s+(\d+)", run(["vm_stat"])) or re.match(r"(\d+)", "0")).group(1))
mem_free_mb = int(free_pages * 4096 / 1048576)
mem_free_pct = num((re.search(r"free percentage: (\d+)%", run(["memory_pressure"], 8)) or re.match(r"(\d+)", "0")).group(1))
ip = run(["ipconfig", "getifaddr", "en0"]).strip()

batt_txt = run(["pmset", "-g", "batt"])
m = re.search(r"(\d+)%", batt_txt)
battery = num(m.group(1)) if m else 0
charging = "AC Power" in batt_txt
ioreg = run(["ioreg", "-r", "-c", "AppleSmartBattery"])
def ireg(key):
    mm = re.search(r'"%s" = (\d+)' % key, ioreg)
    return num(mm.group(1)) if mm else 0
cycles = ireg("CycleCount")
design, raw_max = ireg("DesignCapacity"), ireg("AppleRawMaxCapacity")
health = int(round(100.0 * raw_max / design)) if design and raw_max else None
temp_c = round(ireg("Temperature") / 100.0, 1) if ireg("Temperature") else None
rem = ireg("TimeRemaining")
batt_minutes = rem if 0 < rem < 65535 else None

idle = 0
mm = re.search(r"HIDIdleTime\"? = (\d+)", run(["ioreg", "-c", "IOHIDSystem"]))
if mm:
    idle = int(num(mm.group(1)) / 1e9)
# Display state: pmset powerstate has no IODisplayWrangler on Apple silicon, so infer it from the
# displaysleep setting (minutes) and the idle time; caffeinate -d keeps the display on.
display_asleep = False
mm = re.search(r"^\s*displaysleep\s+(\d+)", run(["pmset", "-g"]), re.M)
ds_min = num(mm.group(1)) if mm else 0
if ds_min > 0 and idle >= ds_min * 60 and not caffeinate:
    display_asleep = True
keychain = "unlocked" if subprocess.run(["security", "show-keychain-info", HOME + "/Library/Keychains/login.keychain-db"],
                                        capture_output=True).returncode == 0 else "locked"

# --- activity: top processes by CPU, dev servers listening ---------------------------------------
top = {}
for line in run(["ps", "-Ao", "%cpu,%mem,comm"]).splitlines()[1:]:
    f = line.split(None, 2)
    if len(f) < 3:
        continue
    cmd = f[2]
    mm = re.search(r"/([^/]+)\.app/", cmd)          # helpers inside an .app bundle count for the app
    name = mm.group(1) if mm else os.path.basename(cmd)
    t = top.setdefault(name, {"name": name, "cpu": 0.0, "mem": 0.0})
    t["cpu"] += num(f[0], float, 0.0)
    t["mem"] += num(f[1], float, 0.0)
top = sorted(top.values(), key=lambda t: -t["cpu"])[:4]
for t in top:
    t["cpu"], t["mem"] = round(t["cpu"], 1), round(t["mem"], 1)

ports = []
seen = set()
for line in run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], 8).splitlines()[1:]:
    f = line.split()
    if len(f) < 9:
        continue
    app, addr = f[0], f[8]
    port = addr.rsplit(":", 1)[-1]
    if not any(app.lower().startswith(d) for d in DEV_CMDS) or num(port) >= 16384:   # skip random high ports
        continue
    if (app, port) in seen:
        continue
    seen.add((app, port))
    ports.append({"app": app, "port": num(port)})
ports.sort(key=lambda p: p["port"])

# --- projects: every folder in ~/projects, with git summary ---------------------------------------
projects = []
now = time.time()
for d in sorted(glob.glob(PROJECTS + "/*/")):
    name = os.path.basename(d.rstrip("/"))
    if name.startswith("."):
        continue
    p = {"name": name, "path": d.rstrip("/"), "git": os.path.isdir(d + ".git")}
    if p["git"]:
        p["branch"] = run(["git", "-C", d, "rev-parse", "--abbrev-ref", "HEAD"]).strip() or "?"
        p["dirty"] = len(run(["git", "-C", d, "status", "--porcelain"]).splitlines())
        ab = run(["git", "-C", d, "rev-list", "--left-right", "--count", "HEAD...@{u}"]).split()
        p["ahead"], p["behind"] = (num(ab[0]), num(ab[1])) if len(ab) == 2 else (0, 0)
        p["last"] = num(run(["git", "-C", d, "log", "-1", "--format=%ct"]).strip())
    else:
        try:
            p["last"] = int(os.stat(d).st_mtime)
        except OSError:
            p["last"] = 0
    projects.append(p)
projects.sort(key=lambda p: -p.get("last", 0))

# --- Claude sessions started today (jsonl files touched since midnight) ---------------------------
midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
claude_today = 0
for f in glob.glob(HOME + "/.claude/projects/*/*.jsonl"):
    try:
        if os.stat(f).st_mtime >= midnight:
            claude_today += 1
    except OSError:
        pass

screen_ts = None
try:
    screen_ts = int(os.stat(os.path.join(CACHE, "screen.jpg")).st_mtime)
except OSError:
    pass

out = {
    "state": "online",
    # compat keys (Home dashboard + older template sensors)
    "sessions": ",".join(s["name"] for s in sessions) or "none",
    "session_count": len(sessions),          # hosts (one per folder)
    "active_sessions": active_sessions,      # sessions served by all hosts
    "session_links": " ".join("%s=%s" % (s["name"], s["link"] or "none") for s in sessions),
    "antigravity": apps.get("Antigravity", False), "cursor": apps.get("Cursor", False), "zed": apps.get("Zed", False),
    # new
    "sessions_json": sessions,
    "claude_today": claude_today,
    "apps": apps,
    "caffeinate": caffeinate, "caffeinate_until": caff_until,
    "uptime_hours": up_h, "load": load,
    "disk_free": disk_free, "disk_used_pct": disk_pct,
    "mem_free_mb": mem_free_mb, "mem_free_pct": mem_free_pct,
    "ip": ip,
    "battery": battery, "charging": charging, "battery_cycles": cycles, "battery_health": health,
    "battery_temp": temp_c, "battery_minutes": batt_minutes,
    "keychain": keychain, "idle_seconds": idle, "display_asleep": display_asleep,
    "top": top, "ports": ports, "projects": projects,
    "screen_ts": screen_ts,
    "updated": time.strftime("%H:%M:%S"),
}
print(json.dumps(out, separators=(",", ":")))
