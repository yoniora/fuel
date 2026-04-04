#api_server.py
import os
import math
import base64
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import re
import asyncio
import logging
import httpx
import uvicorn
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fuel_app import run_optimiser


load_dotenv()


async def _prewarm_caches():
    """Fire-and-forget: warm NSW reference + prices caches, and VIC if configured."""
    try:
        await _refresh_reference_if_needed(force=False)
        await _refresh_prices_if_needed(force=False)
        logging.getLogger("fuel").info("NSW caches pre-warmed OK")
    except Exception as e:
        logging.getLogger("fuel").warning("NSW cache pre-warm failed (non-fatal): %s", e)
    if _VIC_CONSUMER_ID:
        try:
            await _refresh_vic_prices_if_needed(force=False)
            logging.getLogger("fuel").info("VIC cache pre-warmed OK")
        except Exception as e:
            logging.getLogger("fuel").warning("VIC cache pre-warm failed (non-fatal): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_prewarm_caches())
    yield


# =========================================================
# FastAPI
# =========================================================
app = FastAPI(lifespan=lifespan)

# Allow requests from the web frontend (any localhost port, file://, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the web frontend
# index.html at /, everything else (js, css, icons/) via /assets/...
_WEB_DIR = os.path.join(os.path.dirname(__file__), "web")

@app.get("/config")
def client_config():
    """Serves non-secret config to the frontend (Maps JS key must be in the browser)."""
    return {"googleMapsApiKey": _google_api_key()}


@app.get("/")
def serve_index(request: Request):
    host = request.headers.get("host", "")
    # yoni.ink (root domain) → landing page; fuel.yoni.ink (or localhost) → app
    if host.startswith("yoni.ink"):
        return FileResponse(
            os.path.join(_WEB_DIR, "landing.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return FileResponse(
        os.path.join(_WEB_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

app.mount("/assets", StaticFiles(directory=_WEB_DIR), name="static")

@app.get("/debug/runtime")
def debug_runtime():
    return {
        "cwd": os.getcwd(),
        "api_server_file": __file__,
        "cached_count": len(_PRICES_CACHE) if "_PRICES_CACHE" in globals() else "NO _PRICES_CACHE",
        "cached_at": _PRICES_CACHE_AT.isoformat() if "_PRICES_CACHE_AT" in globals() and _PRICES_CACHE_AT else None,
    }

# =========================================================
# ENV VARS (put these in your .env)
# =========================================================
# Google Routes (you already have this)
GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
def _google_api_key() -> str:
    key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=500, detail="Missing GOOGLE_API_KEY env var on server.")
    return key

# NSW FuelCheck / FuelPriceCheck
# IMPORTANT: set these in .env
NSW_API_KEY = os.getenv("NSW_API_KEY") #or os.getenv("FUEL_API_KEY")
NSW_API_SECRET = os.getenv("NSW_API_SECRET") #or os.getenv("FUEL_API_SECRET")

NSW_BASE = "https://api.onegov.nsw.gov.au"
NSW_TOKEN_URL = f"{NSW_BASE}/oauth/client_credential/accesstoken"
NSW_PRICES_ALL_URL = f"{NSW_BASE}/FuelPriceCheck/v2/fuel/prices"
NSW_LOVS_URL       = f"{NSW_BASE}/FuelCheckRefData/v2/fuel/lovs"

# --------- NSW reference (station locations/brands) cache ----------
_STATIONS_REF: dict[int, dict] = {}   # stationcode -> {name, brand, lat, lng, ...}
_STATIONS_REF_AT: Optional[datetime] = None
_STATIONS_REF_TTL = timedelta(days=7)  # LOVs change rarely; weekly is fine

# --------- VIC Fair Fuel Open Data ----------
VIC_BASE_URL = "https://api.fuel.service.vic.gov.au/open-data/v1"
VIC_PRICES_URL = f"{VIC_BASE_URL}/fuel/prices"
_VIC_CONSUMER_ID: str = os.getenv("VIC_CONSUMER_ID", "").strip()

_VIC_PRICES_CACHE: List[Dict[str, Any]] = []   # raw station dicts from VIC API
_VIC_PRICES_CACHE_AT: Optional[datetime] = None


# =========================================================
# Models
# =========================================================
class OptimiseRequest(BaseModel):
    origin: str = Field(min_length=3)
    destination: str = Field(min_length=3)
    litres: float = Field(gt=0)
    fuelType: str = Field(default="U91")
    wMoney: float = Field(default=0.5, ge=0.0, le=1.0)   # money weight  (0–1)
    wTime: float = Field(default=0.5, ge=0.0, le=1.0)    # time weight   (0–1)
    lPer100km: float = Field(default=8.0, ge=1.0, le=30.0)  # ← add this


class RoutesRequest(BaseModel):
    origin: str = Field(min_length=3)
    destination: str = Field(min_length=3)
    avoidTolls: bool = Field(default=False)  # True => avoid tolls


class StationOut(BaseModel):
    station_code: str
    name: str
    brand: str
    brand_key: str
    fuel_type: str
    price: float  # dollars per litre
    lat: float
    lng: float
    distance_km: float
    address: Optional[str] = None
    last_updated: Optional[str] = None


# =========================================================
# Google Routes helpers
# =========================================================
def _location_obj(s: str) -> dict:
    """Routes API v2 rejects lat,lng strings as addresses — use latLng object instead."""
    m = re.match(r"^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$", s.strip())
    if m:
        return {"location": {"latLng": {"latitude": float(m.group(1)), "longitude": float(m.group(2))}}}
    return {"address": s}


def _departure_rfc3339(minutes_ahead: int = 1) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _field_mask() -> str:
    # Keep minimal: you said you don't want polyline etc.
    return ",".join(
        [
            "routes.duration",
            "routes.distanceMeters",
            "routes.travelAdvisory.tollInfo",
        ]
    )


def _duration_to_minutes(duration_str: str) -> float:
    if not duration_str:
        return 0.0
    seconds = float(duration_str.rstrip("s"))
    return round(seconds / 60.0, 1)


def _meters_to_km(meters: int) -> float:
    return round(meters / 1000.0, 2)


def _toll_aud(route: Dict[str, Any]) -> Optional[float]:
    advisory = route.get("travelAdvisory") or {}
    toll_info = advisory.get("tollInfo")
    if not toll_info:
        return None
    est = (toll_info.get("estimatedPrice") or [])
    if not est:
        return None
    p0 = est[0] or {}
    units = float(p0.get("units", "0") or "0")
    nanos = float(p0.get("nanos", 0) or 0)
    value = units + nanos / 1_000_000_000.0
    return round(value, 2) if value > 0 else None


def _simplify_route(route: Dict[str, Any]) -> Dict[str, Any]:
    meters = int(route.get("distanceMeters", 0) or 0)
    duration = str(route.get("duration", "") or "")
    toll = _toll_aud(route)
    return {
        "km": _meters_to_km(meters),
        "minutes": _duration_to_minutes(duration),
        "has_tolls": toll is not None,
        "toll_aud": toll,
    }


async def _compute_route(origin: str, destination: str, *, avoid_tolls: bool) -> Dict[str, Any]:
    api_key = _google_api_key()

    payload = {
        "origin": _location_obj(origin),
        "destination": _location_obj(destination),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "departureTime": _departure_rfc3339(1),  # must be in the future
        "computeAlternativeRoutes": False,
        "routeModifiers": {
            "avoidTolls": avoid_tolls,
            "avoidHighways": False,
            "avoidFerries": False,
        },
        "extraComputations": ["TOLLS"],
        "languageCode": "en-AU",
        "units": "METRIC",
    }

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _field_mask(),
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(GOOGLE_ROUTES_URL, json=payload, headers=headers)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Routes API error: {r.text}")

    data = r.json()
    routes = data.get("routes") or []
    if not routes:
        raise HTTPException(status_code=404, detail="No route returned by Google Routes API")
    return routes[0]


# =========================================================
# NSW FuelCheck / FuelPriceCheck: multi-account rotation
# =========================================================

def _now_req_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%d/%m/%Y %I:%M:%S %p")


def _basic_auth_value(api_key: str, api_secret: str) -> str:
    raw = f"{api_key}:{api_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


# Build account pool from .env: NSW_API_KEY_1/NSW_API_SECRET_1, _2, _3, ...
# Falls back to NSW_API_KEY / NSW_API_SECRET for single-account setups.
def _load_nsw_accounts() -> List[Dict[str, str]]:
    accounts = []
    i = 1
    while True:
        key    = os.getenv(f"NSW_API_KEY_{i}", "").strip()
        secret = os.getenv(f"NSW_API_SECRET_{i}", "").strip()
        if not key or not secret:
            break
        accounts.append({"key": key, "secret": secret})
        i += 1
    # fallback: single account without suffix
    if not accounts:
        key    = os.getenv("NSW_API_KEY", "").strip()
        secret = os.getenv("NSW_API_SECRET", "").strip()
        if key and secret:
            accounts.append({"key": key, "secret": secret})
    return accounts

_NSW_ACCOUNTS: List[Dict[str, str]] = _load_nsw_accounts()

# Per-account state: token, expiry, rate-limited-until
_NSW_ACCOUNT_STATE: List[Dict] = [
    {"token": None, "expiry": None, "limited_until": None}
    for _ in _NSW_ACCOUNTS
]

_PRICES_CACHE: List[Dict[str, Any]] = []
_PRICES_CACHE_AT: Optional[datetime] = None
CACHE_TTL = timedelta(hours=24)


def _active_account_index() -> int:
    """Return the index of the first account that is not rate-limited."""
    now = datetime.now(timezone.utc)
    for i, state in enumerate(_NSW_ACCOUNT_STATE):
        limited = state.get("limited_until")
        if limited is None or now >= limited:
            return i
    # All accounts are limited — return the one whose limit expires soonest
    return min(range(len(_NSW_ACCOUNT_STATE)),
               key=lambda i: _NSW_ACCOUNT_STATE[i]["limited_until"] or datetime.max.replace(tzinfo=timezone.utc))


async def _get_nsw_token(account_index: Optional[int] = None) -> tuple[str, int]:
    """
    Returns (token, account_index) for the active account.
    Fetches a new token if needed.
    """
    if not _NSW_ACCOUNTS:
        raise HTTPException(status_code=500, detail="No NSW API accounts configured in .env")

    idx = account_index if account_index is not None else _active_account_index()
    state   = _NSW_ACCOUNT_STATE[idx]
    account = _NSW_ACCOUNTS[idx]

    now = datetime.now(timezone.utc)
    if state["token"] and state["expiry"] and now < state["expiry"]:
        return state["token"], idx

    headers = {"Authorization": _basic_auth_value(account["key"], account["secret"])}
    params  = {"grant_type": "client_credentials"}

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(NSW_TOKEN_URL, headers=headers, params=params)

    if r.status_code == 429:
        state["limited_until"] = now + timedelta(days=7)  # ← rate-limit duration: adjust here if needed
        logging.getLogger("fuel").warning("NSW account %d rate-limited on token fetch, rotating.", idx)
        next_idx = _active_account_index()
        if next_idx == idx:
            raise HTTPException(status_code=429, detail="All NSW API accounts are rate-limited.")
        return await _get_nsw_token(next_idx)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"NSW token error (account {idx+1}): {r.text}")

    data       = r.json()
    token      = data.get("access_token") or data.get("accessToken")
    expires_in = data.get("expires_in") or data.get("expiresIn") or 43200

    if not token:
        raise HTTPException(status_code=502, detail=f"NSW token missing access_token: {data}")

    state["token"]  = token
    state["expiry"] = now + timedelta(seconds=int(expires_in) - 300)
    return token, idx


def _nsw_headers(token: str, account_index: int) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "apikey": _NSW_ACCOUNTS[account_index]["key"],
        "transactionid": str(uuid.uuid4()),
        "requesttimestamp": _now_req_timestamp(),
        "if-modified-since": _now_req_timestamp(),
    }


async def _nsw_get_with_rotation(url: str, extra_headers: Dict = {}, params: Dict = {}) -> Dict:
    """
    Makes a NSW API GET request, automatically rotating accounts on 429.
    """
    token, idx = await _get_nsw_token()
    headers = {**_nsw_headers(token, idx), **extra_headers}

    async with httpx.AsyncClient(timeout=40.0) as client:
        r = await client.get(url, headers=headers, params=params)

    if r.status_code == 429:
        _NSW_ACCOUNT_STATE[idx]["limited_until"] = datetime.now(timezone.utc) + timedelta(days=7)  # ← rate-limit duration: adjust here if needed
        logging.getLogger("fuel").warning("NSW account %d hit rate limit, rotating.", idx)
        # Invalidate token so next call fetches fresh one for next account
        _NSW_ACCOUNT_STATE[idx]["token"] = None
        return await _nsw_get_with_rotation(url, extra_headers, params)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"NSW API error: {r.text}")

    return r.json()


