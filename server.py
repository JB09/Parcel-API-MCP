"""MCP server exposing Parcel package-tracking tools (parcel.app).

Tools: `get_deliveries` (read), `add_delivery` (write), `get_supported_carriers`
(read), and `get_delivery_status_codes` (local reference).

The server implements NO authentication of its own by design. It is meant to run
on an internal network, fronted by an identity-aware authorization proxy (e.g.
Pomerium in MCP mode) that authenticates and authorizes every request before it
reaches `/mcp`. See README.md.

Configuration is entirely via environment variables (see .env.example). Generate
a Parcel API key from the Parcel web app (https://web.parcelapp.net) and inject it
at runtime as PARCEL_API_KEY.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse

logger = logging.getLogger("parcel-mcp")

# --- Configuration (all from env; secrets injected at runtime, never baked in) ---
# Base URL for Parcel's external API. The specific endpoints below are derived
# from it, so a single override repoints them all (e.g. for a test double).
PARCEL_API_BASE = os.environ.get("PARCEL_API_BASE", "https://api.parcel.app/external").rstrip("/")
# "View deliveries" (authenticated GET) — the api-key header carries the key.
DELIVERIES_URL = f"{PARCEL_API_BASE}/deliveries/"
# "Add delivery" (authenticated POST).
ADD_DELIVERY_URL = f"{PARCEL_API_BASE}/add-delivery/"
# Supported-carrier list (public GET — no api-key needed).
CARRIERS_URL = f"{PARCEL_API_BASE}/supported_carriers.json"
# Personal API key generated in the Parcel web app (web.parcelapp.net). Required
# for the delivery read/write tools; the carrier list is public.
PARCEL_API_KEY = os.environ.get("PARCEL_API_KEY", "")
# Filter used when `get_deliveries` omits `filter_mode`: "active" (in-progress
# only, the sensible default for a briefing) or "recent" (also includes recently
# delivered). An unrecognized value falls back to "active".
DEFAULT_FILTER_MODE = os.environ.get("DEFAULT_FILTER_MODE", "active").strip().lower()
# When true, the writing tool (`add_delivery`) is refused — a read-only mode.
READ_ONLY = os.environ.get("READ_ONLY", "false").lower() == "true"
# Per-request network timeout (seconds). Parcel caches server-side and rate-limits
# to 20 requests/hour, so keep callers from hammering it. Keep this below the
# fronting proxy's gateway timeout so a slow upstream returns a clean tool error
# instead of the proxy surfacing a 502.
PARCEL_TIMEOUT = int(os.environ.get("PARCEL_TIMEOUT", "15"))

# Optional app-layer backstop. The external proxy is still REQUIRED regardless.
# When enabled, /mcp requests must carry a Pomerium identity assertion whose JWT
# is cryptographically verified (signature + exp + audience) against Pomerium's
# JWKS — this blocks anything on the shared network that tries to reach the app
# directly, bypassing Pomerium.
REQUIRE_POMERIUM_IDENTITY = os.environ.get("REQUIRE_POMERIUM_IDENTITY", "false").lower() == "true"
# Candidate header(s) carrying the assertion JWT. Pomerium's MCP mode uses
# `x-pomerium-assertion`; the general identity header is `x-pomerium-jwt-assertion`.
POMERIUM_IDENTITY_HEADER = os.environ.get(
    "POMERIUM_IDENTITY_HEADER", "x-pomerium-assertion,x-pomerium-jwt-assertion"
)
POMERIUM_ASSERTION_HEADERS = [
    h.strip().lower() for h in POMERIUM_IDENTITY_HEADER.split(",") if h.strip()
]
# Pomerium's JWKS endpoint (its signing key's public keys), e.g.
# https://<route-host>/.well-known/pomerium/jwks.json. Required when the gate is on.
POMERIUM_JWKS_URL = os.environ.get("POMERIUM_JWKS_URL", "")
# Expected `aud`/`iss` claims. `aud` is the route's upstream URL/host; verified
# when set. `iss` verified only when set.
POMERIUM_AUDIENCE = os.environ.get("POMERIUM_AUDIENCE", "")
POMERIUM_ISSUER = os.environ.get("POMERIUM_ISSUER", "")

# Call the Parcel API on startup to verify the configuration. On failure the error
# is logged and the server keeps running.
STARTUP_TEST = os.environ.get("STARTUP_TEST", "false").lower() == "true"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

# Parcel's integer `status_code` -> human-readable label. See the Parcel API docs:
# https://parcelapp.net/help/api-view-deliveries.html
STATUS_LABELS = {
    0: "Delivered",
    1: "Frozen",
    2: "In transit",
    3: "Awaiting pickup",
    4: "Out for delivery",
    5: "Not found",
    6: "Failed attempt",
    7: "Exception",
    8: "Info received",
}

mcp = FastMCP("parcel-mcp", host=HOST, port=PORT)


def _require_key() -> None:
    """Guard the authenticated tools when no API key is configured (pre-network)."""
    if not PARCEL_API_KEY:
        raise RuntimeError("PARCEL_API_KEY must be configured to reach the Parcel API.")


def _require_writable() -> None:
    """Guard the writing tool when the server is configured read-only."""
    if READ_ONLY:
        raise RuntimeError("Server is in READ_ONLY mode; add_delivery is disabled.")


def _raise_for_api_error(data) -> None:
    """Surface an explicit Parcel `success: false` payload as a clean tool error."""
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"Parcel API error: {data.get('error_message', 'unknown')}")


def _normalize_filter_mode(filter_mode: str | None) -> str:
    """Resolve the effective filter, falling back to DEFAULT_FILTER_MODE then 'active'.

    Parcel only understands "active" (in-progress) and "recent" (also includes
    recently delivered); anything else is coerced so a bad argument can't reach
    the API.
    """
    value = (filter_mode or DEFAULT_FILTER_MODE or "active").strip().lower()
    return value if value in ("active", "recent") else "active"


def _fetch_deliveries(filter_mode: str) -> list:
    """Call the Parcel API and return the raw `deliveries` list.

    Raises RuntimeError when the key is missing (before any network I/O) or when
    Parcel reports `success: false`; httpx raises on HTTP/transport errors.
    """
    _require_key()
    with httpx.Client(timeout=PARCEL_TIMEOUT) as client:
        resp = client.get(
            DELIVERIES_URL,
            params={"filter_mode": filter_mode},
            headers={"api-key": PARCEL_API_KEY},
        )
        resp.raise_for_status()
        data = resp.json()
    _raise_for_api_error(data)
    return data.get("deliveries") or []


def _post_delivery(payload: dict) -> dict:
    """POST a new delivery to Parcel and return the parsed response.

    Raises RuntimeError when the key is missing or Parcel reports `success: false`;
    httpx raises on HTTP/transport errors.
    """
    _require_key()
    with httpx.Client(timeout=PARCEL_TIMEOUT) as client:
        resp = client.post(
            ADD_DELIVERY_URL,
            json=payload,
            headers={"api-key": PARCEL_API_KEY, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    _raise_for_api_error(data)
    return data if isinstance(data, dict) else {}


def _fetch_carriers() -> object:
    """Fetch Parcel's public supported-carriers list (no API key required)."""
    with httpx.Client(timeout=PARCEL_TIMEOUT) as client:
        resp = client.get(CARRIERS_URL)
        resp.raise_for_status()
        return resp.json()


