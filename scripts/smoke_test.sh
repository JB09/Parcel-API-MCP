#!/usr/bin/env bash
#
# Smoke test for the parcel-api-mcp image.
#
# `docker build` cannot catch the two failure modes that matter here: a server
# bound to the wrong interface and a Host allowlist that rejects the proxy both
# produce an image that builds, a container that starts, and a `/healthz` that
# answers 200 — so Docker reports the container **healthy** while every tool call
# fails. This drives a real MCP handshake instead.
#
#   Phase 1  handshake + tools/list + a real tools/call over a NON-localhost Host
#            (catches the wrong bind interface and an over-tight Host guard)
#   Phase 2  the DNS-rebinding guard both ways: allowed Host 200, foreign Host 421
#            (otherwise only the permissive default is ever tested)
#   Phase 3  the TLS trust store is non-empty (httpx2 verifies against the system
#            store, so a base image that drops ca-certificates breaks HTTPS)
#
# Usage:  IMAGE=ghcr.io/jb09/parcel-api-mcp:latest scripts/smoke_test.sh
#
# No Parcel API key is needed: the handshake and `get_delivery_status_codes` are
# entirely local, so this never touches the Parcel API or its 20 req/hour budget.
#
# To confirm the test can actually fail, temporarily drop `host=`/`port=` or
# `transport_security=` from server.py and re-run — phase 1 or 2 must go red. An
# assertion you have never seen fail is not a test.

set -euo pipefail

IMAGE="${IMAGE:-parcel-mcp:smoke-test}"
# Host port to publish on. Only the test talks to it; the deploy publishes nothing.
PORT="${PORT:-18080}"
# The Host header a proxied request arrives with. Deliberately NOT localhost —
# that is the whole point of phase 1.
ROUTE_HOST="${ROUTE_HOST:-parcel-mcp:8080}"
# A Host that must always be rejected once the guard is on.
FOREIGN_HOST="${FOREIGN_HOST:-attacker.example.com}"
CONTAINER="${CONTAINER:-parcel-mcp-smoke}"
URL="http://127.0.0.1:${PORT}/mcp"
PROTOCOL_VERSION="2025-06-18"
EXPECTED_TOOLS="get_deliveries add_delivery get_supported_carriers get_delivery_status_codes"

WORK="$(mktemp -d)"
trap 'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; echo "--- container log ---" >&2; docker logs "$CONTAINER" 2>&1 | tail -40 >&2; exit 1; }
ok()   { echo "  ok: $*"; }

# Start the container fresh. Extra args become `docker run -e` flags.
start_container() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$CONTAINER" -p "127.0.0.1:${PORT}:8080" \
    -e PARCEL_API_KEY=smoke-test-not-a-real-key "$@" "$IMAGE" >/dev/null
  for _ in $(seq 1 40); do
    if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/healthz"; then return 0; fi
    docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
      || fail "container exited during startup"
    sleep 0.5
  done
  fail "container never answered /healthz on port ${PORT}"
}

# POST a JSON-RPC message to /mcp. $1 host header, $2 body, $3 optional session id.
# Writes response headers to $WORK/h and the body to $WORK/b; echoes the status.
mcp_post() {
  local host="$1" body="$2" session="${3:-}"
  local args=(-sS -o "$WORK/b" -D "$WORK/h" -w '%{http_code}' -X POST "$URL"
    -H "Host: ${host}" -H 'Content-Type: application/json'
    -H 'Accept: application/json, text/event-stream'
    -H "MCP-Protocol-Version: ${PROTOCOL_VERSION}")
  if [ -n "$session" ]; then args+=(-H "mcp-session-id: ${session}"); fi
  curl "${args[@]}" --data-binary "$body"
}

# The streamable-HTTP transport answers in SSE frames by default; the JSON-RPC
# message is the `data:` payload.
sse_data() { sed -n 's/^data: //p' "$WORK/b"; }

initialize() {
  local host="$1" code
  code="$(mcp_post "$host" '{"jsonrpc":"2.0","id":1,"method":"initialize","params":
    {"protocolVersion":"'"$PROTOCOL_VERSION"'","capabilities":{},
     "clientInfo":{"name":"smoke-test","version":"0"}}}')"
  echo "$code"
}

session_id() { grep -i '^mcp-session-id:' "$WORK/h" | tr -d '\r' | awk '{print $2}'; }