async def _nsw_post_with_rotation(url: str, body: Dict) -> Dict:
    """
    Makes a NSW API POST request, automatically rotating accounts on 429.
    """
    token, idx = await _get_nsw_token()
    headers = _nsw_headers(token, idx)

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=body)

    if r.status_code == 429:
        _NSW_ACCOUNT_STATE[idx]["limited_until"] = datetime.now(timezone.utc) + timedelta(days=7)  # ← rate-limit duration: adjust here if needed
        logging.getLogger("fuel").warning("NSW account %d hit rate limit on POST, rotating.", idx)
        _NSW_ACCOUNT_STATE[idx]["token"] = None
        return await _nsw_post_with_rotation(url, body)

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"NSW API error: {r.text}")

    return r.json()

async def _refresh_reference_if_needed(force: bool = False) -> None:
    """
    Fetch LOVs (station metadata like brand + lat/lng) and cache it.
    Uses account rotation automatically.
    """
    global _STATIONS_REF, _STATIONS_REF_AT

    if not force and _STATIONS_REF_AT and (datetime.now(timezone.utc) - _STATIONS_REF_AT) < _STATIONS_REF_TTL:
        return

    data = await _nsw_get_with_rotation(
        NSW_LOVS_URL,
        extra_headers={"if-modified-since": "01/01/2000 12:00:00 AM"},
        params={"states": "NSW"},
    )

    stations_list = None

    # Try common keys for stations
    for key in ["stations", "station", "Stations", "Station"]:
        if key in data:
            val = data[key]
            # Handle both list and {"items": [...]} formats
            if isinstance(val, list):
                stations_list = val
                break
            elif isinstance(val, dict) and "items" in val:
                stations_list = val["items"]
                break

    # If still not found, try searching recursively
    if not stations_list:
        def _find_station_list(obj, depth=0):
            if depth > 3:  # Prevent infinite recursion
                return None
            if isinstance(obj, list) and obj:
                # Check if this looks like a stations list
                if isinstance(obj[0], dict):
                    keys = set(str(k).lower() for k in obj[0].keys())
                    if any(x in keys for x in ["stationcode", "code"]) and \
                       any(x in keys for x in ["latitude", "lat"]):
                        return obj
                return None
            if isinstance(obj, dict):
                for v in obj.values():
                    found = _find_station_list(v, depth + 1)
                    if found:
                        return found
            return None

        stations_list = _find_station_list(data)

    if not stations_list:
        # Debug: show what keys we got
        top_keys = list(data.keys()) if isinstance(data, dict) else f"type={type(data)}"
        raise HTTPException(
            status_code=500, 
            detail=f"Could not find stations in LOVs. Top-level keys: {top_keys}"
        )

    ref: dict[int, dict] = {}
    for s in stations_list:
        if not isinstance(s, dict):
            continue

        # Extract station ID (NSW uses "stationid" or "code")
        station_id = s.get("stationid") or s.get("code") or s.get("stationcode")
        if not station_id:
            continue

        # stationid might be a string, try to convert to int
        try:
            sc_int = int(station_id)
        except Exception:
            # If it's a string like "12345", use hash as fallback
            try:
                sc_int = int(str(station_id).strip())
            except Exception:
                continue

        # Extract name and brand
        name = s.get("name") or ""
        brand = s.get("brand") or ""

        # Extract lat/lng from nested "location" object
        location = s.get("location")
        if not location or not isinstance(location, dict):
            continue

        lat = location.get("latitude")
        lng = location.get("longitude")

        if lat is None or lng is None:
            continue

        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except Exception:
            continue

        address = str(location.get("address") or "").strip()
        suburb  = str(location.get("suburb") or "").strip()
        address_line = ", ".join(part for part in [address, suburb] if part)

        ref[sc_int] = {
            "stationcode": sc_int,
            "name": str(name),
            "brand": str(brand),
            "lat": lat_f,
            "lng": lng_f,
            "address": address_line,
        }

    if not ref:
        raise HTTPException(
            status_code=500,
            detail=f"Parsed stations list but extracted 0 valid stations. List length: {len(stations_list)}"
        )

    _STATIONS_REF = ref
    _STATIONS_REF_AT = datetime.now(timezone.utc)