def _latest_event(delivery: dict) -> dict | None:
    """Summarize a delivery's most recent tracking event, if any.

    Parcel returns `events` newest-first, so `events[0]` is the latest scan. Only
    the fields worth surfacing in a briefing are kept.
    """
    events = delivery.get("events") or []
    if not events:
        return None
    latest = events[0]
    return {
        "event": latest.get("event"),
        "date": latest.get("date"),
        "location": latest.get("location"),
    }


def _summarize_delivery(delivery: dict) -> dict:
    """Flatten one Parcel delivery into the fields a briefing needs."""
    status_code = delivery.get("status_code")
    return {
        "description": delivery.get("description"),
        "carrier_code": delivery.get("carrier_code"),
        "status_code": status_code,
        "status_label": STATUS_LABELS.get(status_code, "Unknown"),
        "tracking_number": delivery.get("tracking_number"),
        "date_expected": delivery.get("date_expected"),
        "date_expected_end": delivery.get("date_expected_end"),
        "timestamp_expected": delivery.get("timestamp_expected"),
        "latest_event": _latest_event(delivery),
    }


# Tool annotations. Clients (e.g. Claude's connector settings) use these hints to
# group tools as read vs write and to decide what warrants confirmation, so every
# tool declares them. Named READ (not READ_ONLY) on purpose: a `READ_ONLY`
# constant would shadow the READ_ONLY env-var boolean above, which
# _require_writable() reads at call time — silently disabling add_delivery.
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
# get_delivery_status_codes is a purely local reference — it makes no external
# call — so it is read-only with openWorldHint false.
READ_LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Adding a delivery creates a new tracked item without altering existing ones, and
# each call makes another — not destructive, not idempotent.
CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)


