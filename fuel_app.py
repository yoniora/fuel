# fuel_app.py
from google_routes import get_route
from dataclasses import dataclass, field
from typing import List, Tuple, Any, Dict, Optional
from nsw_fuel import get_access_token, get_nearby_prices
from cachetools import TTLCache
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import math
import datetime as dt
import json
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fuel")

# Caches
NSW_CACHE = TTLCache(maxsize=100, ttl=86400)   # 1 day
ROUTE_CACHE = TTLCache(maxsize=300, ttl=300)   # 5 minutes
_ROUTE_CACHE_LOCK = threading.Lock()           # TTLCache is not thread-safe


@dataclass
class Candidate:
    name: str
    brand: str
    price_per_l: float
    detour_km: float
    detour_min: float
    money_cost: float
    lat: float
    lng: float
    station_code: str
    score: float = 0.0
    money_regret: float = 0.0
    time_regret: float = 0.0


def _unpack_route(result: Any):
    if isinstance(result, tuple) and len(result) == 3:
        return result[0], result[1], result[2]
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], result[1], None
    raise ValueError("Unexpected return shape from get_route()")


def compute_money_cost(
    price_per_l: float,
    litres_to_buy: float,
    detour_km: float,
    l_per_100km: float = 8.0,   # ← now accepts user's consumption figure
) -> float:
    """
    Total money cost of stopping at this station.

    pump_cost     = litres you fill up × price per litre
    detour_cost   = extra km driven × (L/100km ÷ 100) × price per litre

    l_per_100km defaults to 8.0 (a reasonable Sydney average) but is
    overridden by the value the user sets in the app's Settings screen.
    """
    fuel_eff_l_per_km = l_per_100km / 100.0
    pump_cost = price_per_l * litres_to_buy
    detour_litres = detour_km * fuel_eff_l_per_km
    return pump_cost + detour_litres * price_per_l


