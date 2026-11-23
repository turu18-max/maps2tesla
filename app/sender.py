"""Send waypoints to the car via Tesla Fleet API (signed commands).

Uses the tesla-fleet-api library (the same library Home Assistant's
Tesla Fleet integration uses). All secrets live in the data directory:
  config.json       {client_id, client_secret, domain, vin, region}
  tokens.json       {refresh_token, expires}
  private-key.pem   EC private key for command signing
"""
import asyncio
import json
import os

import aiohttp

DATA_DIR = os.environ.get("MAPS_TO_TESLA_DATA", "/data")


def _load(name, default=None):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(name, obj):
    path = os.path.join(DATA_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def load_config():
    cfg = _load("config.json")
    if not cfg:
        raise RuntimeError("config.json not found in data dir (setup not done)")
    return cfg


async def _api(session):
    from tesla_fleet_api.tesla.oauth import TeslaFleetOAuth

    cfg = load_config()
    tokens = _load("tokens.json", {})
    api = TeslaFleetOAuth(
        session,
        cfg.get("region", "na"),
        cfg["client_id"],
        cfg.get("client_secret"),
        cfg.get("redirect_uri"),
        refresh_token=tokens.get("refresh_token"),
        expires=tokens.get("expires", 0),
    )
    await api.get_private_key(os.path.join(DATA_DIR, "private-key.pem"))
    return api, cfg


async def _persist_tokens(api):
    _save("tokens.json", {
        "refresh_token": api.refresh_token,
        "expires": api.expires,
    })


_token_lock = asyncio.Lock()


async def _api_ready(session):
    """Build the API client and ensure a fresh access token, with
    the load -> refresh -> persist sequence serialized.

    Tesla ROTATES the refresh token on every refresh: the old one
    dies at once. Two rules follow. (1) Whoever refreshes must
    persist the new token — prewarm_wake used to refresh and then
    throw the new token away, leaving a dead token in tokens.json,
    so the next send died with login_required (2026-11-23).
    (2) Refreshes must not run concurrently: two clients refreshing
    with the same stored token can trip Tesla's reuse detection
    and kill the whole token family. Holding the lock across the
    reload makes the second caller see the fresh token and skip
    its own refresh."""
    async with _token_lock:
        api, cfg = await _api(session)
        await api.check_access_token()
        await _persist_tokens(api)
        return api, cfg


# Must match the scopes granted in the Tesla developer app registration.
# vehicle_device_data is required for wake_up (verified 2026-11-22:
# without it wake_up returns Forbidden "check required Scope").
SCOPES = ["openid", "offline_access", "vehicle_device_data",
          "vehicle_location", "vehicle_cmds"]


async def login_url():
    """Build the Tesla OAuth authorize URL (user opens it, logs in,
    and lands on the redirect page carrying ?code=...).
    prompt_missing_scopes=true makes Tesla re-prompt for scopes that
    were added after the user's original grant."""
    cfg = load_config()
    scope = "+".join(SCOPES)
    return (
        "https://auth.tesla.com/oauth2/v3/authorize?response_type=code"
        f"&client_id={cfg['client_id']}"
        f"&redirect_uri={cfg['redirect_uri']}"
        f"&scope={scope}&state=login"
        "&prompt_missing_scopes=true&require_requested_scopes=true"
    )


async def exchange_code(code: str):
    """Exchange an OAuth code for tokens and persist them.
    Done manually (not via the library) so Tesla's error body is
    visible when the exchange fails."""
    import time

    cfg = load_config()
    server = "https://fleet-api.prd.na.vn.cloud.tesla.com"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://auth.tesla.com/oauth2/v3/token",
            data={
                "grant_type": "authorization_code",
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "code": code,
                "audience": server,
                "redirect_uri": cfg["redirect_uri"],
            },
        ) as resp:
            data = await resp.json()
            if not resp.ok:
                return {"refresh_token_saved": False,
                        "status": resp.status, "error": data}
            _save("tokens.json", {
                "refresh_token": data["refresh_token"],
                "expires": int(time.time()) + data["expires_in"],
            })
            return {"refresh_token_saved": True}


async def public_key_pem():
    """Return the public key PEM (generate the key pair on first use).
    This is the file to host at
    https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem"""
    from cryptography.hazmat.primitives import serialization

    async with aiohttp.ClientSession() as session:
        api, _ = await _api(session)
        key = api.private_key
        return key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()


async def register_domain():
    """Register the domain with Tesla (partner_accounts) using a
    client-credentials partner token. Requires the public key to be
    live at https://<domain>/.well-known/appspecific/... first."""
    from tesla_fleet_api.const import Scope
    from tesla_fleet_api.tesla.fleet import TeslaFleetApi

    cfg = load_config()
    async with aiohttp.ClientSession() as session:
        api = TeslaFleetApi(session, region=cfg.get("region", "na"))
        # Partner-registration scopes cap what user tokens can be
        # granted (Tesla docs: portal, partner registration and consent
        # scopes must all agree). Keep in sync with SCOPES.
        await api.partner_login(
            cfg["client_id"], cfg["client_secret"],
            scopes=[Scope.OPENID, Scope.VEHICLE_DEVICE_DATA,
                    Scope.VEHICLE_CMDS, Scope.VEHICLE_LOCATION])
        return await api.partner.register(cfg["domain"])


