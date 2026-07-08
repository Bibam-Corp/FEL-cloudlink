"""
CloudLink/FEL server ready for TurboWarp.

Features enabled by loading the built-in CLPv4 protocol:
- handshake, ping, setid
- global/private messages: gmsg, pmsg
- global/private variables: gvar, pvar
- rooms: link, unlink, per-packet room selection
- direct messages
- user lists, client object, server version, optional MOTD

Run:
    python server_example.py
Then connect TurboWarp to:
    ws://127.0.0.1:3000
"""

import json
import logging
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message="pkg_resources is deprecated as an API.*",
    category=UserWarning,
)

VENDOR_DIR = Path(__file__).with_name("cloudlink_vendor")
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

from cloudlink import server as CloudLinkServer
from cloudlink.server.protocols import clpv4, scratch


HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "3000"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_PACKETS = os.environ.get("LOG_PACKETS", "1").lower() not in {"0", "false", "no"}

# Set this to False if you want data packets to preserve object/list values.
# Keeping it True prevents old Scratch/TurboWarp projects from displaying
# objects as "[object Object]" when a JSON object is sent as message/variable data.
STRINGIFY_DATA_OBJECTS = True

# Optional server greeting shown after handshake.
ENABLE_MOTD = True
MOTD_MESSAGE = "Bienvenue sur le serveur FEL CloudLink."


DATA_COMMANDS = {"gmsg", "pmsg", "gvar", "pvar", "direct"}
NOISY_KEYS = {"val", "details"}


def log(message, level="INFO"):
    print(f"[{level}] {message}", flush=True)


def short(value, max_len=180):
    text = repr(value)
    if len(text) > max_len:
        return f"{text[:max_len]}..."
    return text


def packet_summary(message):
    if not isinstance(message, dict):
        return short(message)

    parts = [f"cmd={message.get('cmd') or message.get('method')!r}"]
    for key in ("listener", "name", "id", "rooms", "mode", "code", "code_id"):
        if key in message:
            parts.append(f"{key}={short(message[key], 80)}")
    for key in NOISY_KEYS:
        if key in message:
            parts.append(f"{key}={short(message[key])}")
    return " ".join(parts)


def client_label(client):
    snowflake = getattr(client, "snowflake", "?")
    username = getattr(client, "username", "")
    protocol = getattr(getattr(client, "protocol", None), "__qualname__", "unknown")
    rooms = sorted(getattr(client, "rooms", []))
    if username:
        return f"{snowflake}/{username} protocol={protocol} rooms={rooms}"
    return f"{snowflake} protocol={protocol} rooms={rooms}"


def make_json_safe(value):
    """Convert Python-only containers into JSON-compatible values."""
    if isinstance(value, set):
        return [make_json_safe(item) for item in value]
    if isinstance(value, list):
        return [make_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [make_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    return value


def stringify_if_object(value):
    """Return objects/lists as compact JSON text for Scratch-safe reporters."""
    value = make_json_safe(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def patch_outgoing_packets(app):
    """Normalize outgoing packets and optionally fix object-valued data packets."""
    original_execute_unicast = app.execute_unicast

    async def execute_unicast_compat(client, message):
        if isinstance(message, dict):
            message = make_json_safe(message)

            if STRINGIFY_DATA_OBJECTS and message.get("cmd") in DATA_COMMANDS:
                if "val" in message:
                    message["val"] = stringify_if_object(message["val"])

        if LOG_PACKETS:
            log(f"TX -> {client_label(client)} {packet_summary(message)}")

        await original_execute_unicast(client, message)

    app.execute_unicast = execute_unicast_compat


def patch_user_object_lookup(app):
    """Allow private recipients to be passed as CloudLink user objects."""
    original_room_find_obj = app.rooms_manager.find_obj
    original_client_find_obj = app.clients_manager.find_obj

    def identity_candidates(query):
        if isinstance(query, dict):
            for key in ("id", "uuid", "username"):
                value = query.get(key)
                if value is not None and str(value):
                    yield str(value)
        else:
            yield query

    def room_find_obj_compat(query, room):
        last_error = None
        for candidate in identity_candidates(query):
            try:
                return original_room_find_obj(candidate, room)
            except Exception as error:
                last_error = error
        if last_error:
            raise last_error
        raise app.rooms_manager.exceptions.NoResultsFound

    def client_find_obj_compat(query):
        last_error = None
        for candidate in identity_candidates(query):
            try:
                return original_client_find_obj(candidate)
            except Exception as error:
                last_error = error
        if last_error:
            raise last_error
        raise app.clients_manager.exceptions.NoResultsFound

    app.rooms_manager.find_obj = room_find_obj_compat
    app.clients_manager.find_obj = client_find_obj_compat


class ServerEvents:
    async def on_connect(self, client):
        peer = getattr(client, "remote_address", "?")
        headers = getattr(client, "request_headers", {})
        origin = headers.get("origin", "?") if hasattr(headers, "get") else "?"
        log(f"CONNECT {client.snowflake} peer={peer} origin={origin}")

    async def on_disconnect(self, client):
        log(f"DISCONNECT {client_label(client)}")

    async def on_error(self, client, error):
        log(f"ERROR {client_label(client)} {error}", "ERROR")

    async def on_message(self, client, message):
        if LOG_PACKETS:
            log(f"RX <- {client_label(client)} {packet_summary(message)}")


if __name__ == "__main__":
    app = CloudLinkServer()

    app.logging.basicConfig(
        level=LOG_LEVEL,
        format="[%(levelname)s] %(message)s",
        force=True
    )

    # Load full CloudLink v4 and Scratch cloud-variable support.
    cl4 = clpv4(app)
    scratch(app)

    cl4.enable_motd = ENABLE_MOTD
    cl4.motd_message = MOTD_MESSAGE

    patch_outgoing_packets(app)
    patch_user_object_lookup(app)

    events = ServerEvents()
    app.bind_event(app.on_connect, events.on_connect)
    app.bind_event(app.on_disconnect, events.on_disconnect)
    app.bind_event(app.on_error, events.on_error)
    app.bind_event(app.on_message, events.on_message)

    log(f"FEL CloudLink server listening on {HOST}:{PORT}")
    log("Local TurboWarp URL: ws://127.0.0.1:3000")
    log("Render URL: wss://<your-render-service>.onrender.com")
    log(f"Packet logs: {'enabled' if LOG_PACKETS else 'disabled'}")
    app.run(ip=HOST, port=PORT)
