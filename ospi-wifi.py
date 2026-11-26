#!/usr/bin/env python3
"""
ospi-wifi - WiFi fallback hotspot and setup console for OpenSprinkler Pi.

* Joins a saved WiFi network when one is in range (NetworkManager does the work).
* If the Pi has no WiFi connection, it tries the saved networks, then starts a
  setup hotspot so a phone can connect.
* A small web console (default port 8090) lets anyone pick a network and enter
  its password, forget saved networks, or turn the hotspot on.
* While the hotspot is up, every web request on port 80 is redirected to the
  console, so most phones pop up a "sign in to network" page by themselves.
  On the home network, port 80 redirects to the OpenSprinkler UI instead.

Requires NetworkManager (Raspberry Pi OS Bookworm or later). Runs as root.
Python standard library only.
"""

import base64
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0"
CONF_PATH = os.environ.get("OSPI_WIFI_CONF", "/etc/ospi-wifi.conf")
STATE_DIR = os.environ.get("OSPI_WIFI_STATE", "/var/lib/ospi-wifi")
SYSFS_NET = os.environ.get("OSPI_WIFI_SYSFS", "/sys/class/net")
PORTAL_DNS_CONF = os.environ.get(
    "OSPI_WIFI_DNS_CONF", "/etc/NetworkManager/dnsmasq-shared.d/ospi-wifi-portal.conf")

DEFAULTS = {
    "IFACE": "wlan0",
    "WIFI_COUNTRY": "AU",            # used by install.sh only
    "HOTSPOT_CON": "OSPi-Hotspot",   # NetworkManager profile name
    "HOTSPOT_SSID": "",              # blank = OSPi-Setup-XXXX (end of MAC)
    "HOTSPOT_PASSWORD": "opensprinkler",  # blank = open network
    "HOTSPOT_ADDRESS": "192.168.4.1/24",
    "HOTSPOT_CHANNEL": "6",
    "CONSOLE_PORT": "8090",
    "CONSOLE_PASSWORD": "",          # asked for on the home network only
    "OSPI_PORT": "8080",
    "PORTAL_PORT": "80",             # 0 = no captive portal / port-80 redirect
    "BOOT_GRACE": "90",
    "DISCONNECT_GRACE": "60",
    "RETRY_INTERVAL": "300",         # 0 = never leave the hotspot by itself
    "CONNECT_TIMEOUT": "45",
    "DEBUG": "no",
}

log = logging.getLogger("ospi-wifi")

CFG = dict(DEFAULTS)
IFACE = HOTSPOT_CON = HOTSPOT_SSID = HOTSPOT_IP = ""
HOTSPOT_NET = None