echo "==> Phase 1: MCP handshake over a non-localhost Host (guard off)"
start_container
# The bind interface is the cheapest possible check, and the failure it catches
# (127.0.0.1:8000 instead of 0.0.0.0:8080) is otherwise invisible from outside.
docker logs "$CONTAINER" 2>&1 | grep -q 'Uvicorn running on http://0.0.0.0:8080' \
  || fail "server did not bind 0.0.0.0:8080 — check host=/port= are passed to mcp.run()"
ok "bound 0.0.0.0:8080"

code="$(initialize "$ROUTE_HOST")"
[ "$code" = "200" ] || fail "initialize returned $code (want 200) for Host: ${ROUTE_HOST}"
SESSION="$(session_id)"
[ -n "$SESSION" ] || fail "initialize returned no mcp-session-id header"
ok "initialize 200, session established"

code="$(mcp_post "$ROUTE_HOST" '{"jsonrpc":"2.0","method":"notifications/initialized"}' "$SESSION")"
[ "$code" = "202" ] || fail "notifications/initialized returned $code (want 202)"

code="$(mcp_post "$ROUTE_HOST" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' "$SESSION")"
[ "$code" = "200" ] || fail "tools/list returned $code (want 200)"
tools="$(sse_data | jq -r '.result.tools[].name')"
for tool in $EXPECTED_TOOLS; do
  grep -qx "$tool" <<<"$tools" || fail "tool '$tool' missing from tools/list (got: $(tr '\n' ' ' <<<"$tools"))"
done
ok "tools/list advertises all $(wc -w <<<"$EXPECTED_TOOLS") tools"

# A real invocation, not just discovery. `get_delivery_status_codes` is a local
# reference tool, so this exercises the full call path without an API key.
code="$(mcp_post "$ROUTE_HOST" '{"jsonrpc":"2.0","id":3,"method":"tools/call",
  "params":{"name":"get_delivery_status_codes","arguments":{}}}' "$SESSION")"
[ "$code" = "200" ] || fail "tools/call returned $code (want 200)"
sse_data | jq -e '.result.content[0].text | fromjson | .["0"] == "Delivered"' >/dev/null \
  || fail "tools/call get_delivery_status_codes returned an unexpected payload"
ok "tools/call get_delivery_status_codes returned the status map"

echo "==> Phase 2: DNS-rebinding guard, both directions"
start_container -e "MCP_ALLOWED_HOSTS=${ROUTE_HOST}"
docker logs "$CONTAINER" 2>&1 | grep -q "DNS-rebinding guard enabled — allowed hosts: ${ROUTE_HOST}" \
  || fail "startup log does not name the allowlist — did MCP_ALLOWED_HOSTS reach the container?"
ok "guard enabled for ${ROUTE_HOST}"

code="$(initialize "$ROUTE_HOST")"
[ "$code" = "200" ] || fail "allowed Host ${ROUTE_HOST} got $code (want 200) — allowlist too tight"
ok "allowed Host -> 200"

code="$(initialize "$FOREIGN_HOST")"
[ "$code" = "421" ] || fail "foreign Host ${FOREIGN_HOST} got $code (want 421) — guard not enforcing"
ok "foreign Host -> 421"

# Explicitly pin the trap that makes this whole script necessary: the guard only
# covers /mcp, so a healthy healthz says nothing about whether tools work.
code="$(curl -sS -o /dev/null -w '%{http_code}' -H "Host: ${FOREIGN_HOST}" "http://127.0.0.1:${PORT}/healthz")"
[ "$code" = "200" ] || fail "/healthz returned $code (want 200) — the liveness probe must stay open"
ok "/healthz stays 200 even for a rejected Host (why healthz alone proves nothing)"

echo "==> Phase 3: TLS trust store"
# httpx2 verifies against the system trust store rather than a bundled certifi
# set, so an empty store means every outbound HTTPS call fails. Assert the store
# is populated instead of calling the internet, which would be flaky.
# (truststore.SSLContext.get_ca_certs() raises NotImplementedError — it resolves
# lazily — so use the stdlib default context, same OpenSSL paths on Linux.)
docker run --rm --entrypoint python "$IMAGE" -c '
import ssl
n = len(ssl.create_default_context().get_ca_certs())
assert n, f"empty trust store: {ssl.get_default_verify_paths()}"
print(f"  ok: {n} CA certificates in the system trust store")
' || fail "TLS trust store check failed — does the base image still ship ca-certificates?"

echo "PASS: smoke test green against ${IMAGE}"