def setup_status():
    """What the setup wizard needs to render its checklist. Never
    returns secret values — only whether they are set."""
    cfg = _load("config.json", {}) or {}
    tokens = _load("tokens.json", {}) or {}
    return {
        "config_exists": bool(cfg),
        "client_id_set": bool(cfg.get("client_id")),
        "client_secret_set": bool(cfg.get("client_secret")),
        "domain": cfg.get("domain") or "",
        "redirect_uri": cfg.get("redirect_uri") or "",
        "region": cfg.get("region") or "na",
        "vin_set": bool(cfg.get("vin")),
        "api_token_set": bool(cfg.get("api_token")),
        "private_key_exists": os.path.exists(
            os.path.join(DATA_DIR, "private-key.pem")),
        "tokens_exist": bool(tokens.get("refresh_token")),
    }


def save_config(fields):
    """Create or update config.json from wizard input. Empty
    strings keep the stored value, so the form can be re-submitted
    without re-typing the client secret. Derives redirect_uri from
    the domain and generates the api_token on first save; the token
    is returned so the page can store it in this browser and show
    it once for the iPhone Shortcut."""
    import secrets as _secrets

    cfg = _load("config.json", {}) or {}
    for key in ("client_id", "client_secret", "domain", "region"):
        value = (fields.get(key) or "").strip()
        if value:
            cfg[key] = value
    if cfg.get("domain"):
        cfg["redirect_uri"] = (
            f"https://{cfg['domain']}/oauth-callback.html")
    cfg.setdefault("region", "na")
    if not cfg.get("api_token"):
        cfg["api_token"] = _secrets.token_hex(16)
    _save("config.json", cfg)
    return {"api_token": cfg["api_token"], "status": setup_status()}


async def list_vehicles():
    """Vehicles visible to the current Tesla login, for the
    wizard's car picker (avoids hand-copying the VIN)."""
    async with aiohttp.ClientSession() as session:
        api, _ = await _api_ready(session)
        token = await api.access_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with api.session.get(f"{api.server}/api/1/vehicles",
                                   headers=headers) as resp:
            data = await resp.json()
            if not resp.ok:
                raise RuntimeError(f"vehicle list failed: {data}")
            return [{"vin": v.get("vin"),
                     "display_name": v.get("display_name") or ""}
                    for v in data.get("response", [])]


def set_vin(vin):
    cfg = _load("config.json", {}) or {}
    cfg["vin"] = vin
    _save("config.json", cfg)
    return setup_status()


async def token_scopes():
    """Diagnostic: refresh the access token and decode its scope claim
    (Tesla access tokens are JWTs carrying an `scp` list)."""
    import base64

    async with aiohttp.ClientSession() as session:
        api, _ = await _api(session)
        await api.refresh_access_token()
        await _persist_tokens(api)
        token = await api.access_token()
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return {"scp": data.get("scp"), "aud": data.get("aud")}


_wake_lock = asyncio.Lock()
_wake_online_at = 0.0
_WAKE_FRESH_S = 60.0

# Self-imposed wake_up budget, deliberately under Tesla's limit:
# Tesla allows 3 wakes/min per vehicle per account — shared with
# every other app on the account (e.g. Home Assistant) — so this
# app caps itself at 2/min and waits for a slot instead of firing
# into a 429. Every wake_up POST (including 429'd ones, which are
# still billable) is timestamped; a 429 also sets a cooldown that
# all later wakes honor.
from collections import deque

_wake_post_times = deque()
_wake_cooldown_until = 0.0
_WAKE_BUDGET = 2
_WAKE_WINDOW_S = 60.0


async def _acquire_wake_slot(deadline):
    """Wait until one more wake_up POST fits the budget, then
    record it. Raises if the wait would exceed `deadline`."""
    import time

    while True:
        now = time.monotonic()
        while _wake_post_times and \
                now - _wake_post_times[0] >= _WAKE_WINDOW_S:
            _wake_post_times.popleft()
        wait = 0.0
        if len(_wake_post_times) >= _WAKE_BUDGET:
            wait = max(wait,
                       _wake_post_times[0] + _WAKE_WINDOW_S - now)
        if _wake_cooldown_until > now:
            wait = max(wait, _wake_cooldown_until - now)
        if wait <= 0:
            _wake_post_times.append(now)
            return
        if now + wait > deadline:
            raise RuntimeError(
                "wake_up budget exhausted (self-imposed "
                f"{_WAKE_BUDGET}/min, Tesla limit 3/min); "
                "retry in about a minute")
        print(f"[wake] waiting {wait:.0f}s for rate budget",
              flush=True)
        await asyncio.sleep(min(wait, 15))