def pareto_frontier(cands: List[Candidate]) -> List[Candidate]:
    frontier = []
    for i, a in enumerate(cands):
        dominated = False
        for j, b in enumerate(cands):
            if i == j:
                continue
            if (
                b.money_cost <= a.money_cost
                and b.detour_min <= a.detour_min
                and (b.money_cost < a.money_cost or b.detour_min < a.detour_min)
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(a)
    return frontier


def normalise(values: List[float]) -> List[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def pick_cheapest(cands: List[Candidate]) -> Candidate:
    return min(cands, key=lambda c: c.money_cost)


def pick_fastest(cands: List[Candidate]) -> Candidate:
    return min(cands, key=lambda c: c.detour_min)


def pick_balanced(
    cands: List[Candidate],
    w_money: float = 0.5,
    w_time: float = 0.5,
) -> Candidate:
    """
    Equal-weight (default) or user-preference-weighted regret minimisation.
    Operates on the Pareto frontier only.

    Score = w_money * M_norm + w_time * T_norm
    Best = argmin(Score) → closest to origin in weighted 2D regret space.
    """
    total = w_money + w_time
    if total <= 0:
        w_money, w_time = 0.5, 0.5
    else:
        w_money /= total
        w_time /= total

    frontier = pareto_frontier(cands)
    if not frontier:
        frontier = cands

    money_norm = normalise([c.money_cost for c in frontier])
    time_norm = normalise([c.detour_min for c in frontier])

    best = frontier[0]
    best_score = float("inf")

    for c, m_n, t_n in zip(frontier, money_norm, time_norm):
        score = w_money * m_n + w_time * t_n
        c.score = round(score, 4)
        c.money_regret = round(m_n, 4)
        c.time_regret = round(t_n, 4)
        if score < best_score:
            best_score = score
            best = c

    return best


EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    )
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def station_within_corridor_2km(st_lat, st_lng, route_points, step=5):
    for (lat, lng) in route_points[0::step]:
        if haversine_km(st_lat, st_lng, lat, lng) <= 2.0:
            return True
    return False


def route_midpoint(points):
    return points[len(points) // 2]


# =========================================================
# MAIN ENGINE
# =========================================================
def run_optimiser(
    origin: str,
    destination: str,
    litres: float,
    fuel_type: str,
    w_money: float = 0.5,
    w_time: float = 0.5,
    l_per_100km: float = 8.0,
    pre_loaded_data: Optional[Dict] = None,  # pre-fetched {stations, prices} — skips NSW API call
) -> Dict[str, Any]:
    """
    w_money + w_time should sum to 1.0, but the function normalises them
    internally so any positive values work.

    l_per_100km is the user's vehicle fuel consumption figure. It affects
    the detour cost component of money_cost so that a Prius owner gets
    different recommendations to a Land Cruiser owner.
    """
    departure_time = (
        dt.datetime.utcnow() + dt.timedelta(minutes=2)
    ).replace(microsecond=0).isoformat() + "Z"

    # ---- Baseline route ----
    base_route_key = ("base", origin, destination, fuel_type.upper(), departure_time)
    if base_route_key in ROUTE_CACHE:
        base_minutes, base_km, route_points = ROUTE_CACHE[base_route_key]
    else:
        base_minutes, base_km, route_points = _unpack_route(
            get_route(origin, destination, departure_time=departure_time)
        )
        ROUTE_CACHE[base_route_key] = (base_minutes, base_km, route_points)

    if route_points is None:
        raise RuntimeError("Route points missing from Google response")

    radius_km = max(2.0, base_km / 2.0)
    mid_lat, mid_lng = route_midpoint(route_points)

    # ---- NSW fuel prices ----
    if pre_loaded_data is not None:
        data = pre_loaded_data
    else:
        nsw_key = (
            round(mid_lat, 4),
            round(mid_lng, 4),
            fuel_type.upper(),
            int(round(radius_km)),
        )
        if nsw_key in NSW_CACHE:
            data = NSW_CACHE[nsw_key]
        else:
            token = get_access_token()
            data = get_nearby_prices(token, mid_lat, mid_lng, fuel_type, radius_km)
            NSW_CACHE[nsw_key] = data

    stations = data.get("stations", [])
    prices = data.get("prices", [])
    lookup = {int(s["code"]): s for s in stations if "code" in s}

    # ---- Pass 1: collect corridor stations (no route calls yet) ----
    seen: set = set()
    corridor: list = []

    for p in prices:
        code = p.get("stationcode")
        if code is None or code in seen:
            continue
        seen.add(code)

        station = lookup.get(int(code))
        if not station:
            continue

        loc = station.get("location", {})
        st_lat = float(loc.get("latitude", 0))
        st_lng = float(loc.get("longitude", 0))

        if not station_within_corridor_2km(st_lat, st_lng, route_points):
            continue

        price = float(p["price"])
        if price > 10:
            price /= 100.0

        corridor.append((code, station, st_lat, st_lng, price))

    # Sort cheapest-first and cap at 10 — expensive stations can't win anyway
    corridor.sort(key=lambda x: x[4])
    corridor = corridor[:10]

    # ---- Pass 2: via-routes fired in parallel ----
    def _fetch_via(item):
        code, station, st_lat, st_lng, price = item
        via_key = (
            "via", origin, destination,
            round(st_lat, 5), round(st_lng, 5),
            fuel_type.upper(), departure_time,
        )
        with _ROUTE_CACHE_LOCK:
            if via_key in ROUTE_CACHE:
                cached = ROUTE_CACHE[via_key]
                return item, cached[0], cached[1]

        via_min, via_km, _ = _unpack_route(
            get_route(
                origin, destination,
                waypoint=(st_lat, st_lng),
                departure_time=departure_time,
            )
        )
        with _ROUTE_CACHE_LOCK:
            ROUTE_CACHE[via_key] = (via_min, via_km, _)
        return item, via_min, via_km

    candidates: List[Candidate] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_via, item) for item in corridor]
        for fut in as_completed(futures):
            try:
                item, via_min, via_km = fut.result()
            except Exception as exc:
                log.warning("Via route failed: %s", exc)
                continue

            code, station, st_lat, st_lng, price = item
            detour_min = max(0.0, via_min - base_minutes)
            detour_km_val = max(0.0, via_km - base_km)
            money = compute_money_cost(price, litres, detour_km_val, l_per_100km)
            brand = station.get("brand", "Unknown")

            candidates.append(
                Candidate(
                    name=station["name"],
                    brand=brand,
                    price_per_l=price,
                    detour_km=via_km - base_km,
                    detour_min=detour_min,
                    money_cost=money,
                    lat=st_lat,
                    lng=st_lng,
                    station_code=str(code),
                )
            )

    if not candidates:
        raise RuntimeError("No viable stations found")

    cheapest = pick_cheapest(candidates)
    fastest = pick_fastest(candidates)
    balanced = pick_balanced(candidates, w_money=w_money, w_time=w_time)

    def pack(c: Candidate) -> Dict[str, Any]:
        return {
            "name": c.name,
            "brand": c.brand,
            "stationCode": c.station_code,
            "price": round(c.price_per_l, 3),
            "detourKm": round(c.detour_km, 2),
            "detourMin": round(c.detour_min, 1),
            "moneyCost": round(c.money_cost, 2),
            "score": round(c.score, 4),
            "moneyRegret": round(c.money_regret, 4),
            "timeRegret": round(c.time_regret, 4),
            "lat": c.lat,
            "lng": c.lng,
        }

    return {
        "baseline": {
            "km": round(base_km, 2),
            "minutes": round(base_minutes, 1),
        },
        "weights": {
            "money": round(w_money / (w_money + w_time), 3),
            "time": round(w_time / (w_money + w_time), 3),
        },
        "cheapest": pack(cheapest),
        "fastest": pack(fastest),
        "balanced": pack(balanced),
        "candidateCount": len(candidates),
    }


# =========================================================
# CLI
# =========================================================
def main():
    print("Fuel Optimiser 🚗\n")
    origin = input("Start: ")
    destination = input("Destination: ")
    litres = float(input("Litres to buy: "))
    fuel_type = input("Fuel type (E10/U91/P95/P98/Diesel): ").upper()
    l_per_100km = float(input("Fuel consumption (L/100km) [default 8]: ") or "8")
    result = run_optimiser(origin, destination, litres, fuel_type, l_per_100km=l_per_100km)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()