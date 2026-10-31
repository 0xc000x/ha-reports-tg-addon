"""Репорты и зоны на карточке карты Home Assistant (через REST API ядра, без интеграций).

Репорты — сущности geo_location.ice_monitor_* с source «ice_monitor»: карточка карты показывает их все
через geo_location_sources. Зоны — пассивные zone.ice_monitor_N: рисуются кругом с радиусом и,
будучи пассивными, не меняют состояние людей («дома», «на работе») и не мешают автоматизациям.
Состояния не хранятся в HA между его перезапусками, поэтому add-on публикует их каждый цикл заново.
"""
import json
import math
import os
import re
import urllib.error
import urllib.request

API = "http://supervisor/core/api"
SOURCE = "ice_monitor"
REPORT_PREFIX = "geo_location.ice_monitor_"
ZONE_PREFIX = "zone.ice_monitor_"
SAME_PLACE_METERS = 400
SAME_TIME_SECONDS = 3 * 3600
MILE = 1609.344


def _request(method, path, body=None):
    token = os.environ.get("SUPERVISOR_TOKEN")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    return json.loads(data) if data else None


def _meters(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371000 * 2 * math.asin(math.sqrt(a))


def group_reports(reports):
    """Один случай из нескольких источников (до 400 м и 3 часов) — одна метка, как на публичной карте."""
    groups = []
    for r in sorted(reports, key=lambda r: r.get("ts") or 0):
        try:
            lat, lon = float(r["lat"]), float(r["long"])
        except (KeyError, TypeError, ValueError):
            continue
        for g in groups:
            if (_meters(g["lat"], g["lon"], lat, lon) <= SAME_PLACE_METERS
                    and abs((r.get("ts") or 0) - g["ts"]) <= SAME_TIME_SECONDS):
                g["items"].append(r)
                g["ts"] = r.get("ts") or g["ts"]
                break
        else:
            groups.append({"lat": lat, "lon": lon, "ts": r.get("ts") or 0, "items": [r]})
    return groups


def _slug(value):
    return re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_")[:60] or "report"


def _short_address(address):
    return str(address or "").split(",")[0].strip()[:40]


class HaMap:
    def __init__(self, log):
        self.log = log
        self.published = None  # entity_id, опубликованные в прошлый раз; None — ещё не сверялись с HA

    def _existing(self):
        states = _request("GET", "/states") or []
        return {s["entity_id"] for s in states
                if s["entity_id"].startswith((REPORT_PREFIX, ZONE_PREFIX))}

    def sync(self, reports, locations):
        """Публикует сегодняшние репорты и зоны, удаляет то, чего больше нет."""
        if not os.environ.get("SUPERVISOR_TOKEN"):
            return
        if self.published is None:
            self.published = self._existing()  # хвосты прошлого запуска
        current = set()

        for n, loc in enumerate(locations, 1):
            entity_id = f"{ZONE_PREFIX}{n}"
            current.add(entity_id)
            _request("POST", f"/states/{entity_id}", {"state": "0", "attributes": {
                "latitude": float(loc["latitude"]), "longitude": float(loc["longitude"]),
                "radius": float(loc["distance_miles"]) * MILE, "passive": True,
                "icon": "mdi:map-marker-radius", "friendly_name": f"ICE: {loc.get('name') or n}",
            }})

        for g in group_reports(reports):
            newest = g["items"][-1]
            entity_id = REPORT_PREFIX + _slug(newest.get("uid"))
            current.add(entity_id)
            count = len(g["items"])
            name = f"{newest.get('status') or 'Репорт'} · {_short_address(newest.get('address'))}"
            if count > 1:
                name += f" ({count})"
            _request("POST", f"/states/{entity_id}", {"state": newest.get("created") or "", "attributes": {
                "latitude": g["lat"], "longitude": g["lon"], "source": SOURCE,
                "friendly_name": name, "icon": "mdi:alert-octagon",
                "address": newest.get("address"), "status": newest.get("status"),
                "description": newest.get("description"), "url": newest.get("url"),
                "sources": sorted({str(r.get("source")) for r in g["items"]}), "reports": count,
            }})

        for entity_id in self.published - current:
            try:
                _request("DELETE", f"/states/{entity_id}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        self.published = current

    @staticmethod
    def card_yaml(locations):
        lines = ["type: map", "title: ICE Monitor", "geo_location_sources:", f"  - {SOURCE}", "entities:"]
        lines += [f"  - entity: {ZONE_PREFIX}{n}" for n in range(1, len(locations) + 1)]
        return "\n".join(lines)