LOCK = threading.Lock()          # held for anything that changes WiFi state
LAST_WEB = -1e9                  # monotonic time of last console request
MSG = {"text": "", "error": False, "time": 0.0}
SCAN = {"time": 0.0, "nets": []}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path):
    cfg = dict(DEFAULTS)
    try:
        with open(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                if key in cfg:
                    cfg[key] = val
                else:
                    log.warning("Unknown setting %s in %s", key, path)
    except FileNotFoundError:
        log.warning("%s not found, using defaults", path)
    return cfg


def cfg_int(key):
    try:
        return int(CFG[key])
    except ValueError:
        return int(DEFAULTS[key])


def set_msg(text, error=False):
    MSG.update(text=text, error=error, time=time.time())
    (log.warning if error else log.info)(text)


# --------------------------------------------------------------------------
# Command helpers
# --------------------------------------------------------------------------

def _redact(args):
    out, hide = [], False
    for a in args:
        out.append("****" if hide else a)
        hide = a in ("wifi-sec.psk", "password")
    return " ".join(out)


def run(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except FileNotFoundError:
        return 127, "", args[0] + " not found"


def nm(*args, timeout=60):
    rc, out, err = run(["nmcli", *args], timeout=timeout)
    if rc != 0:
        log.debug("nmcli %s -> rc=%s %s", _redact(args), rc, err)
    return rc, out, err


def split_terse(line):
    """Split an `nmcli -t` line; ':' and '\\' inside values are backslash-escaped."""
    fields, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def terse(*args, timeout=60):
    rc, out, err = nm("-t", *args, timeout=timeout)
    return rc, [split_terse(l) for l in out.splitlines() if l], err


def get_value(conn, field):
    rc, out, _ = nm("-g", field, "connection", "show", conn)
    if rc != 0:
        return ""
    return split_terse(out.rstrip("\n"))[0] if out.strip() else ""


def short_err(err):
    err = (err or "").strip().splitlines()
    return err[-1][:200] if err else "unknown error"


# --------------------------------------------------------------------------
# Network state
# --------------------------------------------------------------------------

def ipv4_of(dev):
    rc, out, _ = run(["ip", "-4", "-o", "addr", "show", "dev", dev], timeout=5)
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out) if rc == 0 else []


def get_status():
    _, rows, _ = terse("-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
    devs = {r[0]: {"type": r[1], "state": r[2], "con": r[3]} for r in rows if len(r) >= 4}
    st = {"mode": "missing", "con": "", "ssid": "", "ips": [], "eth": []}
    w = devs.get(IFACE)
    if w:
        con = "" if w["con"] in ("", "--") else w["con"]
        state = w["state"]
        if state.startswith("connected") and con == HOTSPOT_CON:
            st["mode"] = "hotspot"
        elif state.startswith("connected") and con:
            st["mode"] = "client"
            st["ssid"] = get_value(con, "802-11-wireless.ssid") or con
        elif state.startswith("connecting"):
            st["mode"] = "connecting"
        elif state == "unavailable":
            st["mode"] = "unavailable"
        else:
            st["mode"] = "disconnected"
        st["con"] = con
        st["ips"] = ipv4_of(IFACE)
    for name, d in devs.items():
        if d["type"] == "ethernet" and d["state"].startswith("connected"):
            st["eth"].append((name, ipv4_of(name)))
    return st


def default_hotspot_ssid():
    try:
        with open(f"{SYSFS_NET}/{IFACE}/address") as fh:
            mac = fh.read().strip().replace(":", "")
    except OSError:
        mac = ""
    return "OSPi-Setup" + ("-" + mac[-4:].upper() if mac else "")


def saved_profiles():
    """Saved client WiFi profiles (not the hotspot)."""
    _, rows, _ = terse("-f", "NAME,TYPE", "connection", "show")
    out = []
    for r in rows:
        if len(r) < 2 or r[1] != "802-11-wireless" or r[0] == HOTSPOT_CON:
            continue
        if get_value(r[0], "802-11-wireless.mode") == "ap":
            continue
        out.append({
            "name": r[0],
            "ssid": get_value(r[0], "802-11-wireless.ssid") or r[0],
            "hidden": get_value(r[0], "802-11-wireless.hidden") == "yes",
        })
    return out


def hotspot_clients():
    rc, out, _ = run(["iw", "dev", IFACE, "station", "dump"], timeout=10)
    if rc != 0:
        return None
    return sum(1 for l in out.splitlines() if l.startswith("Station "))


# --------------------------------------------------------------------------
# Scanning (not possible while the radio is a hotspot, so results are cached)
# --------------------------------------------------------------------------

def load_scan_cache():
    try:
        with open(os.path.join(STATE_DIR, "scan.json")) as fh:
            SCAN.update(json.load(fh))
    except (OSError, ValueError):
        pass


def scan_networks(rescan="yes"):
    rc, rows, err = terse("-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                          "ifname", IFACE, "--rescan", rescan, timeout=40)
    if rc != 0:
        log.info("Scan failed: %s", short_err(err))
        return None
    nets = {}
    for r in rows:
        if len(r) < 3 or not r[0] or r[0] == HOTSPOT_SSID:
            continue
        try:
            sig = int(r[1])
        except ValueError:
            sig = 0
        sec = "" if r[2] in ("", "--") else r[2]
        if r[0] not in nets or sig > nets[r[0]]["signal"]:
            nets[r[0]] = {"ssid": r[0], "signal": sig, "security": sec}
    lst = sorted(nets.values(), key=lambda n: -n["signal"])
    if lst:
        SCAN.update(time=time.time(), nets=lst)
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "scan.json"), "w") as fh:
                json.dump(SCAN, fh)
        except OSError:
            pass
    return lst


# --------------------------------------------------------------------------
# Hotspot and connection actions (call with LOCK held)
# --------------------------------------------------------------------------

def write_portal_dns():
    try:
        if cfg_int("PORTAL_PORT") > 0:
            content = ("# Written by ospi-wifi. Only used while the setup hotspot is on:\n"
                       "# every DNS name points at the Pi so phones show the setup page.\n"
                       f"address=/#/{HOTSPOT_IP}\n")
            try:
                with open(PORTAL_DNS_CONF) as fh:
                    if fh.read() == content:
                        return
            except OSError:
                pass
            os.makedirs(os.path.dirname(PORTAL_DNS_CONF), exist_ok=True)
            with open(PORTAL_DNS_CONF, "w") as fh:
                fh.write(content)
        elif os.path.exists(PORTAL_DNS_CONF):
            os.remove(PORTAL_DNS_CONF)
    except OSError as e:
        log.warning("Couldn't update %s: %s", PORTAL_DNS_CONF, e)


def ensure_hotspot_profile():
    _, rows, _ = terse("-f", "NAME", "connection", "show")
    if any(r and r[0] == HOTSPOT_CON for r in rows):
        _, act, _ = terse("-f", "NAME", "connection", "show", "--active")
        if any(r and r[0] == HOTSPOT_CON for r in act):
            log.info("Hotspot is active; config changes apply next time it starts")
            return True
        nm("connection", "delete", HOTSPOT_CON)
    args = ["connection", "add", "type", "wifi", "ifname", IFACE,
            "con-name", HOTSPOT_CON, "ssid", HOTSPOT_SSID,
            "connection.autoconnect", "no",
            "802-11-wireless.mode", "ap",
            "802-11-wireless.band", "bg",
            "802-11-wireless.channel", CFG["HOTSPOT_CHANNEL"],
            "ipv4.method", "shared",
            "ipv4.addresses", CFG["HOTSPOT_ADDRESS"],
            "ipv6.method", "disabled"]
    pw = CFG["HOTSPOT_PASSWORD"]
    if pw:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", pw,
                 "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
                 "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable"]
    rc, _, err = nm(*args)
    if rc != 0:
        set_msg(f"Couldn't create the hotspot profile: {short_err(err)}", error=True)
        return False
    log.info("Hotspot profile %s ready (SSID %s)", HOTSPOT_CON, HOTSPOT_SSID)
    return True


