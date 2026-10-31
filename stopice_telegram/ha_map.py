"""Уборка за версиями 1.10.x, которые выводили репорты и зоны на карточку карты Home Assistant.

Теперь для этого есть своя панель с картой (panel.html), поэтому при запуске add-on удаляет
оставшиеся сущности geo_location.ice_monitor_* и zone.ice_monitor_* и их значки.
"""
import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

API = "http://supervisor/core/api"
PREFIXES = ("geo_location.ice_monitor_", "zone.ice_monitor_")
ICON_DIR = Path("/homeassistant/www/ice_monitor")


def _request(method, path):
    req = urllib.request.Request(API + path, method=method,
                                 headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN')}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    return json.loads(data) if data else None


def cleanup(log):
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return
    removed = 0
    for s in _request("GET", "/states") or []:
        if s["entity_id"].startswith(PREFIXES):
            try:
                _request("DELETE", f"/states/{s['entity_id']}")
                removed += 1
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
    if ICON_DIR.exists():
        shutil.rmtree(ICON_DIR, ignore_errors=True)
    if removed:
        log(f"Убрал с карты Home Assistant старые метки и зоны ICE Monitor: {removed} шт.")
