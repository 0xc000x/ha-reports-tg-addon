"""Опрашивает stopice.net и antifreeze.app и шлёт новые репорты в Telegram."""
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:
    PACIFIC = None
LOCAL_TZ = PACIFIC or timezone(timedelta(hours=-8))  # время репортов stopice — тихоокеанское

VERSION = "1.6.0"
USER_AGENT = f"StopICE-Telegram-HomeAssistant-Addon/{VERSION}"
HERE = Path(__file__).resolve().parent
OPTIONS_FILE = Path("/data/options.json")  # настройки add-on'а Home Assistant
STATE_FILE = (Path("/data") if OPTIONS_FILE.exists() else HERE) / "seen.json"
MAX_SEEN = 5000
REQUEST_GAP = 5  # пауза между запросами к stopice, иначе срабатывает rate limit
ANTIFREEZE_URL = "https://antifreeze.app/api/sightings"
WEB_PORT = 8099  # Ingress: карта точек в интерфейсе Home Assistant
CHANGELOG_FILE = HERE / "CHANGELOG.md"
MAX_RECENT = 300  # сколько последних репортов показывать на карте

PRIORITIES = {
    "1": "🔴 ПОДТВЕРЖДЕНО",
    "2": "🟠 ЗАМЕЧЕНО",
    "3": "🟡 НЕ ПОДТВЕРЖДЕНО",
    "4": "⚪ ПРОШЛЫЙ ИНЦИДЕНТ",
    "5": "🔵 ВЗАИМОПОМОЩЬ",
}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
SOURCE_SUFFIX = re.compile(r"\s*-\s*(stopice\.net|iceout\.org)\s*$", re.I)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def load_config():
    if OPTIONS_FILE.exists():
        cfg = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
        if not cfg.get("locations") and "latitude" in cfg:  # настройки версии 1.0
            cfg["locations"] = [{"name": "Основная", "latitude": cfg["latitude"],
                                 "longitude": cfg["longitude"], "distance_miles": cfg["distance_miles"]}]
        cfg.setdefault("antifreeze_enabled", True)
        cfg.setdefault("notify_on_start", True)
        return cfg
    # локальный запуск: переменные окружения или .env рядом / уровнем выше
    env = dict(os.environ)
    for path in (HERE / ".env", HERE.parent / ".env"):
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, sep, value = line.partition("=")
                if sep and not key.strip().startswith("#"):
                    env.setdefault(key.strip(), value.strip())
    return {
        "stopice_api_key": env.get("STOPICE_API_KEY", ""),
        "telegram_bot_token": env.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": env.get("TELEGRAM_CHAT_ID", ""),
        "locations": [{
            "name": env.get("LOCATION_NAME", "La Riviera"),
            "latitude": float(env.get("LATITUDE", 38.59)),
            "longitude": float(env.get("LONGITUDE", -121.38)),
            "distance_miles": int(env.get("DISTANCE_MILES", 20)),
        }],
        "antifreeze_enabled": env.get("ANTIFREEZE_ENABLED", "true").lower() == "true",
        "poll_interval": int(env.get("POLL_INTERVAL", 60)),
        "notify_on_start": env.get("NOTIFY_ON_START", "true").lower() == "true",
    }


def http(url, data=None, timeout=30):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        try:
            raw = resp.read()
        except IncompleteRead as e:
            # stopice иногда обрывает ответ, хотя JSON уже пришёл целиком
            raw = e.partial
        return raw.decode("utf-8", errors="replace")


def location_key(loc):
    return f"{loc['latitude']}|{loc['longitude']}|{loc['distance_miles']}"