def start_hotspot():
    if get_status()["mode"] != "hotspot":
        scan_networks()  # last chance to see nearby networks before the radio is busy
    rc, _, err = nm("--wait", "30", "connection", "up", HOTSPOT_CON, timeout=45)
    if rc == 0:
        log.info("Hotspot %s is on at %s", HOTSPOT_SSID, HOTSPOT_IP)
        return True
    set_msg(f"Couldn't start the setup hotspot: {short_err(err)}. "
            "The WiFi adapter may not support hotspot (AP) mode.", error=True)
    return False


def stop_hotspot():
    nm("connection", "down", HOTSPOT_CON, timeout=30)
    time.sleep(2)


def try_saved_networks(exclude=()):
    """Radio must not be in hotspot mode. Returns the profile name joined, or None."""
    st = get_status()
    if st["mode"] == "client":
        return st["con"]
    profiles = [p for p in saved_profiles() if p["name"] not in exclude]
    if not profiles:
        return None
    nets = scan_networks()
    if not nets:
        time.sleep(4)
        nets = scan_networks() or []
    visible = {n["ssid"]: n["signal"] for n in nets}
    cands = sorted((p for p in profiles if p["ssid"] in visible),
                   key=lambda p: -visible[p["ssid"]])
    cands += [p for p in profiles if p["hidden"] and p not in cands]
    t = cfg_int("CONNECT_TIMEOUT")
    for p in cands:
        log.info("Trying saved network %s", p["name"])
        rc, _, err = nm("--wait", str(t), "connection", "up", p["name"], timeout=t + 15)
        if rc == 0:
            log.info("Joined %s", p["ssid"])
            return p["name"]
        log.info("Couldn't join %s: %s", p["ssid"], short_err(err))
    return None


