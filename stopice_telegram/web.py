"""Веб-интерфейс add-on'а (Ingress): карта для выбора точек и радиусов."""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INDEX = Path(__file__).with_name("index.html")
INGRESS_PROXY = "172.30.32.2"  # единственный адрес, с которого Supervisor ходит в Ingress
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")


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

        def allowed(self):
            if SUPERVISOR_TOKEN and self.client_address[0] != INGRESS_PROXY:
                self.send_error(403)
                return False
            return True

        def send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.allowed():
                return
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self.send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/locations":
                self.send(200, {"locations": runtime["cfg"]["locations"]})
            elif path == "/api/reports":
                self.send(200, {"reports": list(runtime["recent"].values())})
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.allowed():
                return
            if self.path.split("?")[0] != "/api/locations":
                self.send(404, {"error": "not found"})
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
