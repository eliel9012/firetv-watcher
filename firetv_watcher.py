#!/usr/bin/env python3
import json, os, re, subprocess, time, urllib.request, urllib.parse, html
from datetime import datetime

HOST = os.environ.get("HOST", "10.0.1.16")   # ajuste seu IP aqui ou em /etc/default/firetv-watcher
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

# Mapeia pacotes -> nome bonitinho + emoji
KNOWN_APPS = {
    "com.amazon.tv.launcher":        ("Home", "🏠"),
    "br.com.claro.now.smarttvclient":("ClaroTV+", "📺"),
    "org.jellyfin.androidtv":        ("Jellyfin", "🍿"),
    "com.google.android.youtube.tv": ("YouTube", "▶️"),
    "com.netflix.ninja":             ("Netflix", "🎬"),
    "com.disney.disneyplus":         ("Disney+", "🧞"),
    "com.hbo.hbonow":                ("HBO", "🟣"),
    "com.spotify.tv.android":        ("Spotify", "🎵"),
    "com.amazon.avod":               ("Prime Video", "🎬"),
}

def sh(args, timeout=4):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=timeout, text=True).stdout

def adb_connect():
    subprocess.run(["adb","connect",f"{HOST}:{PORT}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def adb_state():
    try:
        return sh(ADB + ["get-state"]).strip()
    except Exception:
        return ""

def ensure_adb():
    st = adb_state()
    if st == "device": return "device"
    adb_connect(); time.sleep(1)
    return adb_state()

def dumpsys(what):
    try:
        return sh(ADB + ["shell","dumpsys",what])
    except Exception:
        return ""

def get_media_state():
    m = re.search(r"state=(\d+)", dumpsys("media_session") or "")
    code = int(m.group(1)) if m else -1
    names = {0:"NONE",1:"STOPPED",2:"PAUSED",3:"PLAYING",6:"BUFFERING",8:"CONNECTING"}
    return code, names.get(code, "UNKNOWN")

def clean_pkg(s: str) -> str:
    return re.sub(r'^\s*u\d+\s+','', (s or '').strip())

def get_top_app():
    out = sh(ADB + ["shell","dumpsys","window","windows"])
    m = re.search(r"mCurrentFocus=Window\{[^\s]+ ([^/]+)/", out)
    if not m:
        m = re.search(r"mFocusedApp=.*\s([a-zA-Z0-9._]+?)/", out)
    return clean_pkg(m.group(1)) if m else ""

def tv_power_status():
    out = dumpsys("hdmi_control")
    m = re.search(r"mPowerStatus:\s*(\d+)", out)
    return int(m.group(1)) if m else -1

def display_on_via_power():
    out = dumpsys("power") or ""
    if re.search(r"Display\s*Power:\s*state\s*=\s*ON", out, re.I): return True
    if re.search(r"\bmScreenState\s*=\s*ON\b", out, re.I): return True
    if re.search(r"\bmActualState\s*=\s*ON\b", out, re.I): return True
    if re.search(r"\bmWakefulness\s*=\s*Awake\b", out, re.I): return True
    return False

def display_on_via_display():
    out = dumpsys("display") or ""
    if re.search(r"\bmScreenState\s*=\s*ON\b", out, re.I): return True
    if re.search(r"\bmActualState\s*=\s*ON\b", out, re.I): return True
    if re.search(r"\bmGlobalDisplayState\s*=\s*ON\b", out, re.I): return True
    return False

def get_kbps(prev=None, prev_t=None):
    out = sh(ADB + ["shell","cat","/proc/net/dev"])
    rx = tx = None
    for ln in out.splitlines():
        if "wlan0:" in ln or "eth0:" in ln:
            parts = re.split(r"[:\s]+", ln.strip())
            if len(parts) >= 10:
                r, t = int(parts[1]), int(parts[9]); rx, tx = r, t; break
    ts = time.time(); kb_in = kb_out = 0.0
    if rx is not None and prev and prev_t:
        dt = ts - prev_t
        if dt > 0:
            kb_in  = (rx - prev[0]) * 8.0 / 1000.0 / dt
            kb_out = (tx - prev[1]) * 8.0 / 1000.0 / dt
    return kb_in, kb_out, (rx, tx), ts

def pretty_app(pkg: str) -> str:
    if not pkg: return "—"
    pkg = clean_pkg(pkg)
    name, emoji = KNOWN_APPS.get(pkg, (None, "📦"))
    if name:
        return f"{emoji} <b>{html.escape(name)}</b> <code>{html.escape(pkg)}</code>"
    return f"{emoji} <code>{html.escape(pkg)}</code>"

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true"
        }).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                               data=data, timeout=5).read()
    except Exception:
        pass