async def _refresh_prices_if_needed(force: bool = False) -> None:
    """
    Calls NSW 'all prices' endpoint at most once per 24h.
    Stores the full dataset in memory.
    """
    global _PRICES_CACHE, _PRICES_CACHE_AT

    if not force and _PRICES_CACHE_AT and (datetime.now(timezone.utc) - _PRICES_CACHE_AT) < CACHE_TTL:
        return

    data = await _nsw_get_with_rotation(NSW_PRICES_ALL_URL)

    # NSW responses sometimes are {"prices":[...]} or just [...]
    if isinstance(data, dict):
        prices = data.get("prices") or data.get("Prices") or data.get("data") or []
    elif isinstance(data, list):
        prices = data
    else:
        prices = []

    if not isinstance(prices, list):
        raise HTTPException(status_code=502, detail=f"Unexpected NSW prices payload shape: {type(prices)}")

    _PRICES_CACHE = prices
    _PRICES_CACHE_AT = datetime.now(timezone.utc)


# =========================================================
# VIC Fair Fuel Open Data helpers
# =========================================================

def _vic_headers() -> Dict[str, str]:
    return {
        "x-consumer-id": _VIC_CONSUMER_ID,
        "x-transactionid": str(uuid.uuid4()),
        "User-Agent": "FuelOptimiser/1.0",
        "Accept": "application/json",
    }


