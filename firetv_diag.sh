#!/usr/bin/env bash
set -euo pipefail
HOST="${HOST:-10.0.1.16}"
PORT="${PORT:-5555}"

adb connect "$HOST:$PORT" >/dev/null 2>&1 || true

echo "== ADB STATE =="
adb -s "$HOST:$PORT" get-state || true

echo -e "\n== POWER =="
adb -s "$HOST:$PORT" shell dumpsys power | egrep -i 'Display Power|mWakefulness|mScreenState|mActualState' || true

echo -e "\n== DISPLAY =="
adb -s "$HOST:$PORT" shell dumpsys display | head -n 80 || true

echo -e "\n== HDMI CONTROL =="
adb -s "$HOST:$PORT" shell dumpsys hdmi_control | egrep -i 'mPowerStatus|mIsActiveSource|Active Source|CEC' || true

echo -e "\n== MEDIA SESSION =="
adb -s "$HOST:$PORT" shell dumpsys media_session | egrep -m3 'state=' || true

echo -e "\n== AUDIO (focus) =="
adb -s "$HOST:$PORT" shell dumpsys audio | egrep -m5 -i 'AUDIOFOCUS|Music|media' || true

echo -e "\n== TOP APP =="
adb -s "$HOST:$PORT" shell dumpsys window windows | awk '/mCurrentFocus=/{print; exit}' || true

echo -e "\n== NET (5s amostra) =="
A=$(adb -s "$HOST:$PORT" shell cat /proc/net/dev | egrep 'wlan0:|eth0:' | tr -s ' ')
sleep 5
B=$(adb -s "$HOST:$PORT" shell cat /proc/net/dev | egrep 'wlan0:|eth0:' | tr -s ' ')
echo "Antes:";  echo "$A"
echo "Depois:"; echo "$B"
