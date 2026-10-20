"""Веб-интерфейс add-on'а (Ingress): карта для выбора точек и радиусов."""
import base64
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import OrderedDict
from datetime import datetime, time as dtime
from pathlib import Path

INDEX = Path(__file__).with_name("index.html")  # редактор точек (боковая панель HA)
VIEW = Path(__file__).with_name("view.html")  # карта только для просмотра (прямой доступ по порту)
INGRESS_PROXY = "172.30.32.2"  # единственный адрес, с которого Supervisor ходит в Ingress
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_PATH = re.compile(r"^/tiles/(\d{1,2})/(\d{1,7})/(\d{1,7})\.png$")
TILE_CACHE_SIZE = 800  # ~10–20 МБ памяти
_tile_cache = OrderedDict()
_tile_lock = threading.Lock()


def get_tile(z, x, y, user_agent):
    """Тайл OpenStreetMap через сервер add-on'а: со своим User-Agent и кэшем, как требует политика OSM."""
    if z > 19 or x >= 2 ** z or y >= 2 ** z:
        raise ValueError("bad tile")
    key = (z, x, y)
    with _tile_lock:
        if key in _tile_cache:
            _tile_cache.move_to_end(key)
            return _tile_cache[key]
    req = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    with _tile_lock:
        _tile_cache[key] = data
        while len(_tile_cache) > TILE_CACHE_SIZE:
            _tile_cache.popitem(last=False)
    return data


def validate_locations(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError("нужна хотя бы одна точка")
    result = []
    for n, loc in enumerate(raw, 1):
        try:
            name = str(loc.get("name") or "").strip()[:80]
            lat = round(float(loc["latitude"]), 6)
            lon = round(float(loc["longitude"]), 6)
            dist = int(round(float(loc["distance_miles"])))
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError(f"точка {n}: неверные данные") from None
        if not name:
            raise ValueError(f"точка {n}: пустое название")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"{name}: координаты вне диапазона")
        if not 1 <= dist <= 200:
            raise ValueError(f"{name}: радиус должен быть от 1 до 200 миль")
        result.append({"name": name, "latitude": lat, "longitude": lon, "distance_miles": dist})
    return result


def geocode(query, user_agent):
    """Поиск адреса через Nominatim с сервера add-on'а (со своим User-Agent, как требует их политика)."""
    query = query.strip()[:200]
    if not query:
        return []
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"format": "json", "limit": 1, "countrycodes": "us", "q": query})
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return [{"lat": r["lat"], "lon": r["lon"], "name": r.get("display_name")}
                for r in json.loads(resp.read())]


def today_reports(runtime):
    """Репорты за сегодня, с полуночи до полуночи по тихоокеанскому времени."""
    tz = runtime.get("tz")
    start = datetime.combine(datetime.now(tz).date(), dtime.min, tzinfo=tz).timestamp()
    end = start + 24 * 3600
    return [r for r in runtime["recent"].values() if r.get("ts") and start <= r["ts"] < end]


PUBLIC_LOADER = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICE Monitor — карта</title></head>
<body style="margin:0;font:14px system-ui,sans-serif">
<script>
fetch("page.html?t=" + Date.now(), { cache: "no-store" })
  .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
  .then(function (html) { document.open(); document.write(html); document.close(); })
  .catch(function () { document.body.textContent = "Карта временно недоступна"; });
