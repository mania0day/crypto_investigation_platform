#!/usr/bin/env bash
# start.sh — one command to bring CipherChain up from a fresh clone.
#
#   ./start.sh              check toolchain, install deps, start everything, follow logs
#   ./start.sh --no-follow  same, but return the shell once the URLs are up
#   ./start.sh --stop       stop whatever a previous run started
#   ./start.sh --help
#
# Ports (override with env): SERVER_PORT=4000 CLIENT_PORT=5173 BACKEND_PORT=8000
#
# Everything binds to 127.0.0.1. The investigation API mints itself a real,
# scoped API key on startup and the page it serves carries that credential — so
# anything that can reach the port can spend it. Loopback only, deliberately.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT="$PWD"
LOGS="$ROOT/logs"

SERVER_PORT="${SERVER_PORT:-4000}"
CLIENT_PORT="${CLIENT_PORT:-5173}"
BACKEND_PORT="${BACKEND_PORT:-8000}"

DO_INSTALL=1
FOLLOW=1

# ── output ────────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; N=$'\033[0m'
else
  B=''; DIM=''; R=''; G=''; Y=''; C=''; N=''
fi

# Every line the script prints also lands in logs/start.log, so a failed start is
# still readable tomorrow. The service logs are separate files; see banner().
say()  { printf '%s\n' "$*" | tee -a "$LOGS/start.log"; }
step() { say "${B}==>${N} $*"; }
info() { say "    $*"; }
warn() { say "${Y}    warning:${N} $*"; }
die()  { say "${R}error:${N} $*"; exit 1; }

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-install) DO_INSTALL=0 ;;
    --no-follow)  FOLLOW=0 ;;
    --stop)       STOP_ONLY=1 ;;
    -h|--help)    usage ;;
    *)            echo "unknown flag: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$LOGS"

# ── process control ───────────────────────────────────────────────────────────
# PIDs live in files rather than only in this shell, so --no-follow can hand the
# terminal back and --stop can still find the processes in a later session.
pidfile() { printf '%s/%s.pid' "$LOGS" "$1"; }

# Depth-first, children before parents. `npm start` execs a shell that forks
# node: signalling only npm leaves node holding the port, and the next run then
# fails its port pre-flight for no visible reason.
kill_tree() {
  local pid="$1" sig="$2" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do kill_tree "$child" "$sig"; done
  kill "-$sig" "$pid" 2>/dev/null || true
}

stop_one() {
  local name="$1" pf; pf="$(pidfile "$name")"
  [ -f "$pf" ] || return 0
  local pid; pid="$(cat "$pf")"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill_tree "$pid" TERM
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    if kill -0 "$pid" 2>/dev/null; then kill_tree "$pid" KILL; fi
  fi
  rm -f "$pf"
}

stop_all() { for n in backend server client; do stop_one "$n"; done; }

if [ -n "${STOP_ONLY:-}" ]; then
  step "stopping"
  stop_all
  info "services stopped"
  info "postgres container left running — docker stop cipherchain-dev-pg"
  exit 0
fi

cleanup() {
  local rc=$?
  trap - INT TERM EXIT
  echo
  step "shutting down"
  stop_all
  info "services stopped. logs kept in ${DIM}logs/${N}"
  info "postgres still up (fast restarts) — ${DIM}docker stop cipherchain-dev-pg${N}"
  # The log follower goes last — it keeps streaming the services' own shutdown
  # lines while stop_all works — and it is stopped by signalling the subshell
  # itself, not by walking its tree.
  #
  # Killing tail first lets awk reach EOF and exit 0, so the job completes
  # *normally*, and bash announces a normally-completed background job at the
  # next foreground command. For a pipeline that announcement is the pipeline's
  # entire source text: every Ctrl-C printed the awk program into the user's
  # terminal. Signalled instead, the job dies by signal and bash stays quiet.
  # (`disown` does not help — there is no job control in a script.)
  #
  # The subshell's children are collected first because they outlive it: killing
  # only the subshell leaves tail and awk reparented to init, still running.
  if [ -n "${FOLLOW_PID:-}" ]; then
    local kid kids; kids="$(pgrep -P "$FOLLOW_PID" 2>/dev/null || true)"
    kill -TERM "$FOLLOW_PID" 2>/dev/null || true
    for kid in $kids; do kill -TERM "$kid" 2>/dev/null || true; done
  fi
  exit $rc
}

