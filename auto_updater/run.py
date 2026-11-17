#!/usr/bin/env python3
"""Auto-Updater: installiert nachts alle Updates, bis keine mehr offen sind.

Läuft als Add-on in einem eigenen Container und übersteht daher Neustarts von
Home Assistant Core. Nach einem Neustart des Hosts (OS-Update) wird ein
unterbrochener Lauf anhand von /data/state.json fortgesetzt.
"""
import base64
import json
import logging
import os
import socket
import struct
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
SUPERVISOR = "http://supervisor"
CORE = f"{SUPERVISOR}/core/api"
OPTIONS_FILE = "/data/options.json"
STATE_FILE = "/data/state.json"

CORE_ENTITY = "update.home_assistant_core_update"
OS_ENTITY = "update.home_assistant_operating_system_update"
SUPERVISOR_ENTITY = "update.home_assistant_supervisor_update"

BACKUP_FEATURE = 8          # UpdateEntityFeature.BACKUP
INSTALL_TIMEOUT = 3600      # Firmware (z. B. ESPHome) kann lange dauern
MAX_ATTEMPTS = 2            # Versuche je Update und Nacht
MAX_ROUNDS = 10
SETTLE_SECONDS = 120        # nach Core-Start warten, bis Integrationen geladen sind
OS_REBOOT_WAIT = 600        # nach OS-Update: Zeit, in der der Host neu startet
BACKGROUND_TIMEOUT = 1200   # höchstens so lange auf Updates warten, die im Hintergrund laufen
BACKGROUND_MIN_WAIT = 90    # mindestens so lange auf eine neue Version warten

KINDS = ("addons", "firmware", "hacs", "core", "os")
# Seit 2026.11 ist HACS als "Marketplace" in Home Assistant eingebaut.
# Die Art heißt intern weiter "hacs", damit die Option update_hacs gültig bleibt.
STORE_DOMAINS = ("marketplace", "hacs")

log = logging.getLogger("auto_updater")
opts: dict = {}


# --------------------------------------------------------------------------- API

