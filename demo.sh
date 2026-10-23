#!/bin/bash
# Bring the whole demo up, or take it down. One command, because on review day
# nobody wants to remember six.
#
#   ./demo.sh up     ngrok + backend + frontend + SIP, then prints the link
#   ./demo.sh down   stops all of it, including the keep-awake
#   ./demo.sh link   just prints the current link
#
# A free ngrok URL changes every restart, so `up` rewrites PUBLIC_HOST and
# FRONTEND_URL in backend/.env, NEXT_PUBLIC_BACKEND_WS_URL in
# frontend/.env.local, and re-points the Twilio SIP domain. Skipping any of
# those is what breaks the phone and the shared link.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT_WEB=3001          # 3000 is the user's other project (skyler-ai)
PY="$ROOT/backend/.venv/bin/python"

tunnels() { curl -s http://127.0.0.1:4040/api/tunnels; }

tunnel_url() {  # $1 = tunnel name from ngrok.yml
  tunnels | "$PY" -c "
import sys, json
try:
    ts = json.load(sys.stdin).get('tunnels', [])
except Exception:
    sys.exit(1)
for t in ts:
    if t['name'] == '$1':
        print(t['public_url']); break
" 2>/dev/null
}

down() {
  pkill -f "uvicorn main:app" 2>/dev/null
  pkill -f "next dev" 2>/dev/null
  pkill -f "ngrok start" 2>/dev/null
  pkill -f "ngrok http" 2>/dev/null
  pkill -f "caffeinate -dimsu" 2>/dev/null
  sleep 2
  echo "stopped: backend, frontend, ngrok, keep-awake"
}

up() {
  echo "→ stopping anything already running"
  pkill -f "uvicorn main:app" 2>/dev/null
  pkill -f "ngrok start" 2>/dev/null
  pkill -f "next dev -- -p $PORT_WEB" 2>/dev/null
  sleep 2

  echo "→ keeping the Mac awake (lid must stay open regardless)"
  nohup caffeinate -dimsu >/dev/null 2>&1 &

  echo "→ ngrok"
  nohup ngrok start --all --log=stdout > /tmp/ngrok.log 2>&1 &
  for _ in $(seq 1 30); do
    sleep 1
    API=$(tunnel_url api); WEB=$(tunnel_url web)
    [ -n "${API:-}" ] && [ -n "${WEB:-}" ] && break
  done
  if [ -z "${API:-}" ] || [ -z "${WEB:-}" ]; then
    echo "   ngrok did not come up — check /tmp/ngrok.log"; return 1
  fi
  echo "   api=$API"
  echo "   web=$WEB"

  echo "→ pointing the config at today's URLs"
  "$PY" - "$API" "$WEB" <<'PY'
import pathlib, re, sys
api, web = sys.argv[1], sys.argv[2]
host = api.replace("https://", "")
root = pathlib.Path(__file__).resolve().parent if "__file__" in dir() else pathlib.Path(".")
f = pathlib.Path("frontend/.env.local"); t = f.read_text()
t = re.sub(r"^NEXT_PUBLIC_BACKEND_WS_URL=.*$",
           f"NEXT_PUBLIC_BACKEND_WS_URL=wss://{host}/api/ws/voice", t, flags=re.M)
f.write_text(t)
b = pathlib.Path("backend/.env"); t = b.read_text()
t = re.sub(r"^PUBLIC_HOST=.*$", f"PUBLIC_HOST={host}", t, flags=re.M)
t = re.sub(r"^FRONTEND_URL=.*$", f"FRONTEND_URL={web}", t, flags=re.M)
b.write_text(t)
PY

  echo "→ backend"
  ( cd "$ROOT/backend" && : > /tmp/mc.log && nohup "$PY" -m uvicorn main:app --port 8000 > /tmp/mc.log 2>&1 & )
  echo "→ frontend"
  ( cd "$ROOT/frontend" && nohup npm run dev -- -p $PORT_WEB > /tmp/mc-front.log 2>&1 & )

  for _ in $(seq 1 60); do
    sleep 1
    grep -q "Application startup complete" /tmp/mc.log 2>/dev/null \
      && curl -fsS -o /dev/null "http://localhost:$PORT_WEB/" 2>/dev/null && break
  done

  echo "→ re-pointing the Twilio SIP domain"
  ( cd "$ROOT/backend" && "$PY" tools/setup_sip.py 2>&1 | grep -E "updated SIP|created SIP" | sed 's/^/   /' )

  echo
  echo "checks:"
  printf "   backend  :8000      %s\n" "$(curl -s -o /dev/null -w '%{http_code}' localhost:8000/api/health)"
  printf "   frontend :$PORT_WEB      %s\n" "$(curl -s -o /dev/null -w '%{http_code}' localhost:$PORT_WEB/)"
  printf "   public page         %s\n" "$(curl -s -o /dev/null -w '%{http_code}' -H 'ngrok-skip-browser-warning: 1' "$WEB/")"
  printf "   api via proxy       %s\n" "$(curl -s -o /dev/null -w '%{http_code}' -H 'ngrok-skip-browser-warning: 1' "$WEB/api/doctors/specializations")"
  printf "   active calls        %s\n" "$(curl -s localhost:8000/api/health | sed -E 's/.*active_calls[\": ]+([0-9]+).*/\1/')"
  echo
  echo "SHARE THIS:  $WEB"
  echo "local:       http://localhost:$PORT_WEB   (voice)   /login  (dashboards)"
  echo "phone:       +1 559 238 8581    SIP: mediconnect-097e72.sip.twilio.com"
}

case "${1:-up}" in
  up)   up ;;
  down) down ;;
  link) echo "web: $(tunnel_url web)"; echo "api: $(tunnel_url api)" ;;
  *)    echo "usage: ./demo.sh [up|down|link]"; exit 2 ;;
esac