async def _refresh_vic_prices_if_needed(force: bool = False) -> None:
    """
    Fetches all VIC fuel prices in a single call. Cached for 24h.
    VIC API returns station metadata + prices combined in one response.
    """
    global _VIC_PRICES_CACHE, _VIC_PRICES_CACHE_AT

    if not _VIC_CONSUMER_ID:
        logging.getLogger("fuel").warning("VIC_CONSUMER_ID not set — skipping VIC fetch")
        return

    if not force and _VIC_PRICES_CACHE_AT and (datetime.now(timezone.utc) - _VIC_PRICES_CACHE_AT) < CACHE_TTL:
        return

    async with httpx.AsyncClient(timeout=40.0) as client:
        r = await client.get(VIC_PRICES_URL, headers=_vic_headers())

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"VIC API error: {r.status_code} {r.text[:300]}")

    data = r.json()
    entries = data.get("fuelPriceDetails") or data.get("stations") or data.get("Stations") or []

    if not isinstance(entries, list):
        raise HTTPException(status_code=502, detail=f"Unexpected VIC prices payload: {type(entries)}")

    _VIC_PRICES_CACHE = entries
    _VIC_PRICES_CACHE_AT = datetime.now(timezone.utc)
    logging.getLogger("fuel").info("VIC prices cached: %d stations", len(entries))


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _norm_brand_key(brand: str) -> str:
    # for later: map to local assets like assets/brands/ampol.png etc
    b = (brand or "").strip().lower()
    for ch in [" ", "-", ".", "&", "'", "/", "(", ")", ","]:
        b = b.replace(ch, "")
    return b

def _map_app_fuel_to_nsw(app_fuel: str) -> str:
    """Map UI fuel type codes to NSW FuelCheck API codes."""
    f = app_fuel.upper().strip()
    mapping = {
        "E10":    "E10",
        "U91":    "U91",
        "P95":    "P95",   # Premium 95
        "P98":    "P98",   # Premium 98
        "DL":     "DL",    # Diesel
        "DIESEL": "DL",
        "PDL":    "PDL",   # Premium Diesel
    }
    return mapping.get(f, f)