def miles_between(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def distance_to(loc, alert):
    try:
        return miles_between(float(loc["latitude"]), float(loc["longitude"]),
                             float(alert["lat"]), float(alert["long"]))
    except (KeyError, TypeError, ValueError):
        return None


def clean_text(text):
    return SOURCE_SUFFIX.sub("", str(text or "").replace("\r\n", "\n")).strip()


# --- stopice.net -------------------------------------------------------------

def fetch_stopice(cfg, loc, attempts=2):
    """Запрос к stopice; при обрыве, таймауте или rate limit — одна повторная попытка."""
    for attempt in range(attempts):
        try:
            return fetch_stopice_once(cfg, loc)
        except Exception as e:
            if attempt + 1 >= attempts:
                raise
            log(f"{loc.get('name') or 'Точка'}: stopice: {e}, повтор через {REQUEST_GAP} с")
            time.sleep(REQUEST_GAP)


def fetch_stopice_once(cfg, loc):
    query = urllib.parse.urlencode({
        "key": cfg["stopice_api_key"],
        "recentalerts": 1,
        "lat": loc["latitude"],
        "long": loc["longitude"],
        "distance": loc["distance_miles"],
    })
    data = json.loads(http("https://stopice.net/api/?" + query), strict=False)
    if "error" in data:
        raise RuntimeError(f"API: {data['error']}")
    alerts = []
    for key, value in data.items():
        if key.startswith("DATASET-ALERT") and isinstance(value, list):
            alerts.extend(normalize_stopice(a) for a in value if isinstance(a, dict) and a.get("id"))
    return alerts  # свежие первыми


def parse_stopice_time(value):
    """'OCT 11, 2026 (14:22:17)' (тихоокеанское время) -> unix-время или None."""
    try:
        dt = datetime.strptime(str(value).strip(), "%b %d, %Y (%H:%M:%S)")
    except ValueError:
        return None
    return dt.replace(tzinfo=LOCAL_TZ).timestamp()


def normalize_stopice(a):
    priority = str(a.get("priority", "")).strip()
    media = [u.strip() for u in str(a.get("media") or "").split(",") if u.strip()]
    images = [u for u in media if u.lower().endswith(IMAGE_EXT)]
    previews = [u for u in images if "/preview." in u]
    # заметки модератора: в API их пока нет, подхватываем любое поле с "moder" в названии
    mod_notes = [str(v).strip() for k, v in a.items() if "moder" in k.lower() and str(v or "").strip()]
    return {
        "uid": str(a["id"]),
        "id": str(a["id"]),
        "source": "stopice.net",
        "status": PRIORITIES.get(priority, f"❔ СТАТУС {priority}"),
        "address": a.get("address"),
        "created": a.get("created"),
        "ts": parse_stopice_time(a.get("created")),
        "description": clean_text(a.get("description")),
        "moderator_notes": "\n".join(mod_notes),
        "lat": a.get("lat"),
        "long": a.get("long"),
        "url": a.get("url"),
        "photo": (previews or images or [None])[0],
    }


# --- antifreeze.app ----------------------------------------------------------

def fetch_antifreeze():
    data = json.loads(http(ANTIFREEZE_URL))
    if not isinstance(data, list):
        raise RuntimeError(f"antifreeze: неожиданный ответ {str(data)[:200]}")
    sightings = []
    for s in data:
        if not isinstance(s, dict) or s.get("id") is None:
            continue
        notes = str(s.get("notes") or "")
        if re.search(r"stopice\.net\s*$", notes, re.I):
            continue  # копия репорта stopice.net — его и так получаем напрямую
        sightings.append(normalize_antifreeze(s, notes))
    sightings.sort(key=lambda s: s["sort"], reverse=True)  # свежие первыми, как у stopice
    return sightings


def parse_iso_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def format_iso_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value
    if PACIFIC:
        return dt.astimezone(PACIFIC).strftime("%b %d, %Y (%H:%M:%S) %Z").upper()
    return dt.strftime("%b %d, %Y (%H:%M:%S) UTC").upper()


def normalize_antifreeze(s, notes):
    via_iceout = bool(re.search(r"iceout\.org\s*$", notes, re.I))
    photos = [p for p in (s.get("photos") or []) if isinstance(p, str)]
    images = [p for p in photos if p.lower().endswith(IMAGE_EXT)]
    return {
        "uid": f"af:{s['id']}",
        "id": str(s["id"]),
        "source": "iceout.org (через antifreeze.app)" if via_iceout else "antifreeze.app",
        "status": "🟣 ICEOUT.ORG" if via_iceout else "🟣 ANTIFREEZE",
        "address": s.get("address"),
        "created": format_iso_time(s.get("createdAt")),
        "sort": str(s.get("createdAt") or ""),
        "ts": parse_iso_time(s.get("createdAt")),
        "description": clean_text(notes),
        "moderator_notes": "",
        "lat": s.get("latitude"),
        "long": s.get("longitude"),
        "url": None,
        "photo": (images or photos or [None])[0],
    }


# --- Telegram ----------------------------------------------------------------

def format_alert(alert, loc=None):
    esc = lambda v: html.escape(str(v or "").strip())
    lines = [
        f"<b>{esc(alert['status'])}</b>",
        f"📍 {esc(alert.get('address'))}",
        f"🕒 {esc(alert.get('created'))}",
    ]
    if loc:
        place = f"📌 {esc(loc.get('name') or 'Точка')}"
        dist = distance_to(loc, alert)
        if dist is not None:
            place += f" — {dist:.1f} mi"
        lines.append(place)
    if alert.get("description"):
        lines += ["", esc(alert["description"])]
    if alert.get("moderator_notes"):
        lines += ["", f"🛡 <b>Moderator Notes:</b> {esc(alert['moderator_notes'])}"]
    links = []
    if alert.get("url"):
        links.append(f'<a href="{esc(alert["url"])}">Репорт</a>')
    if alert.get("lat") and alert.get("long"):
        links.append(f'<a href="https://maps.google.com/?q={esc(alert["lat"])},{esc(alert["long"])}">Карта</a>')
    links.append(f"<i>{esc(alert['source'])}</i>")
    lines += ["", " · ".join(links)]
    return "\n".join(lines)


def telegram(cfg, method, **params):
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/{method}"
    try:
        return json.loads(http(url, params))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {method}: {e.code} {e.read().decode('utf-8', 'replace')}") from None


def send_text(cfg, text):
    telegram(cfg, "sendMessage", chat_id=cfg["telegram_chat_id"], text=text[:4096],
             parse_mode="HTML", disable_web_page_preview="true")


def send_alert(cfg, alert, loc=None):
    text = format_alert(alert, loc)
    photo = alert.get("photo")
    if photo and len(text) <= 1024:
        try:
            telegram(cfg, "sendPhoto", chat_id=cfg["telegram_chat_id"], photo=photo,
                     caption=text, parse_mode="HTML")
            return
        except Exception as e:
            log(f"Фото не отправилось ({e}), шлю текстом")
    send_text(cfg, text)


# --- состояние и цикл ----------------------------------------------------------

def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if isinstance(state, list):  # формат версии 1.0
        state = {"seen": state}
    state.setdefault("seen", [])
    state.setdefault("locations", [])
    state.setdefault("af_locations", [])
    return state


def save_state(state):
    state["seen"] = state["seen"][-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def process(cfg, state, loc, alerts, known_key):
    """Отправляет новые репорты точки; для новой точки молча запоминает текущие."""
    name = loc.get("name") or "Точка"
    key = location_key(loc)
    seen = set(state["seen"])
    new = [a for a in alerts if a["uid"] not in seen]
    if key not in state[known_key]:
        state["seen"] += [a["uid"] for a in new]
        state[known_key].append(key)
        save_state(state)
        if new:
            log(f"{name}: новая точка, молча запомнил {len(new)} репортов ({alerts[0]['source'].split()[0]})")
        return
    for alert in reversed(new):  # отправляем от старых к новым
        send_alert(cfg, alert, loc)
        state["seen"].append(alert["uid"])
        save_state(state)
        log(f"{name}: отправлен репорт {alert['uid']}: {alert.get('address')}")


def remember(recent, alerts):
    for a in alerts:
        recent[a["uid"]] = {k: a.get(k) for k in ("uid", "source", "status", "address", "created", "ts",
                                             "description", "lat", "long", "url")}
        recent.move_to_end(a["uid"])
    while len(recent) > MAX_RECENT:
        recent.popitem(last=False)


def poll_once(cfg, state, locations, recent):
    counts = []
    for n, loc in enumerate(locations):
        if n:
            time.sleep(REQUEST_GAP)
        name = loc.get("name") or "Точка"
        try:
            alerts = fetch_stopice(cfg, loc)
            counts.append(f"{name}: {len(alerts)}")
            remember(recent, reversed(alerts))
            process(cfg, state, loc, alerts, "locations")
        except Exception as e:
            counts.append(f"{name}: ошибка")
            log(f"{name}: stopice: ошибка: {e}")

    if cfg.get("antifreeze_enabled"):
        try:
            sightings = fetch_antifreeze()
            matched = 0
            for loc in locations:
                near = [s for s in sightings
                        if (d := distance_to(loc, s)) is not None and d <= float(loc["distance_miles"])]
                matched += len(near)
                remember(recent, reversed(near))
                process(cfg, state, loc, near, "af_locations")
            counts.append(f"antifreeze: {matched} в зонах из {len(sightings)}")
        except Exception as e:
            counts.append("antifreeze: ошибка")
            log(f"antifreeze: ошибка: {e}")
    return counts


def changelog_sections():
    """[(версия, [пункты]), ...] из CHANGELOG.md, свежие первыми."""
    try:
        text = CHANGELOG_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    sections = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections and line.strip().startswith("- "):
            sections[-1][1].append(line.strip()[2:])
    return sections


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3])