def recover():
    """After a failure: get back onto some network, or the hotspot."""
    if get_status()["mode"] in ("client", "hotspot"):
        return
    if not try_saved_networks():
        start_hotspot()


def key_mgmt_for(ssid):
    for n in SCAN["nets"]:
        if n["ssid"] == ssid:
            sec = n["security"]
            if "WPA3" in sec and "WPA2" not in sec and "WPA1" not in sec:
                return "sae"
    return "wpa-psk"


def do_user_connect(ssid, password, hidden):
    time.sleep(2)  # let the "connecting" page reach the phone first
    with LOCK:
        tmp = "ospi-new-" + ssid
        nm("connection", "delete", tmp)
        args = ["connection", "add", "type", "wifi", "ifname", IFACE,
                "con-name", tmp, "ssid", ssid, "connection.autoconnect", "yes"]
        if hidden:
            args += ["802-11-wireless.hidden", "yes"]
        if password:
            args += ["wifi-sec.key-mgmt", key_mgmt_for(ssid), "wifi-sec.psk", password]
        rc, _, err = nm(*args)
        if rc != 0:
            set_msg(f"Couldn't save the settings for {ssid}: {short_err(err)}", error=True)
            return
        if get_status()["mode"] == "hotspot":
            stop_hotspot()
        t = cfg_int("CONNECT_TIMEOUT")
        rc, _, err = nm("--wait", str(t), "connection", "up", tmp, timeout=t + 15)
        if rc == 0:
            for p in saved_profiles():
                if p["ssid"] == ssid and p["name"] != tmp:
                    nm("connection", "delete", p["name"])
            nm("connection", "modify", tmp, "connection.id", ssid)
            set_msg(f"Connected to {ssid}.")
        else:
            nm("connection", "delete", tmp)
            set_msg(f"Couldn't join {ssid}. Check the password and try again. "
                    f"({short_err(err)})", error=True)
            recover()


def do_user_hotspot():
    time.sleep(2)
    with LOCK:
        if get_status()["mode"] != "hotspot":
            if start_hotspot():
                set_msg(f"Setup hotspot {HOTSPOT_SSID} turned on from the console.")
            else:
                recover()


# --------------------------------------------------------------------------
# Monitor loop
# --------------------------------------------------------------------------

def monitor():
    boot = time.monotonic()
    down_since = None
    next_try = 0.0
    last_retry = time.monotonic()
    boot_grace, grace = cfg_int("BOOT_GRACE"), cfg_int("DISCONNECT_GRACE")
    interval = cfg_int("RETRY_INTERVAL")
    while True:
        time.sleep(5)
        if not LOCK.acquire(blocking=False):
            continue  # someone is using the console
        try:
            now = time.monotonic()
            st = get_status()
            mode = st["mode"]
            if mode in ("missing", "client"):
                down_since = None
                continue
            if mode == "hotspot":
                down_since = None
                if interval <= 0 or now - last_retry < interval:
                    continue
                last_retry = now
                if now - LAST_WEB < interval or hotspot_clients():
                    continue  # someone is (or was recently) using the hotspot
                if not saved_profiles():
                    continue
                log.info("Hotspot idle; checking whether a saved network is back")
                stop_hotspot()
                joined = try_saved_networks()
                if joined:
                    set_msg(f"Rejoined {joined}; setup hotspot turned off.")
                else:
                    start_hotspot()
                continue
            # disconnected, connecting or unavailable
            if down_since is None:
                down_since = now
            wait = boot_grace if now - boot < boot_grace else grace
            if now - down_since < wait or now < next_try:
                continue
            log.info("No WiFi connection for %ds", now - down_since)
            if try_saved_networks():
                down_since = None
            elif start_hotspot():
                last_retry = time.monotonic()
                down_since = None
            else:
                next_try = now + max(interval, 120)
        except Exception:
            log.exception("Monitor error")
        finally:
            LOCK.release()


