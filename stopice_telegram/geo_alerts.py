"""Пуш-уведомления по геопозиции: репорты рядом с членами семьи.

Положение берётся из Home Assistant (person.*, его обновляет приложение Home Assistant на телефоне),
пуш уходит через службу notify.mobile_app_* того же приложения. Уровни тревоги — по расстоянию;
о том же случае повторно сообщаем, только если человек оказался ближе (уровень вырос).
"""
import json
import math
import os
import re
import time
import urllib.request

API = "http://supervisor/core/api"
RADIUS_MILES = 3  # дальше уровни 10 и 15 миль слишком спамили
SPOKEN_LEVEL = 1  # на этом уровне телефон ещё и проговаривает тревогу через громкость будильника
SPOKEN_TEXT = "1 mile"  # коротко, чтобы не говорило долго
LEVELS = [  # (миль, заголовок)
    (1, "ПИЗДЕЦ! Шакалы в радиусе 1 мили"),
    (3, "ОСТОРОЖНО! Шакалы в радиусе 3 миль"),
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


DEVICES_TEMPLATE = (
    "{% for d in integration_entities('mobile_app') | map('device_id') | select | unique %}"
    "{{ device_attr(d, 'name') }}|{{ device_attr(d, 'manufacturer') }};{% endfor %}")


def _slugify(text):
    """Как Home Assistant называет службу notify.mobile_app_<имя устройства>."""
    text = re.sub(r"['’`]", "", str(text).lower())
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def detect_platforms():
    """{служба notify: "ios" | "android"} по производителю телефона в Home Assistant."""
    req = urllib.request.Request(
        API + "/template", method="POST", data=json.dumps({"template": DEVICES_TEMPLATE}).encode(),
        headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN')}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8", "replace")
    platforms = {}
    for line in text.split(";"):
        name, _, manufacturer = line.partition("|")
        if name.strip():
            platforms["mobile_app_" + _slugify(name)] = "ios" if "apple" in manufacturer.lower() else "android"
    return platforms


def notify(service, title, message, data):
    service = service.split(".", 1)[1] if service.startswith("notify.") else service
    _request("POST", f"/services/notify/{service}", {"title": title, "message": message, "data": data})


class GeoAlerts:
    def __init__(self, log, spoken_text=None):
        self.log = log
        self.spoken_text = spoken_text or SPOKEN_TEXT
        self.platforms = None

    def check(self, people, reports, state, is_relevant, map_url=None):
        """people — [{person, notify}], reports — свежие репорты из всех источников."""
        if not os.environ.get("SUPERVISOR_TOKEN") or not people:
            return
        now = time.time()
        alerted_all = state.setdefault("geo_alerted", {})
        if self.platforms is None:
            try:
                self.platforms = detect_platforms()
            except Exception as e:
                self.log(f"Уведомления: не удалось определить, где iPhone, а где Android: {e}")
                self.platforms = {}
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
                loud = p.get("critical", True) is not False  # можно отключить для конкретного телефона
                if radius == 1 and loud:
                    # iPhone: критическое уведомление со звуком на полной громкости (звучит и в беззвучном режиме,
                    # если в приложении Home Assistant разрешены «Критические уведомления»)
                    data["push"] = {"interruption-level": "critical",
                                    "sound": {"name": "default", "critical": 1, "volume": 1.0}}
                try:
                    notify(service, title, message, data)
                except Exception as e:
                    self.log(f"Уведомления: не удалось отправить {service}: {e}")
                    continue
                bare = service.split(".", 1)[1] if service.startswith("notify.") else service
                platform = p.get("platform") or self.platforms.get(bare, "android")
                if loud and radius <= SPOKEN_LEVEL and platform == "android":  # на iPhone голоса нет, там критический пуш
                    # обычное уведомление на Samsung и др. не всегда пробивает беззвучный режим,
                    # а голос через поток будильника — пробивает (если будильники разрешены в «Не беспокоить»)
                    try:
                        notify(service, "", "TTS", {"tts_text": self.spoken_text, "media_stream": "alarm_stream_max",
                                                    "ttl": 0, "priority": "high"})
                    except Exception as e:
                        self.log(f"Уведомления: голосовая тревога на {service} не отправилась: {e}")
                alerted = [a for a in alerted if a not in same]
                alerted.append({"lat": rlat, "lon": rlon, "ts": rts, "level": radius})
                self.log(f"Уведомление {name}: {title} — {r.get('address')} ({miles:.1f} mi)")
            alerted_all[key] = alerted[-300:]