def release_notes(last_version):
    """Текст о новой версии: все пункты changelog новее last_version (или только текущей)."""
    sections = changelog_sections()
    if last_version:
        sections = [s for s in sections if version_tuple(s[0]) > version_tuple(last_version)]
    else:
        sections = [s for s in sections if s[0] == VERSION]
    if not sections:
        return None
    to_html = lambda t: re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(t, quote=False))
    lines = [f"🆕 <b>StopICE-бот обновлён до v{VERSION}</b>"]
    for version, items in sections[:5]:
        lines.append("")
        if len(sections) > 1:
            lines.append(f"<b>{html.escape(version)}</b>")
        lines += [f"• {to_html(i)}" for i in items]
    return "\n".join(lines)


def announce_update(cfg, state):
    last = state.get("last_version")
    if last == VERSION:
        return
    notes = release_notes(last)
    if notes:
        try:
            send_text(cfg, notes)
            log(f"Release notes v{VERSION} отправлены в Telegram")
        except Exception as e:
            log(f"Не удалось отправить release notes: {e}")
            return  # попробуем при следующем запуске
    state["last_version"] = VERSION
    save_state(state)


def main():
    cfg = load_config()
    missing = [k for k in ("stopice_api_key", "telegram_bot_token", "telegram_chat_id", "locations")
               if not cfg.get(k)]
    if missing:
        log("Не заполнены настройки: " + ", ".join(missing))
        sys.exit(1)

    runtime = {
        "cfg": cfg,
        "options": json.loads(OPTIONS_FILE.read_text(encoding="utf-8")) if OPTIONS_FILE.exists() else {},
        "recent": OrderedDict(),
        "user_agent": USER_AGENT,
        "tz": LOCAL_TZ,
    }
    locations = cfg["locations"]
    interval = max(30, int(cfg["poll_interval"]))
    state = load_state()
    log(f"Старт v{VERSION}: точек {len(locations)}, опрос каждые {interval} с, "
        f"antifreeze.app {'включён' if cfg.get('antifreeze_enabled') else 'выключен'}")
    for loc in locations:
        log(f"  {loc.get('name') or 'Точка'}: {loc['latitude']}, {loc['longitude']}, "
            f"радиус {loc['distance_miles']} миль")

    if "--test" in sys.argv:  # отправить самый свежий репорт каждого источника и выйти
        loc = locations[0]
        alerts = fetch_stopice(cfg, loc)
        log(f"stopice: {len(alerts)}")
        if alerts:
            send_alert(cfg, alerts[0], loc)
        sightings = fetch_antifreeze()
        log(f"antifreeze: {len(sightings)} (без копий stopice)")
        if sightings:
            send_alert(cfg, sightings[0], min(locations, key=lambda l: distance_to(l, sightings[0]) or 1e9))
        return

    if os.environ.get("SUPERVISOR_TOKEN") or "--ui" in sys.argv:
        try:
            import web
            web.start(runtime, WEB_PORT, log)
        except Exception as e:
            log(f"Карта точек не запустилась: {e}")

    announce_update(cfg, state)
    started = False
    while True:
        cycle_start = time.monotonic()
        locations = cfg["locations"]  # могли поменяться через карту
        counts = poll_once(cfg, state, locations, runtime["recent"])
        try:
            import web
            if web.publish_public(runtime, state, bool(cfg.get("public_page")), log):
                save_state(state)
        except Exception as e:
            log(f"Публичная карта: ошибка: {e}")
        if not started:
            started = True
            log("Опрос работает — " + "; ".join(counts))
            if cfg.get("notify_on_start"):
                try:
                    send_text(cfg, f"✅ StopICE-бот v{VERSION} запущен. Точек: {len(locations)}\n"
                              + html.escape("\n".join(counts)))
                    log("Сообщение о запуске отправлено в Telegram")
                except Exception as e:
                    log(f"Не удалось отправить сообщение о запуске: {e}")
        time.sleep(max(5, interval - (time.monotonic() - cycle_start)))


if __name__ == "__main__":
    main()