def _map_app_fuel_to_vic(app_fuel: str) -> str:
    """Map UI fuel type codes to VIC Fair Fuel API codes."""
    f = app_fuel.upper().strip()
    mapping = {
        "U91":    "U91",
        "P95":    "P95",
        "P98":    "P98",
        "DL":     "DSL",   # NSW uses DL, VIC uses DSL
        "DIESEL": "DSL",
        "PDL":    "PDSL",  # NSW uses PDL, VIC uses PDSL
        "E10":    "E10",
        "E85":    "E85",
        "LPG":    "LPG",
    }
    return mapping.get(f, f)


def _is_vic_coords(lat: float, lng: float) -> bool:
    """Bounding box check: is this point in Victoria?"""
    return -39.2 <= lat <= -33.98 and 140.96 <= lng <= 150.03


# Maps normalised brand keys → canonical logo filename (without .png extension).
# Ampol variants and Caltex all share the same logo.
# Small/one-off stations fall back to "independent".
_BRAND_KEY_CANONICAL: Dict[str, str] = {
    # Metro Fuel (VIC first-word extraction gives "metro", NSW LOVs gives "metrofuel")
    "metro":                   "metrofuel",
    # Ampol family
    "ampolbreeze":             "ampol",
    "ampolfoodary":            "ampol",
    "egampol":                 "ampol",
    "ebmampol":                "ampol",
    "caltex":                  "ampol",
    # Mobil variants
    "mobil1carlingfordcarcare": "mobil",
    # Small / independent stations
    "auspetroleum":            "independent",
    "apw":                     "independent",
    "apco":                    "independent",
    "astron":                  "independent",
    "arkoenergy":              "independent",
    "bangalowgeneralstore":    "independent",
    "bargopetroleum":          "independent",
    "bendalonggeneralstore":   "independent",
    "boostfuel":               "independent",
    "bribbareeservo":          "independent",
    "calvipetrol":             "independent",
    "coralpetroleum":          "independent",
    "evup":                    "independent",
    "ezfuel":                  "independent",
    "enhance":                 "independent",
    "everty":                  "independent",
    "exploren":                "independent",
    "greensmandurama":         "independent",
    "highlandfuels":           "independent",
    "hopefuel":                "independent",
    "iorgroup":                "independent",
    "independentev":           "independent",
    "infinity":                "independent",
    "inlandpetroleum":         "independent",
    "lowes":                   "independent",
    "npgretail":               "independent",
    "pearlenergy":             "independent",
    "powerfuel":               "independent",
    "prime":                   "independent",
    "roopetroleum":            "independent",
    "ruralfuel":               "independent",
    "southwest":               "independent",
    "supremefuel":             "independent",
    "temcopetroleum":          "independent",
    "themajor":                "independent",
    "tinoneegeneralstore":     "independent",
    "transwestfuels":          "independent",
    "ugo":                     "independent",
    "westside":                "independent",
    "woodhampetroleum":        "independent",
}


_KNOWN_BRAND_LOGOS = {
    "7eleven", "ampol", "bp", "budget", "colesexpress", "costco",
    "independent", "liberty", "metrofuel", "mobil", "nrma",
    "reddyexpress", "shell", "speedway", "ultrapetroleum", "united",
}

def _canonical_brand_key(brand: str) -> str:
    """Normalise brand string and resolve to canonical logo key.
    Falls back to 'independent' if no matching logo file exists."""
    raw = _norm_brand_key(brand)
    resolved = _BRAND_KEY_CANONICAL.get(raw, raw)
    return resolved if resolved in _KNOWN_BRAND_LOGOS else "independent"


def _extract_station_fields(p: Dict[str, Any]) -> Optional[Tuple[str, str, str, float, float, float, str, Optional[str]]]:
    """
    Try hard to pull out consistent fields from NSW payload without relying
    on exact casing/keys (because swagger fields vary across versions).
    Returns:
      (station_code, name, brand, lat, lng, price, fuel_type, last_updated)
    """
    def g(*keys, default=None):
        for k in keys:
            if k in p and p[k] is not None:
                return p[k]
        return default

    station_code = str(g("stationcode", "stationCode", "StationCode", default="")).strip()
    name = str(g("stationname", "stationName", "StationName", "name", default="")).strip()
    brand = str(g("brand", "Brand", "brandname", "brandName", default="")).strip()

    lat = g("latitude", "lat", "Latitude")
    lng = g("longitude", "lng", "Longitude")
    price = g("price", "Price")
    fuel_type = str(g("fueltype", "fuelType", "FuelType", default="")).strip()

    last_updated = g("lastupdated", "lastUpdated", "LastUpdated", "pricesubmitted", "priceSubmitted", default=None)
    if last_updated is not None:
        last_updated = str(last_updated)

    # some payloads use "location": {"latitude":..., "longitude":...}
    if (lat is None or lng is None) and isinstance(p.get("location"), dict):
        loc = p["location"]
        lat = lat if lat is not None else loc.get("latitude") or loc.get("lat")
        lng = lng if lng is not None else loc.get("longitude") or loc.get("lng")

    try:
        lat_f = float(lat)
        lng_f = float(lng)
        price_f = float(price)
    except Exception:
        return None

    if not station_code:
        # if station code isn't present, derive a stable-ish id
        station_code = f"{brand}:{name}:{lat_f:.6f}:{lng_f:.6f}"

    if not name:
        name = brand or "Station"

    if not brand:
        brand = "Unknown"

    return station_code, name, brand, lat_f, lng_f, price_f, fuel_type, last_updated


