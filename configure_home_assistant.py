"""Configure the portable Home Assistant MQTT output."""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    reader = getpass.getpass if secret else input
    suffix = f" [{default}]" if default else ""
    while True:
        value = reader(f"{label}{suffix}: ").strip() or (default or "")
        if value:
            return value
        print("A value is required.")


def save_environment(values: dict[str, str]) -> None:
    if os.name != "nt":
        print("\nAdd these variables to the service or shell that runs the monitor:")
        for name in values:
            print(f"  export {name}=<value>")
        return
    for name, value in values.items():
        completed = subprocess.run(["setx", name, value], capture_output=True, text=True, check=False)
        if completed.returncode:
            raise RuntimeError(f"Could not save {name} as a Windows user variable.")


def main() -> None:
    print("\nHome Assistant MQTT setup\n")
    path = ROOT / "multi-camera.json"
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
    else:
        config = {"cameras": []}
    host = prompt("MQTT broker hostname", "homeassistant.local")
    port = prompt("MQTT broker port", "1883")
    username = prompt("MQTT username", "security_cam")
    password = prompt("MQTT password", secret=True)
    mode = prompt("Output mode: mqtt or both", "mqtt").lower()
    if mode not in {"mqtt", "both"}:
        raise RuntimeError("Output mode must be mqtt or both.")

    config["outputs"] = {"mode": mode}
    config["home_assistant"] = {
        "mqtt": {
            "host_env": "HA_MQTT_HOST",
            "port_env": "HA_MQTT_PORT",
            "username_env": "HA_MQTT_USER",
            "password_env": "HA_MQTT_PASSWORD",
            "topic_prefix": "security_cam",
            "discovery_prefix": "homeassistant",
            "qos": 1,
            "tls": False,
        }
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    save_environment({
        "HA_MQTT_HOST": host,
        "HA_MQTT_PORT": port,
        "HA_MQTT_USER": username,
        "HA_MQTT_PASSWORD": password,
    })
    print("\nHome Assistant MQTT output configured in multi-camera.json.")
    if os.name == "nt":
        print("Close this window and open a new Command Prompt before running the monitor.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        sys.exit(1)
    except Exception as error:
        print(f"\nSetup failed: {type(error).__name__}: {error}")
        sys.exit(1)