def read_state():
    try:
        with open(STATEFILE,"r") as f: return json.load(f)
    except: return {}

def write_state(d):
    try:
        with open(STATEFILE,"w") as f: json.dump(d,f)
    except: pass

def main():
    last = read_state()
    last_app   = last.get("app","")
    last_tv_on = last.get("tv_on")
    last_flip_ts = 0.0
    prev_bytes = None; prev_t = None
    first = True; adb_ok_last = None

    while True:
        st = ensure_adb()
        adb_ok = (st == "device")
        if adb_ok_last is None: adb_ok_last = adb_ok
        elif adb_ok != adb_ok_last:
            tg_send("✅ <b>ADB reconectado</b>" if adb_ok else "⚠️ <b>ADB desconectado</b> — autorize a chave na TV")
            adb_ok_last = adb_ok
        if not adb_ok:
            time.sleep(INTERVAL); continue

        kb_in, kb_out, prev, t = get_kbps(prev_bytes, prev_t)
        if prev is not None: prev_bytes, prev_t = prev, t

        code, media = get_media_state()
        app = get_top_app()
        pwr = tv_power_status()                 # 0 on, 1 standby, 2 to_on, 3 to_standby, -1 unknown
        disp_on = display_on_via_power() or display_on_via_display()
        tv_on = disp_on or (pwr in (0, 2))
        if not tv_on and (code in (3,6,8) and kb_in >= ACTIVE_KBPS):
            tv_on = True

        # Log local
        now = datetime.now().strftime("%H:%M:%S")
        print(f"{now} TV={'ON' if tv_on else 'OFF'}  pwr={pwr} disp={disp_on}  app={app or '-':26}  media={media:9}  in={kb_in:.0f}kbps")
        try:
            with open(OUTFILE,"a") as f:
                f.write(json.dumps({
                    "ts": datetime.utcnow().isoformat()+"Z",
                    "tv_on": tv_on, "pwr": pwr, "display_on": disp_on,
                    "app": app, "media": media,
                    "kbps_in": round(kb_in,1), "kbps_out": round(kb_out,1)
                })+"\n")
        except: pass

        # Notificações
        now_ts = time.time()
        if last_tv_on is not None and tv_on != last_tv_on:
            if now_ts - last_flip_ts >= DEBOUNCE_SEC:
                if tv_on:
                    tg_send(f"📺 TV <b>LIGADA</b>\nApp atual: {pretty_app(app)}")
                else:
                    tg_send("💤 TV <b>DESLIGADA</b> ou trocou de entrada")
                last_tv_on = tv_on; last_flip_ts = now_ts

        if tv_on and app and app != last_app:
            tg_send(f"🔁 <b>Mudou de aplicativo</b>\n{pretty_app(last_app)}\n<b>→</b> {pretty_app(app)}")
            last_app = app

        write_state({"tv_on": last_tv_on if last_tv_on is not None else tv_on, "app": last_app})

        if first:
            tg_send(f"🔔 <b>Estado inicial</b>\n"
                    f"TV: <b>{'ON' if tv_on else 'OFF'}</b> • pwr=<code>{pwr}</code>\n"
                    f"App: {pretty_app(app)}\n"
                    f"Mídia: <code>{html.escape(media)}</code>")
            first = False

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