# =========================================================
# Endpoints
# =========================================================
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/env")
def debug_env():
    """Temporary: shows which env vars are present (not their values)."""
    keys_to_check = [
        "GOOGLE_API_KEY", "NSW_API_KEY_1", "NSW_API_SECRET_1",
        "NSW_API_KEY_2", "NSW_API_SECRET_2", "NSW_API_KEY_3",
        "NSW_API_SECRET_3", "NSW_API_KEY_4", "NSW_API_SECRET_4",
    ]
    return {k: ("SET" if os.getenv(k) else "MISSING") for k in keys_to_check}


_STATE_BIAS = {
    "VIC": {"lat": -37.0, "lng": 144.5, "radius": "600000"},
    "NSW": {"lat": -32.5, "lng": 147.0, "radius": "800000"},
}

@app.get("/autocomplete")
async def autocomplete(q: str, lat: float = -33.8688, lng: float = 151.2093, state: str = ""):
    """
    Proxy for Google Places Autocomplete — avoids CORS and keeps the key server-side.
    Optional `state` param ("NSW" or "VIC") biases results to that state.
    """
    if not q or len(q.strip()) < 2:
        return []

    bias = _STATE_BIAS.get(state.upper().strip())
    if bias:
        bias_lat, bias_lng, bias_radius = bias["lat"], bias["lng"], bias["radius"]
    else:
        bias_lat, bias_lng, bias_radius = lat, lng, "50000"

    params = {
        "input": q.strip(),
        "key": _google_api_key(),
        "components": "country:au",
        "types": "geocode",
        "location": f"{bias_lat},{bias_lng}",
        "radius": bias_radius,
        "language": "en-AU",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/place/autocomplete/json",
            params=params,
        )

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Places API HTTP error: {r.text}")

    data = r.json()
    status = data.get("status", "")
    if status == "ZERO_RESULTS":
        return []
    if status != "OK":
        raise HTTPException(
            status_code=502,
            detail=f"Places API status={status}: {data.get('error_message', '')}",
        )

    return [
        {"description": p["description"], "place_id": p["place_id"]}
        for p in data.get("predictions", [])[:5]
    ]


@app.post("/routes")
async def routes(body: RoutesRequest):
    route_raw = await _compute_route(body.origin, body.destination, avoid_tolls=body.avoidTolls)
    return {"avoidTolls": body.avoidTolls, "route": _simplify_route(route_raw)}


def _looks_like_vic(s: str) -> bool:
    """Returns True if the address string (or lat,lng) looks like it's in Victoria."""
    coords = re.match(r"^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$", s.strip())
    if coords:
        return _is_vic_coords(float(coords.group(1)), float(coords.group(2)))
    s_up = s.upper()
    return " VIC " in s_up or " VIC," in s_up or s_up.endswith(" VIC") or "VICTORIA" in s_up