# ── probes ────────────────────────────────────────────────────────────────────
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# Waits for a port, but gives up the moment the process behind it dies — a dead
# service should report its own error, not time out silently 10 minutes later.
wait_for() {
  local name="$1" port="$2" timeout="$3" log="$LOGS/$1.log" pid last=''
  pid="$(cat "$(pidfile "$name")")"
  for ((i = 0; i < timeout * 4; i++)); do
    if port_open "$port"; then return 0; fi
    if ! kill -0 "$pid" 2>/dev/null; then
      say "${R}    $name exited before it opened :$port${N}"
      say "${DIM}$(tail -n 25 "$log" | sed 's/^/    │ /')${N}"
      return 1
    fi
    # Echo the service's own progress markers so a slow first run (the label
    # import moves ~75,000 rows) looks like work rather than a hang.
    if [ $((i % 4)) -eq 0 ]; then
      local now; now="$(grep -a '^==>' "$log" 2>/dev/null | tail -1 || true)"
      if [ -n "$now" ] && [ "$now" != "$last" ]; then last="$now"; info "${DIM}$name: ${now#==> }${N}"; fi
    fi
    sleep 0.25
  done
  say "${R}    $name did not open :$port within ${timeout}s${N}"
  say "${DIM}$(tail -n 25 "$log" | sed 's/^/    │ /')${N}"
  return 1
}

# ── 1. toolchain ──────────────────────────────────────────────────────────────
pkg_hint() {
  if   command -v dnf    >/dev/null; then echo "sudo dnf install -y python3 python3-pip nodejs npm docker"
  elif command -v apt    >/dev/null; then echo "sudo apt install -y python3 python3-venv nodejs npm docker.io"
  elif command -v pacman >/dev/null; then echo "sudo pacman -S python nodejs npm docker"
  elif command -v zypper >/dev/null; then echo "sudo zypper install python3 nodejs npm docker"
  elif command -v brew   >/dev/null; then echo "brew install python@3.12 node && brew install --cask docker"
  else echo "install: Python 3.12+, Node 18+, Docker"
  fi
}

# Picks the newest interpreter that satisfies the floor. `python3` being too old
# is not the same as Python being absent — many distros ship both.
find_python() {
  local c
  for c in python3.14 python3.13 python3.12 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null || continue
    "$c" -m venv --help >/dev/null 2>&1 || continue   # Debian splits this into python3-venv
    command -v "$c"; return 0
  done
  return 1
}

step "checking the toolchain"
MISSING=()

if PY="$(find_python)"; then
  info "${G}ok${N}  python  $("$PY" -V 2>&1 | cut -d' ' -f2)  ${DIM}$PY${N}"
else
  if command -v python3 >/dev/null && ! python3 -m venv --help >/dev/null 2>&1; then
    MISSING+=("python3-venv (python3 is installed but cannot create virtualenvs)")
  else
    if command -v python3 >/dev/null; then
      MISSING+=("python 3.12+ (found $(python3 -V 2>&1))")
    else
      MISSING+=("python 3.12+ (none installed)")
    fi
  fi
fi

if command -v node >/dev/null; then
  NODE_MAJOR="$(node -v 2>/dev/null | sed 's/^v//; s/\..*//')"
  case "$NODE_MAJOR" in ''|*[!0-9]*) NODE_MAJOR=0 ;; esac
  if [ "$NODE_MAJOR" -ge 18 ]; then
    info "${G}ok${N}  node    $(node -v)"
  else
    MISSING+=("node 18+ (found $(node -v))")
  fi
else
  MISSING+=("node 18+")
fi

command -v npm >/dev/null && info "${G}ok${N}  npm     $(npm -v)" || MISSING+=("npm")

