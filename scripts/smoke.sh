#!/usr/bin/env bash
# Runs the image the way the edge does and asserts the public contract end to end.
set -euo pipefail

IMAGE="${1:?usage: smoke.sh <image-ref>}"
SUFFIX="$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"
NAME="wm-smoke-${SUFFIX}"
VOLUME="wm-smoke-${SUFFIX}"
MCP="http://127.0.0.1:9100"
INGEST="http://127.0.0.1:9101"
KEY="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
ACCEPT='application/json, text/event-stream'

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  docker logs "$NAME" >&2 || true
  exit 1
}

wait_healthy() {
  local _
  for _ in $(seq 1 30); do
    if curl -fsS "$MCP/healthz" >/dev/null 2>&1 && curl -fsS "$INGEST/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "listeners did not become healthy within 30 s"
}

status() {
  curl -s -o /dev/null -w '%{http_code}' "$@"
}

mcp_call() {
  curl -sS -X POST "$MCP/mcp" \
    -H "Authorization: Bearer $KEY" \
    -H 'Content-Type: application/json' \
    -H "Accept: $ACCEPT" \
    -d "$1"
}

list_events() {
  mcp_call '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_events","arguments":{"event_name":"smoke.test"}}}'
}

docker volume create "$VOLUME" >/dev/null
docker run -d --name "$NAME" \
  --read-only --cap-drop ALL --security-opt no-new-privileges --memory 192m \
  -v "$VOLUME":/data \
  -p 127.0.0.1:9100:9100 -p 127.0.0.1:9101:9101 \
  -e MCP_AUTH_KEY="$KEY" \
  -e INGEST_BASE_URLS='{"default":"http://127.0.0.1:9101"}' \
  "$IMAGE" >/dev/null

wait_healthy

code="$(status -X POST "$MCP/mcp")"
[ "$code" = "401" ] || fail "POST /mcp without key: expected 401, got $code"

body="$(curl -sS -w '\n%{http_code}' -X POST "$INGEST/hooks" \
  -H 'Content-Type: application/json' \
  -d '{"event_name":"smoke.test","payload":{"ok":true}}')"
code="${body##*$'\n'}"
[ "$code" = "201" ] || fail "POST ingest /hooks: expected 201, got $code"
case "$body" in
  *'"duplicate":false'*) ;;
  *) fail "POST ingest /hooks: body lacks \"duplicate\":false" ;;
esac

code="$(status -X POST "$MCP/hooks" -H 'Content-Type: application/json' -d '{}')"
[ "$code" = "404" ] || fail "POST mcp /hooks: expected 404, got $code"
code="$(status -X POST "$INGEST/mcp" -H 'Content-Type: application/json' -d '{}')"
[ "$code" = "404" ] || fail "POST ingest /mcp: expected 404, got $code"

uid="$(docker exec "$NAME" id -u)"
[ "$uid" = "10001" ] || fail "container uid: expected 10001, got $uid"

init="$(mcp_call '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')"
case "$init" in
  *'"serverInfo"'*) ;;
  *) fail "MCP initialize returned no serverInfo" ;;
esac

stored_pattern='"ok": ?true'
listed="$(list_events)"
echo "$listed" | grep -Eq "$stored_pattern" || fail "list_events lacks the stored payload"

docker restart "$NAME" >/dev/null
wait_healthy
listed="$(list_events)"
echo "$listed" | grep -Eq "$stored_pattern" || fail "event lost across container restart"

start="$(date +%s)"
rc="$(docker stop --time 10 "$NAME" >/dev/null && docker inspect --format '{{.State.ExitCode}}' "$NAME")"
elapsed="$(($(date +%s) - start))"
[ "$rc" = "0" ] || fail "docker stop: expected exit code 0, got $rc"
[ "$elapsed" -lt 10 ] || fail "docker stop took ${elapsed}s (limit 10 s)"

echo "smoke: ok"
