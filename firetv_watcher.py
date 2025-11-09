#!/usr/bin/env python3
import json, os, re, subprocess, time, urllib.request, urllib.parse
from datetime import datetime

HOST = os.environ.get("HOST", "10.0.1.16")
PORT = os.environ.get("PORT", "5555")
INTERVAL = int(os.environ.get("INTERVAL", "5"))

OUTFILE = os.environ.get("OUTFILE", "/home/pi/firetv-usage.jsonl")
STATEFILE = os.environ.get("STATEFILE", "/home/pi/.firetv_last.json")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

IN_PLAY_KBPS = float(os.environ.get("IN_PLAY_KBPS", "1200"))
ACTIVE_KBPS  = float(os.environ.get("ACTIVE_KBPS",  "300"))
DEBOUNCE_SEC = int(os.environ.get("DEBOUNCE_SEC", "8"))

ADB = ["adb", "-s", f"{HOST}:{PORT}"]

def sh(args, timeout=4):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=timeout, text=True).stdout

def adb_connect():
    subprocess.run(["adb", "connect", f"{HOST}:{PORT}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def dumpsys(what):
    try:
        return sh(ADB + ["shell", "dumpsys", what])
    except Exception:
        return ""

def get_media_state():
    m = re.search(r"state=(\d+)", dumpsys("media_session") or "")
    code = int(m.group(1)) if m else -1
    names = {
        0:"NONE",1:"STOPPED",2:"PAUSED",3:"PLAYING",
        6:"BUFFERING",8:"CONNECTING"
    }
    return code, names.get(code, "UNKNOWN")

def get_top_app():
    out = sh(ADB + ["shell","dumpsys","window","windows"])
    m = re.search(r"mCurrentFocus=Window\{[^\s]+ ([^/]+)/", out)
    return m.group(1) if m else ""

def tv_power_status():
    out = dumpsys("hdmi_control")
    m = re.search(r"mPowerStatus:\s*(\d+)", out)
    return int(m.group(1)) if m else -1

def tv_on_via_display():
    out = dumpsys("display") or ""
    if re.search(r"mScreenState\s*=\s*ON", out, flags=re.I): return True
    if re.search(r"mActualState\s*=\s*ON", out, flags=re.I): return True
    return False

def get_kbps(prev=None, prev_t=None):
    out = sh(ADB + ["shell","cat","/proc/net/dev"])
    rx = tx = None
    for ln in out.splitlines():
        if "wlan0:" in ln or "eth0:" in ln:
            parts = re.split(r"[:\s]+", ln.strip())
            if len(parts) >= 10:
                r, t = int(parts[1]), int(parts[9])
                rx, tx = r, t
                break
    ts = time.time()
    kb_in = kb_out = 0.0
    if rx is not None and prev and prev_t:
        dt = ts - prev_t
        if dt > 0:
            kb_in  = (rx - prev[0]) * 8.0 / 1000.0 / dt
            kb_out = (tx - prev[1]) * 8.0 / 1000.0 / dt
    return kb_in, kb_out, (rx, tx), ts

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=data, timeout=5
        ).read()
    except Exception:
        pass

def read_state():
    try:
        with open(STATEFILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def write_state(d):
    try:
        with open(STATEFILE, "w") as f:
            json.dump(d, f)
    except Exception:
        pass

def main():
    adb_connect()
    st = read_state()
    last_app   = st.get("app", "")
    last_tv_on = st.get("tv_on")      # True/False/None
    last_flip_ts = 0.0

    prev_bytes = None
    prev_t = None
    first = True

    while True:
        kb_in, kb_out, prev, t = get_kbps(prev_bytes, prev_t)
        if prev is not None:
            prev_bytes, prev_t = prev, t

        code, media = get_media_state()
        app = get_top_app()

        pwr = tv_power_status()           # 0 on, 1 standby, 2 to_on, 3 to_standby
        disp_on = tv_on_via_display()
        tv_on = (pwr in (0, 2)) or disp_on

        # fallback: se CEC não refletir, mas há reprodução + tráfego
        if not tv_on and (code in (3, 6, 8) and kb_in >= ACTIVE_KBPS):
            tv_on = True

        now = datetime.now().strftime("%H:%M:%S")
        print(
            f"{now} TV={'ON' if tv_on else 'OFF'}  "
            f"pwr={pwr} disp={disp_on}  "
            f"app={app or '-':26}  media={media:9}  in={kb_in:.0f}kbps"
        )

        try:
            with open(OUTFILE, "a") as f:
                f.write(json.dumps({
                    "ts": datetime.utcnow().isoformat()+"Z",
                    "tv_on": tv_on,
                    "pwr": pwr,
                    "display_on": disp_on,
                    "app": app,
                    "media": media,
                    "kbps_in": round(kb_in, 1),
                    "kbps_out": round(kb_out, 1),
                }) + "\n")
        except Exception:
            pass

        now_ts = time.time()
        if last_tv_on is not None and tv_on != last_tv_on:
            if now_ts - last_flip_ts >= DEBOUNCE_SEC:
                tg_send(
                    "📺 A TV **foi ligada** (CEC/Display indicam ON)"
                    if tv_on else
                    "💤 A TV **foi desligada** ou trocou de entrada"
                )
                last_tv_on = tv_on
                last_flip_ts = now_ts

        if tv_on and app and app != last_app:
            tg_send(f"🔄 Mudou de aplicativo: {last_app or 'nenhum'} → {app}")
            last_app = app

        write_state({
            "tv_on": last_tv_on if last_tv_on is not None else tv_on,
            "app": last_app,
        })

        if first:
            tg_send(
                f"🔔 Estado inicial: TV={'ON' if tv_on else 'OFF'} "
                f"| app={app or '-'} | media={media} | pwr={pwr}"
            )
            first = False

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