</script>
</body></html>
"""
PUBLIC_ROOT = Path("/homeassistant/www")  # отдаётся Home Assistant без входа по адресу /local/...


def publish_public(runtime, state, enabled, log):
    """Публичная страница просмотра в /config/www: открывается без входа, в том числе через Home Assistant Cloud."""
    token = state.get("public_token")
    if not enabled:
        if token and (PUBLIC_ROOT / f"stopice_{token}").exists():
            shutil.rmtree(PUBLIC_ROOT / f"stopice_{token}", ignore_errors=True)
            log("Публичная карта выключена, файлы удалены")
        return False
    if not PUBLIC_ROOT.parent.exists():
        return False  # нет доступа к папке конфигурации Home Assistant (локальный запуск)
    first = not token
    if first:
        # случайное имя папки, чтобы адрес нельзя было угадать
        token = state["public_token"] = secrets.token_urlsafe(9)
    folder = PUBLIC_ROOT / f"stopice_{token}"
    folder.mkdir(parents=True, exist_ok=True)
    page = VIEW.read_text(encoding="utf-8").replace("const DATA_FILE = null;", 'const DATA_FILE = "data.json";')
    (folder / "page.html").write_text(page, encoding="utf-8")
    # Home Assistant отдаёт /local с кэшем на месяц, поэтому index.html — неизменный загрузчик,
    # который всегда берёт свежую page.html; сама карта обновляется вместе с add-on'ом.
    if not (folder / "index.html").exists() or (folder / "index.html").read_text(encoding="utf-8") != PUBLIC_LOADER:
        (folder / "index.html").write_text(PUBLIC_LOADER, encoding="utf-8")
    data = {"locations": runtime["cfg"]["locations"], "reports": today_reports(runtime),
            "generated": datetime.now(runtime.get("tz")).isoformat()}
    tmp = folder / "data.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / "data.json")
    if first or not runtime.get("public_logged"):
        runtime["public_logged"] = True
        log(f"Публичная карта: {public_url(runtime, state)}")
    return first


def ha_external_url():
    """Внешний адрес Home Assistant из его настроек (Настройки → Система → Сеть), если он задан."""
    if not SUPERVISOR_TOKEN:
        return None
    req = urllib.request.Request("http://supervisor/core/api/config",
                                 headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("external_url")
    except Exception:
        return None


def public_url(runtime, state):
    """Полная ссылка на публичную карту: адрес из настроек add-on'а или внешний адрес Home Assistant."""
    token = state.get("public_token")
    if not token:
        return None
    if "base_url" not in runtime:
        runtime["base_url"] = (str(runtime["cfg"].get("public_base_url") or "").strip()
                               or ha_external_url() or "")
    base = runtime["base_url"].rstrip("/") or "<адрес Home Assistant>"
    return f"{base}/local/stopice_{token}/index.html"


def save_locations(runtime, locations):
    """Сохраняет точки в настройки add-on'а через Supervisor (переживут перезапуск)."""
    if not SUPERVISOR_TOKEN:  # локальный запуск без Home Assistant
        Path(__file__).with_name("locations_local.json").write_text(
            json.dumps(locations, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    options = dict(runtime["options"])
    options["locations"] = locations
    req = urllib.request.Request(
        "http://supervisor/addons/self/options",
        data=json.dumps({"options": options}).encode(),
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("message")
        except ValueError:
            message = None
        raise RuntimeError(f"Supervisor отклонил настройки: {message or e.code}") from None
    runtime["options"] = options


def start(runtime, port, log):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def is_admin(self):
            # редактировать точки можно только через Ingress (боковую панель Home Assistant)
            return not SUPERVISOR_TOKEN or self.client_address[0] == INGRESS_PROXY

        def allowed(self):
            # через Ingress пользователь уже авторизован в Home Assistant
            if not SUPERVISOR_TOKEN or self.client_address[0] == INGRESS_PROXY:
                return True
            # прямой доступ по порту из сети: пароль, если он задан в настройках
            password = str(runtime["cfg"].get("web_password") or "")
            if not password:
                return True
            header = self.headers.get("Authorization") or ""
            try:
                given = base64.b64decode(header[6:]).decode("utf-8").partition(":")[2]                     if header.startswith("Basic ") else ""
            except ValueError:
                given = ""
            if hmac.compare_digest(given.encode(), password.encode()):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="ICE Monitor", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.allowed():
                return
            path = self.path.split("?")[0]
            tile = TILE_PATH.match(path)
            if tile:
                try:
                    data = get_tile(*map(int, tile.groups()), runtime.get("user_agent", "ICE-Monitor"))
                    self.send(200, data, "image/png", "public, max-age=604800")
                except Exception:
                    self.send(502, b"", "image/png")
            elif path in ("/", "/index.html"):
                page = INDEX if self.is_admin() else VIEW
                self.send(200, page.read_bytes(), "text/html; charset=utf-8")
            elif path == "/view":
                self.send(200, VIEW.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/locations":
                self.send(200, {"locations": runtime["cfg"]["locations"]})
            elif path == "/api/geocode" and self.is_admin():
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
                try:
                    self.send(200, geocode(query, runtime.get("user_agent", "ICE-Monitor")))
                except Exception as e:
                    self.send(502, {"error": f"поиск не удался: {e}"})
            elif path == "/api/reports":
                today = "today=1" in self.path or not self.is_admin()
                reports = today_reports(runtime) if today else list(runtime["recent"].values())
                self.send(200, {"reports": reports})
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.allowed():
                return
            if self.path.split("?")[0] != "/api/locations":
                self.send(404, {"error": "not found"})
                return
            if not self.is_admin():
                self.send(403, {"error": "точки можно менять только через Home Assistant"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                locations = validate_locations(body.get("locations"))
                save_locations(runtime, locations)
            except Exception as e:
                self.send(400, {"error": str(e)})
                return
            runtime["cfg"]["locations"] = locations
            log(f"Точки изменены через карту: {len(locations)} шт., применятся в следующем цикле")
            self.send(200, {"ok": True, "locations": locations})

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Карта точек доступна на порту {port}")
    return server