async def _wake(api, vin):
    """Wake the car with a manual POST. The library's wake_up() sends
    no Content-Type header, which Tesla rejects with 406 NotAcceptable
    (verified 2026-11-22), so set application/json explicitly.

    Tesla rate-limits wake_up hard (HTTP 429, plain-text body), and
    this app used to wake from several places at once — the /api/send
    prewarm task, send_waypoints itself, the Shortcut's retry a few
    seconds later — tripping the limit with its own traffic
    (2026-11-23). So wakes are serialized behind one lock, a car
    confirmed online within _WAKE_FRESH_S is not woken again, a 429
    backs off 15 s instead of hammering, and a non-JSON body is
    tolerated instead of throwing a decode error."""
    import time

    global _wake_online_at, _wake_cooldown_until
    async with _wake_lock:
        if time.monotonic() - _wake_online_at < _WAKE_FRESH_S:
            return {"response": {"state": "online"}, "cached": True}
        token = await api.access_token()
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        wake_url = f"{api.server}/api/1/vehicles/{vin}/wake_up"
        state_url = f"{api.server}/api/1/vehicles/{vin}"
        deadline = time.monotonic() + 120
        last_wake = 0.0
        data = {}
        while time.monotonic() < deadline:
            # wake_up is limited to 3/min and is the priciest call
            # ($0.02); state checks are data calls (60/min, $0.002).
            # So POST wake_up at most every 45 s and poll the
            # vehicle state in between, per Tesla's best practice —
            # the old loop re-POSTed wake_up every 5 s and could
            # trip the limit with a single slow wake.
            if time.monotonic() - last_wake >= 45:
                await _acquire_wake_slot(deadline)
                last_wake = time.monotonic()
                async with api.session.post(wake_url, headers=headers,
                                            data="{}") as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {}
                    if data.get("response", {}).get("state") == "online":
                        _wake_online_at = time.monotonic()
                        return data
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            cool = float(retry_after)
                        except (TypeError, ValueError):
                            cool = 60.0
                        _wake_cooldown_until = time.monotonic() + min(
                            cool, 120.0)
                        print(f"[wake] 429 from Tesla; cooldown "
                              f"{min(cool, 120.0):.0f}s", flush=True)
            await asyncio.sleep(5)
            async with api.session.get(state_url,
                                       headers=headers) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except Exception:
                    data = {}
                if data.get("response", {}).get("state") == "online":
                    _wake_online_at = time.monotonic()
                    return data
        raise RuntimeError(f"vehicle did not come online: {data}")


async def prewarm_wake():
    """Best-effort early wake, meant to run while route snapping is
    still in progress so the send step does not pay the wake latency
    serially. Errors are ignored; send_waypoints wakes again anyway."""
    try:
        async with aiohttp.ClientSession() as session:
            api, cfg = await _api_ready(session)
            await _wake(api, cfg["vin"])
    except Exception:
        pass


async def send_gps(lat, lon, order=0):
    """Send one navigation_gps_request (coordinate pin).
    order follows the library's REST mapping: 0 = replace the trip,
    1 = prepend a stop, 2 = append a stop. (The protobuf enum is
    1-based; which mapping the Fleet endpoint honors is being verified
    on the real car — see docs/ROUTE-GEOMETRY-POC.md.)"""
    async with aiohttp.ClientSession() as session:
        api, cfg = await _api_ready(session)
        vehicle = api.vehicles.createSigned(cfg["vin"])
        await _wake(api, cfg["vin"])
        return await vehicle.navigation_gps_request(lat, lon, order=order)


async def send_gps_destination(lat, lon, name, order=0):
    """Send navigation_gps_destination_request: coordinates plus a
    destination name string. The signed Commands docstring maps order
    as 0 = replace the trip, 1 = prepend, 2 = append (note: other docs
    in the same library describe a 1-based mapping — being verified
    on the real car)."""
    async with aiohttp.ClientSession() as session:
        api, cfg = await _api_ready(session)
        vehicle = api.vehicles.createSigned(cfg["vin"])
        await _wake(api, cfg["vin"])
        return await vehicle.navigation_gps_destination_request(
            lat, lon, name, order)


async def send_waypoints_raw(waypoints: str):
    """Send navigation_waypoints_request with a caller-built string
    (probe: can entries be coordinate text instead of refId: Place IDs?)."""
    async with aiohttp.ClientSession() as session:
        api, cfg = await _api_ready(session)
        vehicle = api.vehicles.createSigned(cfg["vin"])
        await _wake(api, cfg["vin"])
        return await vehicle.navigation_waypoints_request(waypoints)


async def send_waypoints(place_ids):
    """Wake the vehicle and send navigation_waypoints_request.
    place_ids: ordered Google Place IDs; the last one is the destination."""
    if not place_ids:
        raise ValueError("no place_ids")
    waypoints = ",".join(f"refId:{pid}" for pid in place_ids)
    async with aiohttp.ClientSession() as session:
        api, cfg = await _api_ready(session)
        vehicle = api.vehicles.createSigned(cfg["vin"])
        await _wake(api, cfg["vin"])
        try:
            return await vehicle.navigation_waypoints_request(waypoints)
        except Exception:
            # Car may have slipped back to sleep between wake and the
            # signed command; wake once more and retry a single time.
            await _wake(api, cfg["vin"])
            return await vehicle.navigation_waypoints_request(waypoints)