if command -v docker >/dev/null; then
  if docker info >/dev/null 2>&1; then
    info "${G}ok${N}  docker  $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo running)"
  else
    MISSING+=("a running Docker daemon — try: sudo systemctl start docker
             (if that works only under sudo: sudo usermod -aG docker \$USER, then log out and back in)")
  fi
else
  MISSING+=("docker — Postgres runs in a container")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
  say ""
  say "${R}Missing prerequisites:${N}"
  for m in "${MISSING[@]}"; do say "  ${R}·${N} $m"; done
  say ""
  # Deliberately printed rather than run. Installing system packages needs root,
  # and a script you downloaded should not be the thing that decides to use it.
  say "  Install them with:"
  say "    ${C}$(pkg_hint)${N}"
  say ""
  die "toolchain incomplete"
fi

# ── 2. ports ──────────────────────────────────────────────────────────────────
# Checked before installing anything, so a port clash costs a second rather than
# a five-minute npm install followed by a failure.
step "checking ports"
CLASH=0
for spec in "$BACKEND_PORT:investigation backend" "$SERVER_PORT:dashboard API" "$CLIENT_PORT:react client"; do
  p="${spec%%:*}"; what="${spec#*:}"
  if port_open "$p"; then
    say "${R}    :$p is already in use${N} (wanted for the $what)"
    CLASH=1
  else
    info "${G}ok${N}  :$p free  ${DIM}$what${N}"
  fi
done
if [ "$CLASH" -eq 1 ]; then
  say ""
  info "if that is a previous run of this script:  ${C}./start.sh --stop${N}"
  info "otherwise pick another port, e.g.:          ${C}CLIENT_PORT=5174 ./start.sh${N}"
  die "port conflict"
fi

# ── 3. dependencies ───────────────────────────────────────────────────────────
npm_install() {
  local dir="$1"
  if [ -f "$dir/package-lock.json" ]; then
    # `npm ci` installs exactly the lockfile — the point of shipping one. It
    # refuses when the lock has drifted from package.json, so fall back rather
    # than making a stale lockfile fatal on someone else's machine.
    (cd "$dir" && npm ci --no-audit --no-fund) >>"$LOGS/install.log" 2>&1 \
      || (cd "$dir" && npm install --no-audit --no-fund) >>"$LOGS/install.log" 2>&1
  else
    (cd "$dir" && npm install --no-audit --no-fund) >>"$LOGS/install.log" 2>&1
  fi
}

if [ "$DO_INSTALL" -eq 1 ]; then
  step "installing project dependencies  ${DIM}(logs/install.log)${N}"

  if [ ! -x backend/.venv/bin/python ]; then
    info "creating backend/.venv"
    "$PY" -m venv backend/.venv >>"$LOGS/install.log" 2>&1 || die "could not create backend/.venv — see logs/install.log"
  fi
  # Presence of .venv is not proof the package is installed in it: an interrupted
  # pip leaves a directory that looks finished and imports nothing.
  if ! backend/.venv/bin/python -c 'import cipherchain' >/dev/null 2>&1; then
    info "pip install -e '.[dev]'  ${DIM}(first run takes a minute)${N}"
    (cd backend && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e '.[dev]') \
      >>"$LOGS/install.log" 2>&1 || die "backend install failed — see logs/install.log"
  fi
  info "${G}ok${N}  backend  $(backend/.venv/bin/python -V 2>&1 | cut -d' ' -f2)"

  for d in server client; do
    if [ ! -d "$d/node_modules" ]; then
      info "npm install in $d/  ${DIM}(first run takes a minute)${N}"
      npm_install "$d" || die "$d install failed — see logs/install.log"
    fi
    info "${G}ok${N}  $d  $(find "$d/node_modules" -maxdepth 1 -mindepth 1 -type d | wc -l) packages"
  done
else
  step "skipping install  ${DIM}(--no-install)${N}"
fi

[ -f server/.env ] || warn "server/.env is missing — the dashboard's chain lookups will fail. See .env.example"

# ── 4. start ──────────────────────────────────────────────────────────────────
# nohup, so a --no-follow run keeps serving after the terminal that started it
# is closed. Output is already redirected, so no nohup.out appears.
launch() {
  local name="$1" dir="$2"; shift 2
  printf '\n===== %s started %s =====\n' "$name" "$(date -Is)" >>"$LOGS/$name.log"
  ( cd "$dir" && exec nohup "$@" ) >>"$LOGS/$name.log" 2>&1 &
  echo $! > "$(pidfile "$name")"
}

trap cleanup INT TERM EXIT

step "starting the investigation backend  ${DIM}:$BACKEND_PORT${N}"
# demo.sh owns the whole sequence — container, migrations, label import, key
# mint, uvicorn — and ends in `exec`, so the PID recorded here is uvicorn's.
launch backend backend env PORT="$BACKEND_PORT" ./scripts/demo.sh
# Generous: a cold start pulls the postgres image and imports ~75,000 labels.
wait_for backend "$BACKEND_PORT" 600 || die "the investigation backend did not come up"
info "${G}ok${N}  listening"

API_KEY="$(grep -ao "Bearer [A-Za-z0-9._-]*" "$LOGS/backend.log" | tail -1 | cut -d' ' -f2 || true)"

step "starting the dashboard API  ${DIM}:$SERVER_PORT${N}"
launch server server env PORT="$SERVER_PORT" npm start
wait_for server "$SERVER_PORT" 60 || die "the dashboard API did not come up"
info "${G}ok${N}  listening"

step "starting the react client  ${DIM}:$CLIENT_PORT${N}"
# Vite reads VITE_-prefixed variables straight from the environment and gives
# them priority over .env files, so the Investigate button follows whatever
# ports this run actually chose — without writing to anyone's .env.
launch client client env \
  VITE_API_BASE="http://localhost:$SERVER_PORT/api" \
  VITE_INVESTIGATE_URL="http://localhost:$BACKEND_PORT/" \
  npm run dev -- --port "$CLIENT_PORT" --strictPort --host 127.0.0.1
wait_for client "$CLIENT_PORT" 120 || die "the react client did not come up"
info "${G}ok${N}  listening"

# ── 5. banner ─────────────────────────────────────────────────────────────────
say ""
say "  ${B}${G}CipherChain is up.${N}"
say ""
say "  ${B}Dashboard${N}        ${C}http://localhost:$CLIENT_PORT/${N}   ${DIM}← start here${N}"
say "  ${B}Investigation${N}    ${C}http://localhost:$BACKEND_PORT/${N}   ${DIM}the Investigate button lands here${N}"
say "  ${B}API docs${N}         ${C}http://localhost:$BACKEND_PORT/docs${N}"
say "  ${B}Dashboard API${N}    ${C}http://localhost:$SERVER_PORT/api/health${N}"
say ""
if [ -n "$API_KEY" ]; then
  say "  ${B}API key${N}  ${DIM}(local, scoped read+investigate, revocable)${N}"
  say "    ${DIM}$API_KEY${N}"
  say "    ${DIM}curl -H 'Authorization: Bearer \$KEY' http://127.0.0.1:$BACKEND_PORT/investigations${N}"
  say ""
fi
say "  ${B}Logs${N}     ${DIM}logs/backend.log  logs/server.log  logs/client.log  logs/install.log${N}"
say ""

if [ "$FOLLOW" -eq 0 ]; then
  trap - INT TERM EXIT
  say "  Running in the background. Stop with: ${C}./start.sh --stop${N}"
  exit 0
fi

say "  ${DIM}Following logs — Ctrl-C stops everything.${N}"
say ""

# One merged stream, each line tagged with its source.
#
# Backgrounded and waited on rather than run in the foreground: bash defers a
# trap until the current foreground command returns, so blocking directly on the
# pipeline meant a signal sent to this script alone — `kill`, or a supervisor
# sending SIGTERM — was never acted on. Ctrl-C from a terminal happened to work
# because it signals the whole foreground process group, tail included. `wait`
# is interruptible, so both paths now shut down the same way.
(
  tail -n 0 -F "$LOGS/backend.log" "$LOGS/server.log" "$LOGS/client.log" 2>/dev/null \
  | awk -v c="$C" -v y="$Y" -v g="$G" -v n="$N" '
      /^==> .*\.log <==$/ { split($2, p, "/"); f = p[length(p)]; sub(/\.log$/, "", f);
                            col = (f == "backend") ? c : (f == "server") ? y : g; next }
      { printf "%s%-8s%s │ %s\n", col, f, n, $0; fflush() }
    '
) &
FOLLOW_PID=$!
wait "$FOLLOW_PID" || true
