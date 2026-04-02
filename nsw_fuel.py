# nsw_fuel.py
import os
import uuid
import time
import json
import datetime as dt
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# -------------------------
# Env
# -------------------------
NSW_API_KEY = (os.getenv("NSW_API_KEY") or "").strip()
# Must be exactly: "Basic <base64(api_key:api_secret)>"
NSW_AUTH_HEADER = (os.getenv("NSW_AUTH_HEADER") or "").strip()

# -------------------------
# Endpoints (NSW OneGov FuelCheck)
# -------------------------
OAUTH_URL = "https://api.onegov.nsw.gov.au/oauth/client_credential/accesstoken"

# Reference data (brands, fuel types, etc.)
LOVS_URL = "https://api.onegov.nsw.gov.au/FuelCheckRefData/v2/fuel/lovs"

# Prices near a lat/lng
NEARBY_URL = "https://api.onegov.nsw.gov.au/FuelPriceCheck/v2/fuel/prices/nearby"

# -------------------------
# Token cache (persist across runs)
# -------------------------
TOKEN_CACHE_FILE = os.path.expanduser("~/.nsw_fuel_token_cache.json")


def _utc_request_timestamp() -> str:
    # NSW expects: dd/MM/yyyy hh:mm:ss AM/PM (UTC)
    return dt.datetime.utcnow().strftime("%d/%m/%Y %I:%M:%S %p")


def _load_cached_token() -> str:
    try:
        with open(TOKEN_CACHE_FILE, "r") as f:
            data = json.load(f)
        token = (data.get("access_token") or "").strip()
        expires_at = float(data.get("expires_at", 0))
        if token and time.time() < expires_at:
            return token
    except Exception:
        pass
    return ""


def _save_cached_token(token: str, expires_in_seconds: float) -> None:
    # subtract 60s buffer
    expires_at = time.time() + float(expires_in_seconds) - 60.0
    try:
        with open(TOKEN_CACHE_FILE, "w") as f:
            json.dump({"access_token": token, "expires_at": expires_at}, f)
    except Exception:
        pass


def get_access_token(debug: bool = False) -> str:
    """
    OAuth token via client_credentials.
    Uses NSW_AUTH_HEADER = "Basic <base64(api_key:api_secret)>".
    """
    cached = _load_cached_token()
    if cached:
        if debug:
            print("Using cached NSW token (no OAuth call).")
        return cached

    if not NSW_AUTH_HEADER.startswith("Basic "):
        raise ValueError("NSW_AUTH_HEADER must be: Basic <base64(api_key:api_secret)>")

    headers = {"Authorization": NSW_AUTH_HEADER, "Accept": "application/json"}
    params = {"grant_type": "client_credentials"}

    r = requests.get(OAUTH_URL, headers=headers, params=params, timeout=20)

    if debug:
        print("\n--- TOKEN DEBUG (network call) ---")
        print("STATUS:", r.status_code)
        print("TEXT:", r.text[:800])
        print("---------------------------------\n")

    r.raise_for_status()
    payload = r.json()

    token = (payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError(f"No access_token in token response: {payload}")

    expires_in = payload.get("expires_in", 0)
    try:
        expires_in = float(expires_in)
    except Exception:
        expires_in = 0.0

    if expires_in > 0:
        _save_cached_token(token, expires_in)

    return token


def _fuelcheck_headers(token: str) -> Dict[str, str]:
    """
    Common headers required for FuelCheck protected endpoints.
    """
    if not token:
        raise ValueError("Bearer token missing.")

    if not NSW_API_KEY:
        raise ValueError("NSW_API_KEY env var missing (must be the PLAIN api key).")

    return {
        "Authorization": f"Bearer {token}",
        "apikey": NSW_API_KEY,  # IMPORTANT: plain key, not Basic/base64
        "transactionid": str(uuid.uuid4()),
        "requesttimestamp": _utc_request_timestamp(),
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }


def get_lovs(token: str, states: str = "NSW") -> Dict[str, Any]:
    """
    Fetch reference data lists (brands, fuel types, etc.).
    Docs show it can return NSW and TAS.
    states can be "NSW" or "NSW|TAS" etc (pipe-delimited).
    """
    headers = _fuelcheck_headers(token)

    # Many NSW endpoints support "if-modified-since" but it's required per your screenshot.
    # We'll set it to "01/01/2000 ..." so it always returns something.
    headers["if-modified-since"] = "01/01/2000 12:00:00 AM"

    params = {}
    if states:
        params["states"] = states

    r = requests.get(LOVS_URL, headers=headers, params=params, timeout=30)

    if r.status_code != 200:
        raise Exception(f"LOVs API error ({r.status_code}): {r.text}")

    return r.json()


def get_nearby_prices(
    token: str,
    lat: float,
    lng: float,
    fuel_type: str = "U91",
    radius_km: float = 5.0,
    sort_by: str = "price",
    sort_ascending: bool = True,
) -> Dict[str, Any]:
    """
    Fetch nearby prices around a coordinate.
    """
    headers = _fuelcheck_headers(token)

    body = {
        "fueltype": fuel_type,
        "brand": [],  # empty => all brands
        "namedlocation": "",
        "latitude": str(lat),
        "longitude": str(lng),
        "radius": str(int(round(radius_km))),  # NSW expects int-like string
        "sortby": sort_by,
        "sortascending": "true" if sort_ascending else "false",
    }

    r = requests.post(NEARBY_URL, headers=headers, json=body, timeout=30)

    if r.status_code != 200:
        raise Exception(f"NSW nearby lookup failed ({r.status_code}): {r.text}")

    return r.json()