# --- Read tools ---------------------------------------------------------------


@mcp.tool(annotations=READ)
def get_deliveries(filter_mode: str | None = None) -> str:
    """List tracked package deliveries from Parcel (parcel.app).

    Args:
        filter_mode: Which shipments to return — "active" (in-progress only, the
            default) or "recent" (also includes recently delivered). Falls back to
            DEFAULT_FILTER_MODE, then "active", when omitted; an unrecognized value
            is treated as "active".

    Returns:
        A JSON array of deliveries sorted by soonest expected arrival. Each entry
        has description, carrier_code, status_code, status_label, tracking_number,
        date_expected, date_expected_end, timestamp_expected, and latest_event
        (event/date/location of the most recent scan, or null).
    """
    deliveries = _fetch_deliveries(_normalize_filter_mode(filter_mode))
    summaries = [_summarize_delivery(d) for d in deliveries]
    # Soonest expected first; deliveries with no ETA sort to the end.
    summaries.sort(key=lambda s: s.get("timestamp_expected") or 9_999_999_999)
    return json.dumps(summaries)


@mcp.tool(annotations=READ)
def get_supported_carriers() -> str:
    """List the carriers Parcel can track, with their `carrier_code` values.

    Use this to look up the correct `carrier_code` before calling `add_delivery`.
    This endpoint is public (no API key required).

    Returns:
        The Parcel supported-carriers document as a JSON string (carrier codes and
        their display names / details).
    """
    return json.dumps(_fetch_carriers())


@mcp.tool(annotations=READ_LOCAL)
def get_delivery_status_codes() -> str:
    """Return the mapping from Parcel `status_code` integers to human labels.

    A local reference (no API call): 0 Delivered, 1 Frozen, 2 In transit,
    3 Awaiting pickup, 4 Out for delivery, 5 Not found, 6 Failed attempt,
    7 Exception, 8 Info received.

    Returns:
        A JSON object mapping each status code (as a string) to its label.
    """
    return json.dumps({str(code): label for code, label in STATUS_LABELS.items()})


# --- Write tools --------------------------------------------------------------


@mcp.tool(annotations=CREATE)
def add_delivery(
    tracking_number: str,
    carrier_code: str,
    description: str,
    language: str = "en",
    send_push_confirmation: bool = False,
) -> str:
    """Add a package to Parcel tracking.

    Args:
        tracking_number: The carrier's tracking number for the shipment.
        carrier_code: Parcel's code for the carrier (see `get_supported_carriers`).
        description: A short human label for the shipment (e.g. "Running shoes").
        language: ISO 639-1 language code for Parcel's tracking updates. Defaults
            to "en".
        send_push_confirmation: When true, Parcel sends a push notification
            confirming the delivery was added. Defaults to false.

    Returns:
        A short confirmation string naming the added shipment.
    """
    _require_writable()
    payload = {
        "tracking_number": tracking_number,
        "carrier_code": carrier_code,
        "description": description,
        "send_push_confirmation": send_push_confirmation,
    }
    if language:
        payload["language"] = language
    _post_delivery(payload)
    return f"Delivery added: {description!r} ({carrier_code} {tracking_number})."