def request(method, url, data=None, timeout=60):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _ws_send(sock, payload):
    data = json.dumps(payload).encode()
    mask = os.urandom(4)
    header = bytes([0x81])
    if len(data) < 126:
        header += bytes([0x80 | len(data)])
    elif len(data) < 65536:
        header += bytes([0x80 | 126]) + struct.pack("!H", len(data))
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", len(data))
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_recv(sock):
    def read(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("WebSocket geschlossen")
            buf += chunk
        return buf

    first, second = read(2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read(8))[0]
    payload = read(length)
    if first & 0x0F != 0x1:  # nur Textnachrichten sind JSON
        return _ws_recv(sock)
    return json.loads(payload)


def ws_command(command):
    """Führt einen WebSocket-Befehl als Benutzer des Add-ons aus."""
    with socket.create_connection(("supervisor", 80), timeout=30) as sock:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                "GET /core/websocket HTTP/1.1\r\nHost: supervisor\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(1024)
            if not chunk:
                raise OSError("Keine Antwort beim WebSocket-Aufbau")
            response += chunk
        status = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise OSError(status.decode())
        _ws_recv(sock)  # auth_required
        _ws_send(sock, {"type": "auth", "access_token": TOKEN})
        if _ws_recv(sock).get("type") != "auth_ok":
            raise OSError("WebSocket-Anmeldung fehlgeschlagen")
        _ws_send(sock, {"id": 1, **command})
        while True:
            msg = _ws_recv(sock)
            if msg.get("id") == 1 and msg.get("type") == "result":
                return msg


def accept_marketplace_warning():
    """Bestätigt den Marketplace-Hinweis für den Systembenutzer dieses Add-ons.

    Seit 2026.11 gilt die Bestätigung pro Benutzer. Nur auf ausdrücklichen Wunsch
    (Option marketplace_warning_accepted).
    """
    try:
        result = ws_command({"type": "marketplace/warning/accept"})
    except (OSError, ValueError) as err:
        log.error("Marketplace-Hinweis konnte nicht bestätigt werden: %s", err)
        return
    if result.get("success"):
        log.info("Marketplace-Hinweis für das Add-on bestätigt")
    elif result.get("error", {}).get("code") != "unknown_command":  # ohne Marketplace (vor 2026.11)
        log.error("Marketplace-Hinweis konnte nicht bestätigt werden: %s", result.get("error"))


def call_service(domain, service, data, timeout=60):
    return request("POST", f"{CORE}/services/{domain}/{service}", data, timeout)


def template(tpl):
    return request("POST", f"{CORE}/template", {"template": tpl})


def get_state(entity_id):
    try:
        return request("GET", f"{CORE}/states/{entity_id}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


_last_core_error = None


def core_up():
    global _last_core_error
    try:
        request("GET", f"{CORE}/", timeout=10)
        _last_core_error = None
        return True
    except (urllib.error.URLError, OSError, ValueError) as err:
        if str(err) != _last_core_error:
            log.info("Home Assistant noch nicht erreichbar: %s", err)
            _last_core_error = str(err)
        return False


def hacs_entities():
    """Update-Entitäten aus dem Marketplace (früher HACS)."""
    domains = " + ".join(f"integration_entities('{d}')" for d in STORE_DOMAINS)
    try:
        result = template(f"{{{{ ({domains}) | select('match', 'update[.]') | unique | list | tojson }}}}")
    except (urllib.error.URLError, OSError, ValueError):
        return set()
    return set(result) if isinstance(result, list) else set()


def wait_for_core(down_first=False):
    """Wartet, bis Core (wieder) läuft und die Integrationen geladen sind."""
    if down_first:
        end = time.time() + 180
        while time.time() < end and core_up():
            time.sleep(5)
    end = time.time() + 3600
    while not core_up():
        if time.time() > end:
            raise RuntimeError("Home Assistant ist nach einer Stunde noch nicht erreichbar")
        time.sleep(10)
    time.sleep(SETTLE_SECONDS)
    if opts.get("update_hacs"):
        end = time.time() + 300
        while not hacs_entities() and time.time() < end:
            time.sleep(15)


def notify(title, message):
    service = (opts.get("notify_service") or "persistent_notification.create").strip()
    domain, _, name = service.partition(".")
    try:
        call_service(domain, name, {"title": title, "message": message})
    except (urllib.error.URLError, OSError, ValueError) as err:
        log.error("Push über %s fehlgeschlagen: %s", service, err)


# ------------------------------------------------------------------------ Zustand

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def next_time(hhmm, tz, after=None):
    now = after or datetime.now(tz)
    hour, minute = map(int, hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# ------------------------------------------------------------------------ Updates

def pending(skip, self_name):
    """Offene Updates, sortiert nach Art."""
    states = request("GET", f"{CORE}/states")
    hacs = hacs_entities()
    hassio = set(template("{{ integration_entities('hassio') | select('match', 'update[.]') | list | tojson }}") or [])
    exclude = set(opts.get("exclude") or [])
    groups = {kind: [] for kind in KINDS}
    for s in states:
        eid = s["entity_id"]
        if not eid.startswith("update.") or s["state"] != "on":
            continue
        if eid in skip or eid in exclude or eid == SUPERVISOR_ENTITY:
            continue
        if s["attributes"].get("in_progress"):
            continue  # läuft schon, z. B. Firmware im Hintergrund
        if eid == CORE_ENTITY:
            kind = "core"
        elif eid == OS_ENTITY:
            kind = "os"
        elif eid in hacs:
            kind = "hacs"
        elif eid in hassio:
            if s["attributes"].get("title") == self_name:
                continue  # sich selbst nicht aktualisieren, das bricht den Lauf ab
            kind = "addons"
        else:
            kind = "firmware"
        if opts.get(f"update_{kind}"):
            groups[kind].append(eid)
    return groups


def describe(s):
    attrs = s["attributes"]
    name = attrs.get("title") or attrs.get("friendly_name") or s["entity_id"]
    return f"{name} {attrs.get('installed_version')} → {attrs.get('latest_version')}"


def done(s, target):
    return bool(s) and (s["state"] == "off" or s["attributes"].get("installed_version") == target)


def wait_until_done(eid, target):
    """Wartet, bis das Update fertig ist.

    Manche Geräte (z. B. Shelly) starten das Update nur und installieren im
    Hintergrund weiter; danach sind sie kurz nicht erreichbar.
    """
    start = time.time()
    s = get_state(eid)
    while not done(s, target) and time.time() - start < BACKGROUND_TIMEOUT:
        busy = not s or s["state"] in ("unavailable", "unknown") or s["attributes"].get("in_progress")
        if not busy and time.time() - start >= BACKGROUND_MIN_WAIT:
            break
        time.sleep(15)
        s = get_state(eid)
    return s


def install(st, eid, kind):
    s = get_state(eid)
    if not s or s["state"] != "on":
        return False
    text = describe(s)
    target = s["attributes"].get("latest_version")
    data = {"entity_id": eid}
    if (
        kind in ("core", "addons")
        and opts.get(f"backup_{kind}")
        and s["attributes"].get("supported_features", 0) & BACKUP_FEATURE
    ):
        data["backup"] = True

    st["attempts"][eid] = st["attempts"].get(eid, 0) + 1
    save_state(st)
    log.info("Installiere %s%s", text, " (mit Backup)" if data.get("backup") else "")

    error = None
    rejected = False
    try:
        call_service("update", "install", data, timeout=INSTALL_TIMEOUT)
    except urllib.error.HTTPError as err:
        # HA hat das Update abgelehnt; der Grund steht in der Antwort.
        error = error_message(err)
        rejected = True
    except (urllib.error.URLError, OSError, ValueError) as err:
        # Bei Core/OS normal: die Verbindung bricht beim Neustart ab.
        error = getattr(err, "reason", None) or str(err)

    if kind == "core":
        wait_for_core(down_first=True)
    elif kind == "os":
        time.sleep(OS_REBOOT_WAIT)  # startet der Host neu, endet der Prozess hier
        wait_for_core()

    s = get_state(eid) if rejected and kind not in ("core", "os") else wait_until_done(eid, target)
    ok = done(s, target)
    if ok:
        log.info("Erfolgreich: %s", text)
        st["installed"].append(text)
        st["failed"].pop(eid, None)
    else:
        log.error("Fehlgeschlagen: %s%s", text, f" ({error})" if error else "")
        st["failed"][eid] = text + (f" ({error})" if error and kind not in ("core", "os") else "")
    save_state(st)
    return ok


def error_message(err):
    """Fehlermeldung von Home Assistant aus einer HTTP-Fehlerantwort."""
    try:
        body = err.read().decode()
    except (OSError, ValueError):
        body = ""
    try:
        message = json.loads(body).get("message")
    except (ValueError, AttributeError):
        message = body.strip()
    if message and "Marketplace" in message and "warning" in message:
        return "Marketplace-Hinweis für das Add-on nicht bestätigt: Option „Marketplace-Hinweis bestätigt“ einschalten"
    return (message or str(err))[:200]


def restart_core():
    log.info("Starte Home Assistant neu, damit Marketplace-Updates aktiv werden")
    try:
        call_service("homeassistant", "restart", {}, timeout=30)
    except (urllib.error.URLError, OSError, ValueError):
        pass
    wait_for_core(down_first=True)


def finish(st):
    failed = list(st.get("failed", {}).values())
    installed = st.get("installed", [])
    log.info("Lauf beendet: %d installiert, %d fehlgeschlagen", len(installed), len(failed))
    if failed:
        notify("⚠️ Updates fehlgeschlagen", "Nicht installiert:\n" + "\n".join(f"• {f}" for f in failed))
    if installed and opts.get("notify_success"):
        notify("✅ Updates installiert", "\n".join(f"• {i}" for i in installed))
    st["running"] = False
    save_state(st)


def run(st, tz, self_name):
    if not st.get("running"):
        st.clear()
        st.update(
            running=True,
            deadline=next_time(opts["window_end"], tz).isoformat(),
            attempts={},
            installed=[],
            failed={},
        )
        save_state(st)
        log.info("Starte Update-Lauf (Ende spätestens %s)", st["deadline"])
    else:
        log.info("Setze unterbrochenen Update-Lauf fort")

    deadline = datetime.fromisoformat(st["deadline"])
    try:
        for _ in range(MAX_ROUNDS):
            if datetime.now(tz) >= deadline:
                log.warning("Zeitfenster vorbei, restliche Updates in der nächsten Nacht")
                break
            skip = {e for e, n in st["attempts"].items() if n >= MAX_ATTEMPTS}
            todo = pending(skip, self_name)
            log.info("Offen: %s", {k: v for k, v in todo.items() if v} or "nichts")

            if opts.get("dry_run"):
                lines = [describe(s) for k in KINDS for e in todo[k] if (s := get_state(e))]
                notify(
                    "🧪 Auto-Updater Testlauf",
                    "Würde installieren:\n" + "\n".join(f"• {l}" for l in lines) if lines else "Keine offenen Updates.",
                )
                st["running"] = False
                save_state(st)
                return

            if not any(todo.values()):
                break

            restart_needed = False
            for kind in ("addons", "firmware", "hacs"):
                for eid in todo[kind]:
                    if install(st, eid, kind) and kind == "hacs":
                        restart_needed = True

            core_ok = install(st, CORE_ENTITY, "core") if todo["core"] else False
            if todo["os"] and (get_state(OS_ENTITY) or {}).get("state") == "on":
                install(st, OS_ENTITY, "os")
            elif restart_needed and not core_ok:
                restart_core()
    except Exception as err:  # noqa: BLE001 - Lauf darf den Scheduler nicht beenden
        log.exception("Update-Lauf abgebrochen")
        st.setdefault("failed", {})["_lauf"] = f"Lauf abgebrochen: {err}"
    finish(st)


# --------------------------------------------------------------------------- Main

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    global opts
    with open(OPTIONS_FILE, encoding="utf-8") as f:
        opts = json.load(f)

    if not TOKEN:
        log.error("SUPERVISOR_TOKEN fehlt – keine Verbindung zu Home Assistant möglich")
    log.info("Warte auf Home Assistant …")
    wait_for_core()
    try:
        tz = ZoneInfo(request("GET", f"{CORE}/config")["time_zone"])
    except Exception:  # noqa: BLE001
        tz = None
    self_name = request("GET", f"{SUPERVISOR}/addons/self/info")["data"]["name"]
    if opts.get("marketplace_warning_accepted"):
        accept_marketplace_warning()

    st = load_state()
    if st.get("running") or opts.get("run_on_start"):
        run(st, tz, self_name)

    while True:
        target = next_time(opts["time"], tz)
        log.info("Nächster Lauf: %s", target.strftime("%d.%m.%Y %H:%M"))
        while (remaining := (target - datetime.now(tz)).total_seconds()) > 0:
            time.sleep(min(60, remaining))
        run(st, tz, self_name)


if __name__ == "__main__":
    main()
