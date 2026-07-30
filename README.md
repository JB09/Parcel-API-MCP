# Parcel-API-MCP

A minimal, self-hosted [MCP](https://modelcontextprotocol.io) server for the
[Parcel](https://parcelapp.net) app's package-tracking API. It exposes tools to list
your deliveries, add a new one, look up carrier codes, and read the status-code map.
The tools are served over the streamable-HTTP MCP transport at `/mcp`, with an
unauthenticated `/healthz` liveness route. The implementation is intentionally small to
keep the audit/attack surface tight.

## Tools

| Tool | Kind | Description |
| --- | --- | --- |
| `get_deliveries` | read | List tracked deliveries, sorted by soonest expected arrival. |
| `add_delivery` | write | Add a package to Parcel tracking. Disabled when `READ_ONLY=true`. |
| `get_supported_carriers` | read | List carriers and their `carrier_code` values (public endpoint). |
| `get_delivery_status_codes` | read | Return the `status_code` → label map (local reference, no API call). |

## ⚠️ Security requirement: this server MUST be gated by an authorization service

**This server implements no authentication of its own, by design.** Anyone who can reach
`/mcp` can read your deliveries. **Do not expose it directly to the internet or bind it
to a public port.**

It **must** sit behind an identity-aware authorization proxy — such as
**[Pomerium](https://www.pomerium.com/docs/capabilities/mcp) in MCP mode**, or an
equivalent like [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/)
or [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) — that authenticates and
authorizes **every** request before it reaches `/mcp`.

Reference topology:

```
edge tunnel → reverse proxy (TLS) → Pomerium (SSO + allowlist to a single identity) → parcel-api-mcp
                                                                                        (internal network only)
```

The provided `docker-compose.yml` deliberately publishes **no host ports** and attaches
the container only to the proxy's internal Docker network, so the server is unreachable
except through the authorization proxy.

**Defense in depth already built in** (these complement, they do not replace, the proxy):

- The API is **read-mostly**: `get_deliveries` never mutates state, and the only writing
  tool, `add_delivery`, can be turned off entirely with `READ_ONLY=true` — so a misused
  tool cannot add shipments to your account. Parcel's API has no delete/edit surface.
- Setting `REQUIRE_POMERIUM_IDENTITY=true` makes the app **cryptographically verify**
  Pomerium's identity assertion on every `/mcp` request — signature (against Pomerium's
  JWKS), expiry, and audience. This blocks anything on the shared Docker network from
  reaching the app directly and bypassing Pomerium. See
  [Enabling app-layer verification](#enabling-app-layer-verification).

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and fill in
real values. `.env` is git-ignored and must stay that way — it holds the Parcel API key.
Nothing secret is baked into the image (the key is injected at runtime), which is why the
published container image can safely be public.

| Variable | Default | Description |
| --- | --- | --- |
| `PARCEL_API_KEY` | — | Personal Parcel API key. Generate it in the Parcel web app (see below). Required for the delivery tools; the carrier list is public. |
| `PARCEL_API_BASE` | `https://api.parcel.app/external` | Base URL; all endpoints are derived from it. Override only for a test double. |
| `DEFAULT_FILTER_MODE` | `active` | Filter when `get_deliveries`'s `filter_mode` is omitted: `active` (in-progress only) or `recent` (also recently delivered). Parcel's own default is `recent`. |
| `READ_ONLY` | `false` | When `true`, disable the `add_delivery` tool (read tools still work). |
| `PARCEL_TIMEOUT` | `15` | Per-request network timeout (seconds). Keep it below the fronting proxy's gateway timeout. |
| `STARTUP_TEST` | `false` | Call the Parcel API and count active deliveries on startup to verify the key. On failure, logs the reason; the server keeps running either way. |
| `REQUIRE_POMERIUM_IDENTITY` | `false` | Verify Pomerium's identity assertion on every `/mcp` request (see below). Requires `POMERIUM_JWKS_URL`. |
| `POMERIUM_JWKS_URL` | — | Pomerium's JWKS endpoint, e.g. `https://<host>/.well-known/pomerium/jwks.json`. Required when the gate is on. |
| `POMERIUM_AUDIENCE` | — | Expected `aud` claim (the route host/URL). Verified when set — strongly recommended. |
| `POMERIUM_ISSUER` | — | Expected `iss` claim. Verified only when set. |
| `POMERIUM_IDENTITY_HEADER` | `x-pomerium-assertion,x-pomerium-jwt-assertion` | Comma-separated header(s) carrying the assertion JWT. |
| `MCP_ALLOWED_HOSTS` | — (guard off) | Comma-separated `Host` allowlist for `/mcp` (DNS-rebinding guard). Empty disables the guard, with a warning at startup. See below — this is **not** your public hostname. |
| `MCP_ALLOWED_ORIGINS` | `https://<each allowed host>` | Comma-separated `Origin` allowlist. Requests with no `Origin` header always pass. |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | Server bind address/port. |

### Getting a Parcel API key

Open the Parcel web app at **[web.parcelapp.net](https://web.parcelapp.net)** → **Account
→ API** and generate a key. Parcel rate-limits the key to **20 requests/hour** and caches
results server-side, so a periodic caller (e.g. a once-a-day briefing) is well within
budget — do not add tight polling.

### Tools

```
get_deliveries(filter_mode?: str) -> str
```

Returns a JSON array of your tracked deliveries, sorted by soonest expected arrival. Each
entry has `description`, `carrier_code`, `status_code`, `status_label`, `tracking_number`,
`date_expected`, `date_expected_end`, `timestamp_expected`, and `latest_event`
(event/date/location of the most recent scan, or `null`). `filter_mode` is `active`
(in-progress only, the default) or `recent` (also includes recently delivered); it falls
back to `DEFAULT_FILTER_MODE` when omitted.

```
add_delivery(tracking_number: str, carrier_code: str, description: str,
             language?: str = "en", send_push_confirmation?: bool = false) -> str
```

Adds a package to Parcel tracking (`POST /add-delivery/`). Look up `carrier_code` with
`get_supported_carriers` first. `language` is an ISO 639-1 code for Parcel's tracking
updates; `send_push_confirmation` asks Parcel to push a confirmation. Refused when
`READ_ONLY=true`.

```
get_supported_carriers() -> str
```

Returns Parcel's supported-carrier list (codes + names) as JSON. Public endpoint — no API
key required.

```
get_delivery_status_codes() -> str
```

Returns the `status_code` → label map as JSON, without calling the API.

`status_code` maps to `status_label` as: `0` Delivered · `1` Frozen · `2` In transit ·
`3` Awaiting pickup · `4` Out for delivery · `5` Not found · `6` Failed attempt ·
`7` Exception · `8` Info received.

## DNS-rebinding guard (`MCP_ALLOWED_HOSTS`)

The MCP SDK checks the `Host` header on `/mcp` and answers `421 Misdirected Request`
when it is not on the allowlist ([CVE-2025-66416](https://advisories.gitlab.com/pypi/mcp/CVE-2025-66416/)).
This server leaves the guard **off** unless `MCP_ALLOWED_HOSTS` is set, so it is
something you opt into rather than something an upgrade can switch on under you. The
startup log always says which way it went:

```
DNS-rebinding guard enabled — allowed hosts: parcel-mcp:8080
INFO:     Uvicorn running on http://0.0.0.0:8080
```

### ⚠️ The allowlist is not your public hostname

Most reverse proxies — Pomerium included — **rewrite `Host` to the upstream address**
before forwarding. The public route may be `https://parcel-mcp.example.com`, but what
the container actually receives is `Host: parcel-mcp:8080`. Setting
`MCP_ALLOWED_HOSTS=parcel-mcp.example.com` therefore still `421`s every call.

**Don't guess — read it off the proxy.** In Pomerium's access log, the `authority`
field on the `http-request` line is the `Host` the upstream sees:

```json
{"upstream-cluster":"parcel-mcp-...","authority":"parcel-mcp:8080",
 "path":"/mcp","response-code":200}
```

Whether the Host is preserved or rewritten varies **per route**, so check this route
specifically. Then either allowlist what arrives (`MCP_ALLOWED_HOSTS=parcel-mcp:8080`)
or set `preserve_host_header: true` on the Pomerium route and allowlist the public
name. Matching is literal: a bare `parcel-mcp` will not match a `Host` carrying a
port — use `parcel-mcp:*` to allow any port.

This misconfiguration is invisible from the outside: the guard only covers `/mcp`, so
`/healthz` keeps returning 200 and Docker keeps reporting the container **healthy**
while every tool call fails. `docker compose logs parcel-mcp | grep "Invalid Host"` is
the tell — and `scripts/smoke_test.sh` (below) asserts both directions in CI.

## Smoke test

```sh
IMAGE=ghcr.io/jb09/parcel-api-mcp:latest scripts/smoke_test.sh
```

Starts the image and drives a real MCP `initialize` → `tools/list` → `tools/call`
over a non-localhost `Host`, then asserts the rebinding guard returns 200 for an
allowed `Host` and 421 for a foreign one, and that the image's TLS trust store is
populated. No Parcel API key is required — the handshake and
`get_delivery_status_codes` are entirely local, so it never spends the Parcel API's
20 requests/hour. CI runs it against the built image **before** pushing to GHCR.

## Enabling app-layer verification

This step is **optional** — Pomerium already gates all access. Enable it only if you also
want the app to reject any request that reaches it *without* a valid Pomerium identity
(e.g. a compromised neighbor on the shared Docker network hitting `parcel-mcp:8080`
directly). When on, the app verifies the assertion JWT's signature, expiry, and audience.

**1. Pomerium — set these on the `parcel-mcp` route.** The critical addition is
`pass_identity_headers: true`; without it Pomerium forwards no identity header and the app
rejects every request. Pomerium must also have a **signing key** configured (it serves the
matching public keys at `/.well-known/pomerium/jwks.json`).

```yaml
routes:
  - from: https://parcel-mcp.example.com
    to: http://parcel-mcp:8080         # pathless — the /mcp path passes through
    name: parcel-mcp
    mcp:
      server: {}
    pass_identity_headers: true        # <-- REQUIRED: sends X-Pomerium-Assertion to the app
    policy:
      - allow:
          and:
            - email:
                is: you@example.com
```

**2. App — set these in `.env`:**

```sh
REQUIRE_POMERIUM_IDENTITY=true
POMERIUM_JWKS_URL=https://parcel-mcp.example.com/.well-known/pomerium/jwks.json
POMERIUM_AUDIENCE=parcel-mcp.example.com
```

Then `docker compose up -d`. If `REQUIRE_POMERIUM_IDENTITY=true` but `POMERIUM_JWKS_URL` is
unset, the server refuses to start (a security gate must not run unable to verify). To turn
the feature off again, set `REQUIRE_POMERIUM_IDENTITY=false`.

## Run

```sh
cp .env.example .env      # then edit .env with real values
docker compose up -d
```

Health check:

```sh
docker compose exec parcel-mcp \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/healthz').read())"
# -> b'ok'
```

A green health check proves only that the process is up — it does not go through
`/mcp`, so it stays 200 even when every tool call is being rejected. Confirm the real
path with a tool call from your MCP client, or run `scripts/smoke_test.sh` against the
image.

Then add the `parcel-mcp` route to your authorization proxy (pathless upstream, e.g.
`to: http://parcel-mcp:8080`, so the `/mcp` path passes through) and connect your MCP
client to `https://<your-host>/mcp`.

## Maintenance

Patches flow with near-zero manual effort:

- **Dependabot** (`.github/dependabot.yml`) watches `requirements.txt`, the Dockerfile base
  image, and the workflow's actions, opening upgrade PRs weekly. Also enable Dependabot
  **security updates** in the repo's Settings → Code security.
- **CI** (`.github/workflows/build.yml`) builds the image, runs `scripts/smoke_test.sh`
  against it, and only then pushes it to GHCR — on push to `main`, on Dependabot PRs
  (build + smoke test only, no push), via manual dispatch, and **weekly (Mon 06:00 UTC)
  with `no-cache`** so the OS and Python patches are genuinely refreshed even without
  code changes. The pushed image is the retagged one the smoke test passed, so what
  ships is exactly what was tested.
- On the host, pull the rebuilt image with [Watchtower](https://containrrr.dev/watchtower/)
  (the compose file already sets the opt-in label) or a cron running
  `docker compose pull && docker compose up -d`.

## Links

- Parcel — [view-deliveries API](https://parcelapp.net/help/api-view-deliveries.html)
- Pomerium — [MCP support](https://www.pomerium.com/docs/capabilities/mcp)
- Pomerium — [Protect an MCP server](https://www.pomerium.com/docs/capabilities/mcp/protect-mcp-server)
- [Dependabot configuration options](https://docs.github.com/code-security/dependabot/dependabot-version-updates/configuration-options-for-the-dependabot.yml-file)
