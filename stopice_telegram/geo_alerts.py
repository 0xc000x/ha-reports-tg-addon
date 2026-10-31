"""Пуш-уведомления по геопозиции: репорты рядом с членами семьи.

Положение берётся из Home Assistant (person.*, его обновляет приложение Home Assistant на телефоне),
пуш уходит через службу notify.mobile_app_* того же приложения. Уровни тревоги — по расстоянию;
о том же случае повторно сообщаем, только если человек оказался ближе (уровень вырос).
"""
import json
import math
import os
import time
import urllib.request

API = "http://supervisor/core/api"
RADIUS_MILES = 15
LEVELS = [  # (миль, заголовок)
    (1, "ПИЗДЕЦ! Шакалы в радиусе 1 мили"),
    (3, "ОСТОРОЖНО! Шакалы в радиусе 3 миль"),
    (10, "Шакалы в радиусе 10 миль"),
    (15, "Шакалы в радиусе 15 миль"),
]
SAME_PLACE_MILES = 0.25  # один случай из разных источников — одно уведомление
SAME_TIME_SECONDS = 3 * 3600
KEEP_SECONDS = 36 * 3600


def _request(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN')}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    return json.loads(data) if data else None


def miles_between(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def level_for(miles):
    for radius, title in LEVELS:
        if miles <= radius:
            return radius, title
    return None, None


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def age_text(ts, now=None):
    minutes = max(0, int(((now or time.time()) - ts) // 60))
    if minutes < 1:
        return "Опубликовано только что"
    if minutes < 60:
        return f"Опубликовано {minutes} {plural(minutes, 'минуту', 'минуты', 'минут')} назад"
    hours = minutes // 60
    return f"Опубликовано {hours} {plural(hours, 'час', 'часа', 'часов')} назад"


def person_location(entity_id):
    s = _request("GET", f"/states/{entity_id}")
    attrs = (s or {}).get("attributes", {})
    try:
        return float(attrs["latitude"]), float(attrs["longitude"]), attrs.get("friendly_name") or entity_id
    except (KeyError, TypeError, ValueError):
        return None


def notify(service, title, message, data):
    service = service.split(".", 1)[1] if service.startswith("notify.") else service
    _request("POST", f"/services/notify/{service}", {"title": title, "message": message, "data": data})


class GeoAlerts:
    def __init__(self, log):
        self.log = log

    def check(self, people, reports, state, is_relevant, map_url=None):
        """people — [{person, notify}], reports — свежие репорты из всех источников."""
        if not os.environ.get("SUPERVISOR_TOKEN") or not people:
            return
        now = time.time()
        alerted_all = state.setdefault("geo_alerted", {})
        for p in people:
            entity_id, service = p.get("person"), p.get("notify")
            if not entity_id or not service:
                continue
            try:
                where = person_location(entity_id)
            except Exception as e:
                self.log(f"Уведомления: не удалось получить положение {entity_id}: {e}")
                continue
            if not where:
                continue
            lat, lon, name = where
            key = f"{entity_id}|{service}"  # у одного человека может быть несколько телефонов
            alerted = [a for a in alerted_all.get(key, []) if now - a["ts"] < KEEP_SECONDS]

            candidates = []
            for r in reports:
                if not is_relevant(r):
                    continue
                try:
                    rlat, rlon, rts = float(r["lat"]), float(r["long"]), float(r["ts"])
                except (KeyError, TypeError, ValueError):
                    continue
                miles = miles_between(lat, lon, rlat, rlon)
                radius, title = level_for(miles)
                if radius:
                    candidates.append((miles, radius, title, r, rlat, rlon, rts))

            for miles, radius, title, r, rlat, rlon, rts in sorted(candidates, key=lambda c: c[0]):
                # тот же случай (из этого или другого источника) уже присылали — на этом уровне или ближе?
                same = [a for a in alerted
                        if miles_between(a["lat"], a["lon"], rlat, rlon) <= SAME_PLACE_MILES
                        and abs(a["ts"] - rts) <= SAME_TIME_SECONDS]
                if any(a["level"] <= radius for a in same):
                    continue
                status = str(r.get("status") or "").strip()
                message = "\n".join(x for x in (
                    f"{status} · {miles:.1f} mi от вас".strip(" ·"),
                    str(r.get("address") or ""),
                    age_text(rts, now),
                ) if x)
                actions = [{"action": "URI", "title": "Локация", "uri": f"https://maps.google.com/?q={rlat},{rlon}"}]
                if map_url:
                    actions.append({"action": "URI", "title": "Общая карта", "uri": map_url})
                data = {"tag": f"ice_{r.get('uid')}", "ttl": 0, "priority": "high",
                        "channel": "ICE Monitor", "importance": "high", "actions": actions,
                        "clickAction": map_url or f"https://maps.google.com/?q={rlat},{rlon}",
                        "push": {"interruption-level": "time-sensitive"}}
                if radius == 1:
                    data["channel"] = "alarm_stream"  # Android: звучит как будильник, даже в беззвучном режиме
                    data["push"] = {"interruption-level": "critical"}
                try:
                    notify(service, title, message, data)
                except Exception as e:
                    self.log(f"Уведомления: не удалось отправить {service}: {e}")
                    continue
                alerted = [a for a in alerted if a not in same]
                alerted.append({"lat": rlat, "lon": rlon, "ts": rts, "level": radius})
                self.log(f"Уведомление {name}: {title} — {r.get('address')} ({miles:.1f} mi)")
            alerted_all[key] = alerted[-300:]
