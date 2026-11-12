#!/usr/bin/env bash
# ai-factory's app check: start the project's app the way a person would and look at it.
#
# Copied into the step's control directory and run from the workspace, after
# tests/services.sh has been sourced (runner.with_services). Contract for the project:
# `scripts/start.sh` starts the app in the foreground and serves it on
# http://127.0.0.1:$APP_PORT.
#
# Writes into $AI_FACTORY_RESULTS: junit.xml (one case per page), app.json (the
# problems found) and report/app/index.html (screenshots). Exits non-zero when the app
# doesn't start or any page has a problem.
set -u

RESULTS="${AI_FACTORY_RESULTS:-$PWD/results}"
mkdir -p "$RESULTS/report/app"
export APP_PORT="${APP_PORT:-8799}"
START_TIMEOUT="${APP_START_TIMEOUT:-90}"
URL="http://127.0.0.1:$APP_PORT"
LOG="$RESULTS/report/app/start.log"
HERE="$(cd "$(dirname "$0")" && pwd)"

fail_start() {
  # $1: why. The check runs no pages; record one failing case so the result is visible.
  local why="$1" tail
  tail="$(tail -n 40 "$LOG" 2>/dev/null | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')"
  why_json="$(printf '%s' "$why" | jq -Rs .)"
  printf '{"started": false, "browser": false, "problems": [%s], "pages": []}\n' \
    "$why_json" > "$RESULTS/app.json"
  cat > "$RESULTS/junit.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="app" tests="1" failures="1">
  <testcase classname="app" name="the app starts" file="scripts/start.sh">
    <failure message="$(printf '%s' "$why" | sed 's/"/\&quot;/g; s/</\&lt;/g')">$tail</failure>
  </testcase>
</testsuite>
EOF
  echo "app check: $why"
  [ -s "$LOG" ] && { echo "--- last lines of scripts/start.sh output ---"; tail -n 40 "$LOG"; }
  exit 1
}

[ -f scripts/start.sh ] || fail_start "scripts/start.sh is missing"

echo "app check: starting scripts/start.sh (APP_PORT=$APP_PORT)"
# Job control puts the app in its own process group (setsid isn't on macOS, where the
# local runtime runs), so everything it started can be stopped together afterwards.
set -m
sh scripts/start.sh > "$LOG" 2>&1 < /dev/null &
app_pid=$!
set +m
cleanup() { kill -TERM -- "-$app_pid" 2>/dev/null; sleep 1; kill -KILL -- "-$app_pid" 2>/dev/null; }
trap cleanup EXIT

up=""
for _ in $(seq 1 "$START_TIMEOUT"); do
  if curl -s -o /dev/null --max-time 2 "$URL/"; then up=1; break; fi
  kill -0 "$app_pid" 2>/dev/null || fail_start "scripts/start.sh exited before the app answered on $URL"
  sleep 1
done
[ -n "$up" ] || fail_start "nothing answered on $URL within ${START_TIMEOUT}s of running scripts/start.sh"
echo "app check: the app answers on $URL"

# The pages to look at: / and any listed in tests/app-pages.txt (one path per line).
pages="/"
if [ -f tests/app-pages.txt ]; then
  pages="$pages $(grep -v '^[[:space:]]*\(#\|$\)' tests/app-pages.txt | tr '\n' ' ')"
fi

# Node: the project's, else pi's private copy. Playwright: wherever npm installed it.
NODE="$(command -v node || true)"
[ -n "$NODE" ] || NODE=/opt/pi/node/bin/node
paths="$PWD/node_modules"
for d in */node_modules; do [ -d "$d" ] && paths="$paths:$PWD/$d"; done
if command -v npm >/dev/null 2>&1; then paths="$paths:$(npm root -g 2>/dev/null)"; fi
export NODE_PATH="$paths"

# Not exec: the EXIT trap must still stop the app afterwards.
# shellcheck disable=SC2086  # one argument per page
APP_URL="$URL" "$NODE" "$HERE/appcheck.cjs" $pages
exit $?