@app.post("/optimise")
def optimise(body: OptimiseRequest):
    try:
        pre_loaded_data = None
        origin_is_vic = _looks_like_vic(body.origin)

        if origin_is_vic and _VIC_PRICES_CACHE:
            # Build pre_loaded_data from VIC cache in the format run_optimiser expects.
            # VIC station IDs are strings, so we use sequential ints as codes.
            vic_fuel = _map_app_fuel_to_vic(body.fuelType.upper())
            stations_list = []
            prices_list = []
            for idx, entry in enumerate(_VIC_PRICES_CACHE):
                fs = entry.get("fuelStation") or {}
                location = fs.get("location") or {}
                s_lat = location.get("latitude")
                s_lng = location.get("longitude")
                if s_lat is None or s_lng is None:
                    continue
                fuel_prices = entry.get("fuelPrices") or []
                matching = next(
                    (pr for pr in fuel_prices
                     if (pr.get("fuelType") or "").upper() == vic_fuel and pr.get("isAvailable", True)),
                    None
                )
                if not matching:
                    continue
                name = str(fs.get("name") or "Station")
                brand = name.split()[0] if name else ""
                stations_list.append({
                    "code": str(idx),
                    "name": name,
                    "brand": brand,
                    "location": {"latitude": float(s_lat), "longitude": float(s_lng)},
                })
                prices_list.append({
                    "stationcode": idx,
                    "fueltype": vic_fuel,
                    "price": float(matching.get("price") or 0),
                })
            pre_loaded_data = {"stations": stations_list, "prices": prices_list}

        elif _PRICES_CACHE and _STATIONS_REF:
            # NSW path (existing)
            ft_nsw = _map_app_fuel_to_nsw(body.fuelType.upper())
            prices_list = [
                p for p in _PRICES_CACHE
                if (p.get("fueltype") or p.get("fuelType") or "").upper() == ft_nsw
            ]
            needed_codes: set = set()
            for p in prices_list:
                sc = p.get("stationcode")
                if sc is not None:
                    try:
                        needed_codes.add(int(sc))
                    except (ValueError, TypeError):
                        pass
            stations_list = [
                {
                    "code": str(sc),
                    "name": ref["name"],
                    "brand": ref["brand"],
                    "location": {"latitude": ref["lat"], "longitude": ref["lng"]},
                }
                for sc, ref in _STATIONS_REF.items()
                if sc in needed_codes
            ]
            pre_loaded_data = {"stations": stations_list, "prices": prices_list}

        return run_optimiser(
            origin=body.origin,
            destination=body.destination,
            litres=body.litres,
            fuel_type=body.fuelType.upper(),
            w_money=body.wMoney,
            w_time=body.wTime,
            l_per_100km=body.lPer100km,
            pre_loaded_data=pre_loaded_data,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stations", response_model=List[StationOut])
async def stations(
    lat: float,
    lng: float,
    radius_km: float = 5.0,
    fuel_type: str = "U91",
):
    """
    Returns stations near (lat, lng) within radius_km for the given fuel_type.
    Routes to VIC or NSW API based on the coordinates.
    Prices are cached daily; NSW station reference is cached weekly.
    """
    if radius_km <= 0 or radius_km > 50:
        raise HTTPException(status_code=400, detail="radius_km must be between 0 and 50")

    ft = fuel_type.upper().strip()
    out: List[StationOut] = []

    if _is_vic_coords(lat, lng):
        # ---- VIC path ----
        await _refresh_vic_prices_if_needed(force=False)
        vic_fuel = _map_app_fuel_to_vic(ft)

        for entry in _VIC_PRICES_CACHE:
            if not isinstance(entry, dict):
                continue

            fs = entry.get("fuelStation") or {}

            location = fs.get("location") or {}
            s_lat = location.get("latitude")
            s_lng = location.get("longitude")

            if s_lat is None or s_lng is None:
                continue

            try:
                s_lat = float(s_lat)
                s_lng = float(s_lng)
            except Exception:
                continue

            d = _haversine_km(lat, lng, s_lat, s_lng)
            if d > float(radius_km):
                continue

            # Find price for requested fuel type (skip unavailable)
            fuel_prices = entry.get("fuelPrices") or []
            matching_price = None
            for pr in fuel_prices:
                if (pr.get("fuelType") or "").upper() == vic_fuel and pr.get("isAvailable", True):
                    matching_price = pr
                    break

            if not matching_price:
                continue

            try:
                raw_price = float(matching_price.get("price") or 0)
            except Exception:
                continue

            # VIC prices are in cents per litre
            price = raw_price / 100.0 if raw_price > 20 else raw_price

            name = str(fs.get("name") or "Station")
            site_id = str(fs.get("id") or f"{s_lat:.5f}:{s_lng:.5f}")
            address_str = str(fs.get("address") or "") or None
            last_updated = str(matching_price.get("updatedAt") or "") or None

            # Best-effort brand: first word of station name (e.g. "United Wodonga" → "United")
            brand = name.split()[0] if name else ""

            out.append(StationOut(
                station_code=f"VIC-{site_id}",
                name=name,
                brand=brand,
                brand_key=_canonical_brand_key(brand),
                fuel_type=ft,
                price=round(price, 3),
                lat=s_lat,
                lng=s_lng,
                distance_km=round(d, 2),
                address=address_str,
                last_updated=last_updated,
            ))

    else:
        # ---- NSW path ----
        await _refresh_reference_if_needed(force=False)
        await _refresh_prices_if_needed(force=False)

        for p in _PRICES_CACHE:
            if not isinstance(p, dict):
                continue

            # Extract station code/ID - NSW uses "stationcode" in prices endpoint
            station_code = p.get("stationcode") or p.get("stationCode") or p.get("code")
            if station_code is None:
                continue

            try:
                station_code_int = int(station_code)
            except Exception:
                continue

            # Fuel type from NSW payload
            p_fuel_type = (p.get("fueltype") or p.get("fuelType") or "").strip().upper()

            # Map your app fuel_type to NSW codes and filter
            if p_fuel_type and _map_app_fuel_to_nsw(ft) != p_fuel_type:
                continue

            # Price extraction - NSW returns price in cents per litre
            try:
                raw_price = float(p.get("price") or 0)
            except Exception:
                continue

            # NSW prices are in cents, convert to dollars
            price = raw_price / 100.0 if raw_price > 20 else raw_price

            # Extract last updated timestamp
            last_updated = str(p.get("lastupdated") or p.get("lastUpdated") or "") or None

            # Join to station reference cache to get name/brand/lat/lng
            ref = _STATIONS_REF.get(station_code_int)
            if not ref:
                continue

            name = ref.get("name") or f"Station {station_code_int}"
            brand = ref.get("brand") or "Unknown"
            s_lat = float(ref["lat"])
            s_lng = float(ref["lng"])

            d = _haversine_km(lat, lng, s_lat, s_lng)
            if d <= float(radius_km):
                out.append(StationOut(
                    station_code=str(station_code_int),
                    name=str(name),
                    brand=str(brand),
                    brand_key=_canonical_brand_key(str(brand)),
                    fuel_type=ft,
                    price=round(price, 3),
                    lat=s_lat,
                    lng=s_lng,
                    distance_km=round(d, 2),
                    address=ref.get("address") or None,
                    last_updated=last_updated,
                ))

    # Sort by price first, then distance
    out.sort(key=lambda s: (s.price, s.distance_km))
    return out

@app.get("/debug/vic-raw")
async def debug_vic_raw():
    """Returns the raw VIC API response so we can inspect its structure."""
    if not _VIC_CONSUMER_ID:
        return {"error": "VIC_CONSUMER_ID not set"}
    async with httpx.AsyncClient(timeout=40.0) as client:
        r = await client.get(VIC_PRICES_URL, headers=_vic_headers())
    try:
        data = r.json()
    except Exception:
        return {"status_code": r.status_code, "raw_text": r.text[:500]}
    # Return top-level keys + first item of any list value to reveal structure
    preview = {"status_code": r.status_code, "top_level_keys": list(data.keys()) if isinstance(data, dict) else f"type={type(data).__name__}"}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and v:
                preview[f"{k}_count"] = len(v)
                preview[f"{k}_first_item"] = v[0]
                break
    elif isinstance(data, list) and data:
        preview["list_count"] = len(data)
        preview["first_item"] = data[0]
    return preview


@app.get("/debug/caches")
async def debug_caches():
    """Debug endpoint to check cache status"""
    return {
        "prices_cache": {
            "count": len(_PRICES_CACHE),
            "cached_at": _PRICES_CACHE_AT.isoformat() if _PRICES_CACHE_AT else None,
            "sample": _PRICES_CACHE[:2] if _PRICES_CACHE else [],
        },
        "stations_ref": {
            "count": len(_STATIONS_REF),
            "cached_at": _STATIONS_REF_AT.isoformat() if _STATIONS_REF_AT else None,
            "sample_keys": list(_STATIONS_REF.keys())[:5] if _STATIONS_REF else [],
            "sample_values": [_STATIONS_REF[k] for k in list(_STATIONS_REF.keys())[:2]] if _STATIONS_REF else [],
        }
    }


@app.get("/debug/match-test")
async def debug_match_test(fuel_type: str = "U91"):
    """Test if prices can match with station reference"""
    await _refresh_reference_if_needed(force=False)
    await _refresh_prices_if_needed(force=False)
    
    ft = fuel_type.upper().strip()
    
    # Get first 10 prices for this fuel type
    matching_prices = []
    unmatched_prices = []
    
    for p in _PRICES_CACHE[:100]:  # Check first 100
        if not isinstance(p, dict):
            continue
            
        p_fuel_type = (p.get("fueltype") or p.get("fuelType") or "").strip().upper()
        if p_fuel_type != _map_app_fuel_to_nsw(ft):
            continue
            
        station_code = p.get("stationcode") or p.get("stationCode")
        if station_code is None:
            continue
            
        try:
            sc_int = int(station_code)
        except Exception:
            continue
            
        if sc_int in _STATIONS_REF:
            matching_prices.append({
                "stationcode": sc_int,
                "price": p.get("price"),
                "ref": _STATIONS_REF[sc_int]
            })
        else:
            unmatched_prices.append({
                "stationcode": sc_int,
                "price": p.get("price"),
            })
        
        if len(matching_prices) >= 5:
            break
    
    return {
        "fuel_type_requested": ft,
        "nsw_fuel_code": _map_app_fuel_to_nsw(ft),
        "matching_prices": matching_prices,
        "unmatched_sample": unmatched_prices[:5],
        "total_prices": len(_PRICES_CACHE),
        "total_stations_ref": len(_STATIONS_REF),
    }

@app.post("/stations/refresh")
async def refresh_stations():
    await _refresh_reference_if_needed(force=True)
    await _refresh_prices_if_needed(force=True)
    if _VIC_CONSUMER_ID:
        await _refresh_vic_prices_if_needed(force=True)
    return {
        "ok": True,
        "nsw_prices_count": len(_PRICES_CACHE),
        "nsw_ref_count": len(_STATIONS_REF),
        "nsw_prices_at": _PRICES_CACHE_AT.isoformat() if _PRICES_CACHE_AT else None,
        "nsw_ref_at": _STATIONS_REF_AT.isoformat() if _STATIONS_REF_AT else None,
        "vic_stations_count": len(_VIC_PRICES_CACHE),
        "vic_prices_at": _VIC_PRICES_CACHE_AT.isoformat() if _VIC_PRICES_CACHE_AT else None,
    }

@app.get("/stations/debug/sample")
async def stations_debug_sample(n: int = 1):
    """
    Returns the first N raw items from the NSW cache so we can adapt _extract_station_fields().
    """
    return {
        "cached_count": len(_PRICES_CACHE),
        "sample": _PRICES_CACHE[: max(0, min(n, 5))],
    }


# =========================================================
# Local run
# =========================================================
if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=True)