def _run_startup_test() -> None:
    """Call the Parcel API at startup to verify config. Never raises.

    Fetches active deliveries (a cheap authenticated round-trip). On failure the
    reason is logged (auth / connection / API error) and the server starts anyway
    so a transient Parcel outage does not block boot.
    """
    logger.info("STARTUP_TEST enabled — calling the Parcel API to verify config...")
    try:
        deliveries = _fetch_deliveries("active")
    except Exception as exc:
        logger.error(
            "Startup Parcel check FAILED against %s — %s: %s. "
            "The server will keep running; fix PARCEL_API_KEY and restart to retest.",
            DELIVERIES_URL,
            type(exc).__name__,
            exc,
        )
        return
    logger.info("Startup Parcel check OK — %d active delivery(ies).", len(deliveries))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> PlainTextResponse:
    """Unauthenticated liveness probe used by Docker/compose healthchecks."""
    return PlainTextResponse("ok")


_jwks_client = None  # lazily constructed jwt.PyJWKClient (caches signing keys)


def _get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        import jwt  # PyJWT

        _jwks_client = jwt.PyJWKClient(POMERIUM_JWKS_URL)
    return _jwks_client


def _extract_assertion(headers) -> str | None:
    """Return the first present Pomerium assertion header value, else None."""
    for name in POMERIUM_ASSERTION_HEADERS:
        value = headers.get(name)
        if value:
            return value
    return None


def _verify_assertion(token: str) -> None:
    """Verify Pomerium's assertion JWT: signature (ES256) + exp + optional aud/iss.

    Raises on any failure (bad/expired/forged token). Runs sync network I/O to the
    JWKS endpoint on first use, then serves cached keys.
    """
    import jwt  # PyJWT

    signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256"],
        audience=POMERIUM_AUDIENCE or None,
        issuer=POMERIUM_ISSUER or None,
        options={
            "require": ["exp"],
            "verify_aud": bool(POMERIUM_AUDIENCE),
            "verify_iss": bool(POMERIUM_ISSUER),
        },
    )


def _run_with_identity_gate() -> None:
    """Serve the MCP app, cryptographically verifying Pomerium's identity on /mcp.

    Defense-in-depth: the external proxy remains the primary gate. Every /mcp
    request must carry a Pomerium assertion whose JWT verifies against Pomerium's
    JWKS; otherwise it is rejected with 401. `/healthz` stays open for healthchecks.
    """
    import uvicorn
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.base import BaseHTTPMiddleware

    app = mcp.streamable_http_app()

    async def require_identity(request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            token = _extract_assertion(request.headers)
            if not token:
                logger.warning("Rejected /mcp request: missing Pomerium assertion header.")
                return PlainTextResponse(
                    "Missing authorization proxy identity header.", status_code=401
                )
            try:
                await run_in_threadpool(_verify_assertion, token)
            except Exception as exc:
                # Log the reason (expired / bad signature / wrong audience), never the token.
                logger.warning(
                    "Rejected /mcp request: invalid Pomerium assertion — %s: %s",
                    type(exc).__name__,
                    exc,
                )
                return PlainTextResponse(
                    "Invalid authorization proxy identity.", status_code=401
                )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=require_identity)
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if STARTUP_TEST:
        _run_startup_test()

    if REQUIRE_POMERIUM_IDENTITY:
        if not POMERIUM_JWKS_URL:
            logger.error(
                "REQUIRE_POMERIUM_IDENTITY=true but POMERIUM_JWKS_URL is not set. "
                "The gate cannot verify assertions; refusing to start. Set POMERIUM_JWKS_URL "
                "(e.g. https://<route-host>/.well-known/pomerium/jwks.json) and "
                "POMERIUM_AUDIENCE, or set REQUIRE_POMERIUM_IDENTITY=false."
            )
            raise SystemExit(1)
        _run_with_identity_gate()
    else:
        mcp.run(transport="streamable-http")
