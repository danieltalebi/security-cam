"""Router-backed Wi-Fi presence for Home Assistant.

The monitor deliberately matches devices by MAC address. DHCP addresses are
reported only as diagnostics and are never used as identity.
"""

from __future__ import annotations

import base64
import getpass
import http.cookiejar
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


MAC_PATTERN = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", re.IGNORECASE)


def normalize_mac(value: str) -> str:
    mac = value.strip().lower().replace("-", ":")
    if not MAC_PATTERN.fullmatch(mac):
        raise ValueError(f"Invalid MAC address: {value!r}")
    return mac


def configured_secret(config: dict, key: str) -> str | None:
    env_name = config.get(f"{key}_env")
    if env_name:
        value = os.environ.get(str(env_name))
        if value:
            return value
        if sys.platform == "darwin":
            try:
                result = subprocess.run(
                    [
                        "security", "find-generic-password",
                        "-a", getpass.getuser(),
                        "-s", f"security-cam-{env_name}",
                        "-w",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.rstrip("\n"):
                    return result.stdout.rstrip("\n")
            except (OSError, subprocess.SubprocessError):
                pass
        return None
    value = config.get(key)
    return str(value) if value else None


def decode_huawei_javascript(text: str) -> str:
    """Decode the hex escapes used in the Huawei's generated JavaScript."""
    return re.sub(
        r"\\x([0-9a-fA-F]{2})",
        lambda match: chr(int(match.group(1), 16)),
        text,
    )


def huawei_online_macs(text: str) -> set[str]:
    """Extract MACs which have an Online record in Huawei page data.

    In access-point mode the Huawei reports devices connected through the
    downstream AP as ``ETH`` clients.  Those records are just as useful for
    presence as devices connected directly to the Huawei's Wi-Fi.
    """
    decoded = decode_huawei_javascript(text)
    online: set[str] = set()
    # The firmware emits USERDevice/USERDeviceV6 constructor calls. Restrict
    # matching to a single call so an Offline record cannot borrow Online from
    # a later device.
    for call in re.findall(r"new\s+\w*USERDevice\w*\([^)]*\)", decoded, re.IGNORECASE):
        if not re.search(r'["\']Online["\']', call, re.IGNORECASE):
            continue
        if not re.search(r'["\'](?:WIFI|ETH)["\']', call, re.IGNORECASE):
            continue
        online.update(match.lower() for match in MAC_PATTERN.findall(call))
    return online


def asus_online_macs(text: str) -> set[str]:
    """Extract only currently-online MACs from ASUSWRT client data.

    ``update_clients.asp`` also contains ``nmpClient``, a historical database
    which keeps devices long after they disconnect.  Presence must use
    ``fromNetworkmapd`` and its explicit ``isOnline`` flag instead.
    """
    if "Main_Login.asp" in text or "ASUS Login" in text:
        raise PermissionError("ASUSWRT session is not authenticated")

    marker = re.search(r"\bfromNetworkmapd\s*:\s*\[", text)
    if not marker:
        raise ValueError("ASUSWRT response has no live-client collection")

    start = text.find("[", marker.start())
    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        raise ValueError("ASUSWRT live-client collection is incomplete")

    try:
        collections = json.loads(text[start:end])
    except json.JSONDecodeError as error:
        raise ValueError("ASUSWRT live-client collection is invalid") from error

    online: set[str] = set()
    for collection in collections:
        if not isinstance(collection, dict):
            continue
        for record in collection.values():
            if not isinstance(record, dict) or str(record.get("isOnline")) != "1":
                continue
            mac = str(record.get("mac", "")).lower()
            if MAC_PATTERN.fullmatch(mac):
                online.add(mac)
    return online


@dataclass(frozen=True)
class PresenceObservation:
    name: str
    available: bool
    present: bool = False
    detail: str = ""


@dataclass(frozen=True)
class RouterClient:
    ip: str
    mac: str
    interface: str
    name: str = ""


def huawei_ethernet_clients(text: str) -> list[RouterClient]:
    """Parse active IPv4 Ethernet clients from Huawei's detailed device page."""
    decoded = decode_huawei_javascript(text)
    clients: dict[tuple[str, str], RouterClient] = {}
    for arguments in re.findall(r"new\s+USERDev\s*\(([^)]*)\)", decoded, re.IGNORECASE):
        fields = re.findall(r'"([^"]*)"', arguments)
        if len(fields) < 8:
            continue
        path, ip, mac, interface = fields[:4]
        status, medium = fields[6:8]
        name = fields[9] if len(fields) > 9 else ""
        try:
            address = ipaddress.ip_address(ip)
            normalized_mac = normalize_mac(mac)
        except ValueError:
            continue
        if (
            address.version == 4
            and status.lower() == "online"
            and medium.upper() == "ETH"
            and path
        ):
            client = RouterClient(str(address), normalized_mac, interface, name)
            clients[(client.ip, client.mac)] = client
    return list(clients.values())


class RouterSource:
    def __init__(self, config: dict):
        self.name = str(config.get("name") or config.get("type") or "router")
        self.host = str(config["host"]).rstrip("/")
        if not self.host.startswith(("http://", "https://")):
            self.host = "http://" + self.host
        self.macs = {normalize_mac(value) for value in config.get("macs", [])}
        if not self.macs:
            raise ValueError(f"{self.name}: configure at least one MAC address")
        self.username = configured_secret(config, "username")
        self.password = configured_secret(config, "password")
        self.timeout = float(config.get("timeout_seconds", 8))
        self._new_session()

    def _new_session(self) -> None:
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def _request(
        self, path: str, data: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        body = urlencode(data).encode() if data is not None else None
        request_headers = {"User-Agent": "security-cam-presence/1.0"}
        if headers:
            request_headers.update(headers)
        request = Request(
            self.host + path,
            data=body,
            headers=request_headers,
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8", "replace")

    def poll(self) -> PresenceObservation:
        try:
            online = self.online_macs()
            matches = sorted(self.macs & online)
            return PresenceObservation(
                self.name, True, bool(matches),
                "matched " + ", ".join(matches) if matches else "no configured MAC online",
            )
        except (HTTPError, URLError, TimeoutError, OSError, PermissionError, ValueError) as error:
            return PresenceObservation(self.name, False, detail=f"{type(error).__name__}: {error}")

    def online_macs(self) -> set[str]:
        raise NotImplementedError


class HuaweiRouterSource(RouterSource):
    CLIENT_PATH = "/html/bbsp/userdevinfo/userdevinfosmart.asp?type=wifidev"
    DETAIL_PATH = "/html/bbsp/userdevinfo/userdetdevinfo.asp"

    def _login(self) -> None:
        if not self.username or not self.password:
            raise PermissionError(
                f"set {self.name} username/password environment variables"
            )
        self._new_session()
        # This Huawei firmware prefixes the token response with a UTF-8 BOM.
        # It is not considered whitespace by ``str.strip()``, and sending it
        # back makes an otherwise valid login fail.
        token = self._request("/asp/GetRandCount.asp").lstrip("\ufeff").strip().split("|")[0]
        self._request(
            "/login.cgi",
            {
                "UserName": self.username,
                "PassWord": base64.b64encode(self.password.encode()).decode(),
                "Language": "english",
                "x.X_HW_Token": token,
            },
        )

    def online_macs(self) -> set[str]:
        page = self._request(self.CLIENT_PATH)
        if "top.location.replace" in page or "Waiting..." in page:
            self._login()
            page = self._request(self.CLIENT_PATH)
        if "top.location.replace" in page or "Waiting..." in page:
            raise PermissionError("Huawei login failed")
        return huawei_online_macs(page)

    def ethernet_clients(self) -> list[RouterClient]:
        page = self._request(self.DETAIL_PATH)
        if "top.location.replace" in page or "Waiting..." in page:
            self._login()
            page = self._request(self.DETAIL_PATH)
        if "top.location.replace" in page or "Waiting..." in page:
            raise PermissionError("Huawei login failed")
        return huawei_ethernet_clients(page)


class AsusRouterSource(RouterSource):
    def __init__(self, config: dict):
        super().__init__(config)
        self.discover_via = str(config.get("discover_via", ""))
        self.device_ouis = {
            normalize_mac(value + ":00:00:00")[:8]
            for value in config.get("device_ouis", [])
        }

    def use_discovered_host(self, host: str) -> None:
        self.host = "http://" + str(ipaddress.ip_address(host))
        self._new_session()

    def _client_page(self) -> str:
        return self._request(
            "/update_clients.asp",
            headers={"Referer": self.host + "/index.asp"},
        )

    def _login(self) -> None:
        if not self.username or not self.password:
            raise PermissionError(
                f"set {self.name} username/password environment variables"
            )
        self._new_session()
        authorization = base64.b64encode(
            f"{self.username}:{self.password}".encode()
        ).decode()
        self._request(
            "/login.cgi",
            {
                "group_id": "",
                "action_mode": "",
                "action_script": "",
                "action_wait": "5",
                "current_page": "Main_Login.asp",
                "next_page": "index.asp",
                "login_authorization": authorization,
                "login_captcha": "",
            },
            # ASUSWRT rejects an otherwise valid login with NOREFERER (4)
            # unless the request identifies its own login page as the source.
            headers={"Referer": self.host + "/Main_Login.asp"},
        )

    def online_macs(self) -> set[str]:
        page = self._client_page()
        try:
            online = asus_online_macs(page)
        except PermissionError:
            self._login()
            page = self._client_page()
            online = asus_online_macs(page)
        return online


SOURCE_TYPES = {
    "huawei": HuaweiRouterSource,
    "asuswrt": AsusRouterSource,
}


def combined_presence(observations: list[PresenceObservation]) -> str:
    """Return home, away, or unavailable using fail-safe semantics."""
    if any(item.available and item.present for item in observations):
        return "home"
    if observations and all(item.available for item in observations):
        return "away"
    return "unavailable"


class RouterPresenceMonitor:
    def __init__(
        self,
        config: dict,
        publish: Callable[[str, dict], None],
    ):
        self.interval = max(10.0, float(config.get("poll_interval_seconds", 30)))
        self.publish = publish
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.sources: list[RouterSource] = []
        source_configs = [dict(item) for item in config.get("sources", [])]
        configs_by_name = {
            str(item.get("name") or item.get("type") or "router"): item
            for item in source_configs
        }
        for source_config in source_configs:
            macs_from = source_config.get("macs_from")
            if macs_from and not source_config.get("macs"):
                referenced = configs_by_name.get(str(macs_from))
                if not referenced:
                    raise ValueError(f"Unknown presence macs_from source: {macs_from!r}")
                source_config["macs"] = list(referenced.get("macs", []))
            source_type = str(source_config.get("type", "")).lower()
            source_class = SOURCE_TYPES.get(source_type)
            if not source_class:
                raise ValueError(f"Unsupported presence source type: {source_type!r}")
            self.sources.append(source_class(source_config))
        if not self.sources:
            raise ValueError("Configure at least one router presence source")
        self.sources_by_name = {source.name: source for source in self.sources}

    def start(self) -> "RouterPresenceMonitor":
        self.thread = threading.Thread(target=self._run, name="router-presence", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def poll_once(self) -> tuple[str, dict]:
        observations = []
        for source in self.sources:
            observation = source.poll()
            if isinstance(source, AsusRouterSource) and not observation.available:
                observation = self._retry_discovered_asus(source, observation)
            observations.append(observation)
        state = combined_presence(observations)
        attributes = {
            "checked_at": datetime.now().astimezone().isoformat(),
            "sources": {
                item.name: {
                    "available": item.available,
                    "present": item.present,
                    "detail": item.detail,
                }
                for item in observations
            },
        }
        self.publish(state, attributes)
        return state, attributes

    def _retry_discovered_asus(
        self, source: AsusRouterSource, original: PresenceObservation,
    ) -> PresenceObservation:
        if not source.discover_via or not source.device_ouis:
            return original
        upstream = self.sources_by_name.get(source.discover_via)
        if not isinstance(upstream, HuaweiRouterSource):
            return PresenceObservation(
                source.name, False,
                detail=f"{original.detail}; discovery source {source.discover_via!r} is unavailable",
            )
        try:
            clients = upstream.ethernet_clients()
        except (HTTPError, URLError, TimeoutError, OSError, PermissionError, ValueError) as error:
            return PresenceObservation(
                source.name, False,
                detail=f"{original.detail}; Huawei discovery failed: {type(error).__name__}",
            )
        candidates = [
            client for client in clients
            if any(client.mac.startswith(oui) for oui in source.device_ouis)
        ]
        if not candidates:
            return PresenceObservation(
                source.name, False,
                detail=f"{original.detail}; no matching ASUS Ethernet client on Huawei",
            )
        failures = []
        for client in candidates:
            source.use_discovered_host(client.ip)
            retried = source.poll()
            if retried.available:
                return PresenceObservation(
                    retried.name, True, retried.present,
                    detail=f"{retried.detail}; discovered via {upstream.name} at {client.ip}",
                )
            failures.append(client.ip)
        return PresenceObservation(
            source.name, False,
            detail=f"{original.detail}; discovered ASUS at {', '.join(failures)} but it was unreachable",
        )

    def _run(self) -> None:
        while not self.stop_event.is_set():
            state, attributes = self.poll_once()
            summary = ", ".join(
                f"{name}={'home' if data['present'] else 'away' if data['available'] else 'unavailable'}"
                for name, data in attributes["sources"].items()
            )
            print(f"Wi-Fi presence: {state} ({summary})")
            self.stop_event.wait(self.interval)