# --------------------------------------------------------------------------
# Web console
# --------------------------------------------------------------------------

CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f3f5f7;color:#1d2329}
main{max-width:34rem;margin:0 auto;padding:1rem}
h1{font-size:1.35rem;margin:.4rem 0 1rem}
h2{font-size:1.05rem;margin:0 0 .6rem}
section{background:#fff;border-radius:10px;padding:1rem;margin-bottom:1rem;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.msg{padding:.75rem 1rem;border-radius:8px;margin-bottom:1rem;background:#e7f6ec;border:1px solid #9fd6b1}
.msg.err{background:#fdecea;border-color:#f2a9a1}
.row{display:flex;justify-content:space-between;gap:1rem;padding:.25rem 0}
.muted{color:#66707a;font-size:.9rem}
label.net{display:flex;align-items:center;gap:.6rem;padding:.55rem .4rem;border-bottom:1px solid #eef0f2;cursor:pointer}
label.net span.s{margin-left:auto;color:#66707a;font-size:.85rem;white-space:nowrap}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:.65rem;font-size:1rem;border:1px solid #c8ced4;border-radius:6px;margin:.3rem 0 .6rem}
button{font-size:1rem;padding:.65rem 1.1rem;border-radius:6px;border:0;background:#1f6feb;color:#fff;cursor:pointer}
button.sec{background:#e9edf1;color:#1d2329}
button.warn{background:#c0392b}
form.inline{display:inline}
a{color:#1f6feb}
ol{padding-left:1.2rem}li{margin:.3rem 0}
"""


def page(title, body):
    return (f"<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{body}<p class=muted>ospi-wifi {VERSION} on "
            f"{html.escape(socket.gethostname())}</p></main></body></html>")


def bars(sig):
    return "▂▄▆█"[: max(1, min(4, (sig + 24) // 25))]


def ospi_links(st):
    host = socket.gethostname()
    links = [f"http://{host}.local:{CFG['OSPI_PORT']}/"]
    for ip in st["ips"] + [ip for _, ips in st["eth"] for ip in ips]:
        if ip != HOTSPOT_IP:
            links.append(f"http://{ip}:{CFG['OSPI_PORT']}/")
    return " &nbsp;".join(f"<a href='{html.escape(u)}'>{html.escape(u)}</a>" for u in links)


def render_home(st, scanned_live):
    e = html.escape
    out = ["<h1>OpenSprinkler WiFi setup</h1>"]
    if MSG["text"] and time.time() - MSG["time"] < 900:
        out.append(f"<div class='msg{' err' if MSG['error'] else ''}'>{e(MSG['text'])}</div>")

    mode = st["mode"]
    wifi = {
        "client": f"Connected to <b>{e(st['ssid'])}</b> ({e(', '.join(st['ips']) or 'no address yet')})",
        "hotspot": f"Setup hotspot <b>{e(HOTSPOT_SSID)}</b> is on",
        "connecting": "Connecting…",
        "disconnected": "Not connected",
        "unavailable": "WiFi adapter unavailable (blocked, or country not set)",
        "missing": f"No WiFi adapter found ({e(IFACE)})",
    }[mode]
    out.append("<section><h2>Status</h2>")
    out.append(f"<div class=row><span>WiFi</span><span>{wifi}</span></div>")
    for name, ips in st["eth"]:
        out.append(f"<div class=row><span>Ethernet</span><span>Connected "
                   f"({e(', '.join(ips) or name)})</span></div>")
    if mode == "client" or st["eth"]:
        out.append(f"<p class=muted>OpenSprinkler: {ospi_links(st)}</p>")
    out.append("</section>")

    out.append("<section><h2>Join a WiFi network</h2><form method=post action=/connect>")
    nets = SCAN["nets"]
    if nets:
        for i, n in enumerate(nets[:20]):
            lock = " 🔒" if n["security"] else ""
            cur = " checked" if mode == "client" and n["ssid"] == st["ssid"] else ""
            out.append(f"<label class=net><input type=radio name=ssid value='{e(n['ssid'])}'{cur}>"
                       f"{e(n['ssid'])}{lock}<span class=s>{bars(n['signal'])} {n['signal']}%</span></label>")
    else:
        out.append("<p class=muted>No networks found yet.</p>")
    if mode == "hotspot" and SCAN["time"]:
        when = time.strftime("%H:%M", time.localtime(SCAN["time"]))
        out.append(f"<p class=muted>This list was taken at {when}, before the hotspot started "
                   "(the adapter can't scan while it's a hotspot). "
                   "If your network isn't listed, type its name below.</p>")
    out.append("<p style='margin:.8rem 0 0'>Other network (type the name):</p>"
               "<input type=text name=ssid_other autocomplete=off autocapitalize=none "
               "placeholder='Network name (SSID)'>"
               "<label class=muted><input type=checkbox name=hidden> Hidden network</label>"
               "<p style='margin:.8rem 0 0'>Password:</p>"
               "<input type=password name=password id=pw autocomplete=off>"
               "<label class=muted><input type=checkbox "
               "onclick=\"document.getElementById('pw').type=this.checked?'text':'password'\">"
               " Show password</label><p><button>Connect</button></p></form>")
    if mode != "hotspot":
        out.append("<form method=post action=/scan class=inline>"
                   "<button class=sec>Scan again</button></form>")
    out.append("</section>")

    saved = saved_profiles()
    out.append("<section><h2>Saved networks</h2>")
    if saved:
        for p in saved:
            label = e(p["ssid"]) + (f" <span class=muted>({e(p['name'])})</span>"
                                    if p["name"] != p["ssid"] else "")
            out.append(f"<div class=row><span>{label}</span>"
                       "<form method=post action=/forget class=inline "
                       "onsubmit=\"return confirm('Forget this network?')\">"
                       f"<input type=hidden name=name value='{e(p['name'])}'>"
                       "<button class='sec'>Forget</button></form></div>")
    else:
        out.append("<p class=muted>None yet.</p>")
    out.append("</section>")

    if mode != "hotspot":
        out.append("<section><h2>Setup hotspot</h2>"
                   f"<p class=muted>Turns WiFi into the hotspot <b>{e(HOTSPOT_SSID)}</b> "
                   "and disconnects from the current WiFi network. If nobody uses the hotspot, "
                   "the Pi goes back to a saved network after a few minutes.</p>"
                   "<form method=post action=/hotspot "
                   "onsubmit=\"return confirm('Turn on the setup hotspot now?')\">"
                   "<button class=warn>Turn on setup hotspot</button></form></section>")
    return page("OpenSprinkler WiFi setup", "".join(out))


def render_connecting(ssid, from_hotspot):
    e = html.escape
    host = socket.gethostname()
    url = f"http://{host}.local:{CFG['OSPI_PORT']}/"
    body = [f"<h1>Connecting to {e(ssid)}…</h1><section>"]
    if from_hotspot:
        body.append(
            "<ol>"
            "<li>The setup hotspot is turning off, so your phone will disconnect in a moment.</li>"
            f"<li>Connect your phone back to <b>{e(ssid)}</b>.</li>"
            f"<li>Open OpenSprinkler at <a href='{e(url)}'>{e(url)}</a>. If that doesn't open, "
            "find the Pi's address in your router's device list.</li></ol>"
            f"<p>If the password was wrong, the hotspot <b>{e(HOTSPOT_SSID)}</b> comes back within "
            "about a minute. Join it again and this page will show what went wrong.</p>")
    else:
        body.append(
            f"<p>The Pi is leaving its current network to join <b>{e(ssid)}</b>. "
            "This page will stop responding.</p>"
            f"<p>Connect to <b>{e(ssid)}</b> yourself, then open "
            f"<a href='{e(url)}'>{e(url)}</a>. If joining fails, the Pi goes back to a "
            f"saved network or turns on the hotspot <b>{e(HOTSPOT_SSID)}</b>.</p>")
    body.append("</section>")
    return page("Connecting", "".join(body))


def render_notice(title, text):
    return page(title, f"<h1>{html.escape(title)}</h1><section><p>{text}</p>"
                       "<p><a href='/'>Back</a></p></section>")


def valid_psk(pw):
    return (8 <= len(pw) <= 63) or (len(pw) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", pw))


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "ospi-wifi/" + VERSION

    def log_message(self, fmt, *args):
        log.debug("console %s " + fmt, self.client_address[0], *args)

    def _from_hotspot(self):
        try:
            return ipaddress.ip_address(self.client_address[0]) in HOTSPOT_NET
        except ValueError:
            return False

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, to="/"):
        self._send(303, "", headers=[("Location", to)])

    def _authorised(self):
        pw = CFG["CONSOLE_PASSWORD"]
        if not pw or self._from_hotspot():
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                given = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")[2]
                if hmac.compare_digest(given.encode(), pw.encode()):
                    return True
            except Exception:
                pass
        self._send(401, "Password required", "text/plain",
                   [("WWW-Authenticate", 'Basic realm="OSPi WiFi setup"')])
        return False

    def _touch(self):
        global LAST_WEB
        LAST_WEB = time.monotonic()

    def do_GET(self):
        self._touch()
        if not self._authorised():
            return
        path = urllib.parse.urlparse(self.path).path
        if path != "/":
            return self._redirect("/")
        st = get_status()
        live = False
        if st["mode"] != "hotspot" and not LOCK.locked():
            live = scan_networks("auto") is not None
        self._send(200, render_home(st, live))

    do_HEAD = do_GET

    def do_POST(self):
        self._touch()
        if not self._authorised():
            return
        origin = self.headers.get("Origin")
        if origin and origin != "null" and \
                urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self._send(403, "Cross-site request refused", "text/plain")
        if origin == "null" and not self._from_hotspot():
            return self._send(403, "Cross-site request refused", "text/plain")
        length = min(int(self.headers.get("Content-Length") or 0), 16384)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        f = lambda k: form.get(k, [""])[0]
        path = urllib.parse.urlparse(self.path).path

        if LOCK.locked() and path in ("/connect", "/hotspot", "/scan"):
            return self._send(200, render_notice(
                "Busy", "The Pi is changing WiFi settings right now. Try again in a minute."))

        if path == "/connect":
            ssid = f("ssid_other").strip() or f("ssid")
            password = f("password")
            hidden = bool(f("hidden"))
            if not ssid or len(ssid.encode()) > 32:
                return self._send(200, render_notice(
                    "Choose a network", "Pick a network from the list or type its name."))
            secured = next((n["security"] for n in SCAN["nets"] if n["ssid"] == ssid), None)
            if password and not valid_psk(password):
                return self._send(200, render_notice(
                    "Check the password", "WiFi passwords are 8 to 63 characters long."))
            if not password and secured:
                return self._send(200, render_notice(
                    "Password needed", f"{html.escape(ssid)} needs a password."))
            from_hs = get_status()["mode"] == "hotspot"
            set_msg(f"Trying to join {ssid}…")
            self._send(200, render_connecting(ssid, from_hs))
            threading.Thread(target=do_user_connect, args=(ssid, password, hidden),
                             daemon=True).start()
            return

        if path == "/forget":
            name = f("name")
            if name in [p["name"] for p in saved_profiles()]:
                rc, _, err = nm("connection", "delete", name)
                if rc == 0:
                    set_msg(f"Forgot {name}.")
                else:
                    set_msg(f"Couldn't forget {name}: {short_err(err)}", error=True)
            return self._redirect("/")

        if path == "/hotspot":
            self._send(200, render_notice(
                "Turning on the setup hotspot",
                f"The Pi is switching to the hotspot <b>{html.escape(HOTSPOT_SSID)}</b>. "
                f"Join it, then open <b>http://{HOTSPOT_IP}:{CFG['CONSOLE_PORT']}/</b>."))
            threading.Thread(target=do_user_hotspot, daemon=True).start()
            return

        if path == "/scan":
            if get_status()["mode"] != "hotspot":
                with LOCK:
                    if scan_networks() is None:
                        set_msg("Scan failed; try again in a few seconds.", error=True)
            return self._redirect("/")

        self._redirect("/")


class PortalHandler(BaseHTTPRequestHandler):
    """Port 80: send hotspot users to the console, everyone else to OpenSprinkler."""
    server_version = "ospi-wifi/" + VERSION

    def log_message(self, fmt, *args):
        log.debug("portal %s " + fmt, self.client_address[0], *args)

    def do_GET(self):
        local_ip = self.connection.getsockname()[0]
        if local_ip == HOTSPOT_IP:
            target = f"http://{HOTSPOT_IP}:{CFG['CONSOLE_PORT']}/"
        else:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
                host = local_ip
            target = f"http://{host}:{CFG['OSPI_PORT']}/"
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_POST = do_GET


def serve(handler, port, name):
    try:
        srv = ThreadingHTTPServer(("", port), handler)
    except OSError as e:
        log.error("Couldn't open %s on port %d: %s", name, port, e)
        return
    log.info("%s listening on port %d", name, port)
    threading.Thread(target=srv.serve_forever, daemon=True, name=name).start()


# --------------------------------------------------------------------------

def wait_for_adapter():
    """USB WiFi adapters can appear well after boot (or not at all). Wait for it."""
    path = os.path.join(SYSFS_NET, IFACE)
    if os.path.exists(path):
        return
    log.warning("%s isn't present yet; waiting for the WiFi adapter to appear", IFACE)
    start = time.monotonic()
    next_note = start + 120
    while not os.path.exists(path):
        time.sleep(3)
        if time.monotonic() >= next_note:
            log.warning("Still no %s after %d s. Check the adapter with lsusb and dmesg "
                        "(a Pi 1 may need a powered USB hub).", IFACE, time.monotonic() - start)
            next_note += 600
    log.info("%s appeared after %d s", IFACE, time.monotonic() - start)
    time.sleep(3)  # let NetworkManager pick it up


def main():
    global CFG, IFACE, HOTSPOT_CON, HOTSPOT_SSID, HOTSPOT_IP, HOTSPOT_NET
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)s %(message)s")
    CFG = load_config(CONF_PATH)
    if CFG["DEBUG"].lower() in ("1", "yes", "true"):
        log.setLevel(logging.DEBUG)
    IFACE = CFG["IFACE"]
    HOTSPOT_CON = CFG["HOTSPOT_CON"]
    HOTSPOT_SSID = CFG["HOTSPOT_SSID"] or "OSPi-Setup"
    iface_addr = ipaddress.ip_interface(CFG["HOTSPOT_ADDRESS"])
    HOTSPOT_IP, HOTSPOT_NET = str(iface_addr.ip), iface_addr.network
    pw = CFG["HOTSPOT_PASSWORD"]
    if pw and not valid_psk(pw):
        log.error("HOTSPOT_PASSWORD must be 8-63 characters (or blank for an open hotspot)")
        sys.exit(1)

    rc, _, err = nm("-t", "general", "status")
    if rc != 0:
        log.error("NetworkManager isn't available (%s). ospi-wifi needs NetworkManager.",
                  short_err(err))
        sys.exit(1)
    if os.geteuid() != 0:
        log.warning("Not running as root; WiFi changes will probably fail")

    os.makedirs(STATE_DIR, exist_ok=True)
    load_scan_cache()
    write_portal_dns()
    serve(ConsoleHandler, cfg_int("CONSOLE_PORT"), "console")
    if cfg_int("PORTAL_PORT") > 0:
        serve(PortalHandler, cfg_int("PORTAL_PORT"), "portal")
    wait_for_adapter()
    # Name the hotspot only once the adapter exists, so the MAC suffix is right.
    HOTSPOT_SSID = CFG["HOTSPOT_SSID"] or default_hotspot_ssid()
    ensure_hotspot_profile()
    log.info("Watching %s; hotspot SSID %s", IFACE, HOTSPOT_SSID)
    monitor()


if __name__ == "__main__":
    main()
