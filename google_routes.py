import os
import re
import datetime as dt
import requests
from typing import List, Tuple, Optional, Any
from dotenv import load_dotenv

load_dotenv()

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def _location_obj(s: str) -> dict:
    """
    Routes API v2 requires coordinates as {"location": {"latLng": ...}}.
    Passing a lat,lng string as {"address": ...} returns 400.
    Detect coord strings and use the correct format.
    """
    m = re.match(r"^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$", s.strip())
    if m:
        return {"location": {"latLng": {"latitude": float(m.group(1)), "longitude": float(m.group(2))}}}
    return {"address": s}


def _rfc3339_future_utc(minutes_ahead: int = 2) -> str:
    return (dt.datetime.utcnow() + dt.timedelta(minutes=minutes_ahead)).replace(
        microsecond=0
    ).isoformat() + "Z"


def _parse_duration_to_minutes(duration_str: str) -> float:

    if not duration_str or not duration_str.endswith("s"):
        raise ValueError(f"Unexpected duration format: {duration_str}")
    seconds = float(duration_str[:-1])
    return seconds / 60.0


def _decode_polyline(encoded: str) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0

    while index < len(encoded):
        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        points.append((lat / 1e5, lng / 1e5))

    return points


def get_route(
    origin: str,
    destination: str,
    waypoint: Optional[Tuple[float, float]] = None,
    routing_preference: str = "TRAFFIC_AWARE_OPTIMAL",
    departure_time: Optional[str] = None,
    departure_minutes_ahead: int = 2,
) -> Tuple[float, float, List[Tuple[float, float]]]:
    """
    Returns: (minutes, km, route_points)

    IMPORTANT:
    Pass a fixed departure_time from fuel_app.py so baseline and via routes
    are comparable (otherwise traffic changes between calls).
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GOOGLE_API_KEY in .env")

    if departure_time is None:
        departure_time = _rfc3339_future_utc(departure_minutes_ahead)

    body: dict[str, Any] = {
        "origin": _location_obj(origin),
        "destination": _location_obj(destination),
        "travelMode": "DRIVE",
        "routingPreference": routing_preference,
        "departureTime": departure_time,
    }

    if waypoint is not None:
        lat, lng = waypoint
        body["intermediates"] = [
            {
                "location": {"latLng": {"latitude": float(lat), "longitude": float(lng)}},
                "vehicleStopover": True,   # force it as a stop
            }
        ]

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline",
    }

    r = requests.post(ROUTES_URL, headers=headers, json=body, timeout=20)

    if r.status_code != 200:
        print("\nGOOGLE DEBUG")
        print("STATUS:", r.status_code)
        try:
            print(r.json())
        except Exception:
            print(r.text)
        print()
        r.raise_for_status()

    payload = r.json()
    routes = payload.get("routes") or []
    if not routes:
        raise RuntimeError(f"No routes returned: {payload}")

    route0 = routes[0]
    minutes = _parse_duration_to_minutes(route0["duration"])
    km = float(route0["distanceMeters"]) / 1000.0

    encoded_poly = route0.get("polyline", {}).get("encodedPolyline")
    if not encoded_poly:
        raise RuntimeError(f"No polyline returned: {route0}")

    points = _decode_polyline(encoded_poly)
    return minutes, km, points
