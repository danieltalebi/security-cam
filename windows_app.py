"""Small Windows desktop dashboard for the multi-camera monitor.

The packaged executable intentionally stays lightweight: it controls the tested
Python environment created by ``setup_windows.bat`` instead of bundling a second
copy of PyTorch, OpenCV, and the YOLO runtime.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from single_instance import SingleInstanceLock


APP_TITLE = "Security Cam"
MONITOR_SCRIPT = "multi_camera_monitor.py"
CONFIG_FILE = "multi-camera.json"
HEALTH_REFRESH_MS = 15_000
STATUS_COLORS = {
    "online": "#16803a",
    "warning": "#b45309",
    "offline": "#b91c1c",
    "unknown": "#64748b",
}


def find_project_root(start: Path | None = None) -> Path:
    """Locate the installed project when run from source or ``dist``."""
    candidates: list[Path] = []
    configured = os.environ.get("SECURITY_CAM_HOME")
    if configured:
        candidates.append(Path(configured))
    if start is not None:
        candidates.append(start)
    candidates.append(Path.cwd())
    executable = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    candidates.extend((executable.parent, executable.parent.parent))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / MONITOR_SCRIPT).is_file():
            return resolved
    raise FileNotFoundError(
        "No se encontró multi_camera_monitor.py. Ejecute SecurityCam.exe desde "
        "la carpeta del proyecto o defina SECURITY_CAM_HOME."
    )


def python_executable(root: Path) -> Path:
    windows_python = root / "venv" / "Scripts" / "python.exe"
    if windows_python.is_file():
        return windows_python
    development_python = root / "venv" / "bin" / "python"
    if development_python.is_file():
        return development_python
    raise FileNotFoundError("No se encontró venv. Ejecute setup_windows.bat primero.")


def monitor_command(root: Path) -> list[str]:
    return [
        str(python_executable(root)), "-u", str(root / MONITOR_SCRIPT),
        "--config", str(root / CONFIG_FILE),
    ]


def windows_environment() -> dict[str, str]:
    """Return the current environment plus fresh Windows user variables."""
    values = dict(os.environ)
    if os.name != "nt":
        return values
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if value:
                    values[name] = str(value)
    except OSError:
        pass
    return values


def camera_endpoints(project: Path, environment: dict[str, str]) -> list[tuple[str, str | None, int]]:
    """Load camera DVRIP endpoints without importing the heavyweight monitor."""
    multi_path = project / CONFIG_FILE
    try:
        multi = json.loads(multi_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    endpoints = []
    for entry in multi.get("cameras", []):
        name = str(entry.get("name") or "camera")
        try:
            config = json.loads((multi_path.parent / entry["config"]).read_text(encoding="utf-8"))
        except (KeyError, OSError, json.JSONDecodeError):
            endpoints.append((name, None, 34567))
            continue
        camera = config.get("camera", {})
        prefix = str(camera.get("credential_prefix") or f"CAMERA_{name.upper()}")
        configured = camera.get("host") or config.get("dvrip", {}).get("host")
        host = environment.get(f"{prefix}_HOST") or configured
        if host == "{host}":
            host = None
        endpoints.append((name, str(host) if host else None, int(config.get("dvrip", {}).get("port", 34567))))
    return endpoints


def parse_dvrip_log(text: str, camera_names: list[str]) -> dict[str, str]:
    """Return the latest connected/reconnecting state logged for each camera."""
    states = {name: "unknown" for name in camera_names}
    for line in text.splitlines():
        lowered = line.lower()
        for name in camera_names:
            prefix = f"{name.lower()}: dvrip "
            if prefix not in lowered:
                continue
            if "listener connected" in lowered:
                states[name] = "online"
            elif "listener reconnecting" in lowered or "stopped responding" in lowered:
                states[name] = "offline"
    return states


def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def internet_reachable(timeout: float = 4.0) -> bool:
    request = Request(
        "https://www.gstatic.com/generate_204",
        headers={"User-Agent": "security-cam-dashboard/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout):
            return True
    except HTTPError:
        return True
    except (OSError, URLError):
        return False


def telegram_reachable(environment: dict[str, str], timeout: float = 5.0) -> tuple[str, str, str]:
    token = environment.get("TELEGRAM_BOT_TOKEN")
    chat_id = environment.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return "unknown", "No configurado", "Faltan token o chat"
    base = f"https://api.telegram.org/bot{token}/"
    try:
        for method, payload in (("getMe", {}), ("getChat", {"chat_id": chat_id})):
            request = Request(
                base + method,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                return "offline", "Error", "Telegram rechazó la configuración"
        return "online", "Conectado", "Bot y chat validados"
    except (OSError, URLError, HTTPError, ValueError, json.JSONDecodeError):
        return "offline", "Sin conexión", "No se pudo validar Telegram"


def recent_log_text(path: Path, limit: int = 100_000) -> str:
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - limit))
            return source.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def alarm_policy_controller(project: Path):
    """Open the shared local policy state without importing monitor dependencies."""
    from alarm_policy import AlarmPolicyController

    config_path = project / CONFIG_FILE
    try:
        multi = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        multi = {}
    config = dict(multi.get("alarm_policy", {}))
    state_path = Path(config.get("state_file", ".security-cam-state.json"))
    if not state_path.is_absolute():
        state_path = project / state_path
    return AlarmPolicyController(config, state_path)


class SecurityCamApp:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.root = root
        self.project = find_project_root()
        self.log_dir = self.project / "logs"
        self.log_path = self.log_dir / "security-cam-windows.log"
        self.process: subprocess.Popen | None = None
        self.log_handle = None
        self.keep_monitor_running = False
        self.restart_pending = False
        self.status = tk.StringVar(value="Monitor detenido")
        self.health_check_running = False
        self.health_cards: dict[str, dict[str, object]] = {}
        self.policy = alarm_policy_controller(self.project)

        root.title(APP_TITLE)
        root.geometry("1100x860")
        root.minsize(860, 680)
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=APP_TITLE, font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.status, font=("Segoe UI", 11)).pack(anchor="w", pady=(2, 14))

        cards = ttk.Frame(frame)
        cards.pack(fill="x", pady=(0, 14))
        for column in range(3):
            cards.columnconfigure(column, weight=1, uniform="health")
        configured_cameras = [name for name, _host, _port in camera_endpoints(self.project, windows_environment())]
        card_specs = (
            ("alarm", "Modo de alarma"),
            ("control", "Control"),
            ("presence", "Celulares"),
            ("routers", "Routers"),
            ("incidents", "Incidentes"),
            ("notifications", "Notificaciones"),
            ("monitor", "Monitor"),
            *((name.lower(), f"{name.title()} DVRIP") for name in configured_cameras),
            ("internet", "Internet"),
            ("telegram", "Telegram"),
            ("home_assistant", "Home Assistant"),
        )
        for index, (key, title) in enumerate(card_specs):
            self._create_health_card(cards, key, title, index // 3, index % 3)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(0, 12))
        self.start_button = ttk.Button(actions, text="Iniciar monitor", command=self.start_monitor)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="Detener", command=self.stop_monitor)
        self.stop_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Diagnosticar", command=self.run_diagnostics).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Abrir configuración", command=lambda: self.open_path(self.project / CONFIG_FILE)).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Abrir carpeta de logs", command=lambda: self.open_path(self.log_dir)).pack(side="left")

        alarm_actions = ttk.Frame(frame)
        alarm_actions.pack(fill="x", pady=(0, 12))
        ttk.Button(alarm_actions, text="Auto", command=self.set_auto).pack(side="left", padx=(0, 8))
        ttk.Button(alarm_actions, text="Desarmar", command=lambda: self.set_alarm_mode("disarmed")).pack(side="left", padx=(0, 8))
        ttk.Button(alarm_actions, text="En casa", command=lambda: self.set_alarm_mode("armed_home")).pack(side="left", padx=(0, 8))
        ttk.Button(alarm_actions, text="Fuera", command=lambda: self.set_alarm_mode("armed_away")).pack(side="left", padx=(0, 8))
        ttk.Button(alarm_actions, text="Pausar 1 h", command=lambda: self.pause_notifications(3600)).pack(side="left", padx=(12, 8))
        ttk.Button(alarm_actions, text="Reanudar", command=self.resume_notifications).pack(side="left", padx=(0, 8))
        ttk.Button(alarm_actions, text="Reconocer incidentes", command=self.acknowledge_incidents).pack(side="left")

        ttk.Label(frame, text="Actividad reciente", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.log = tk.Text(frame, wrap="none", font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        self.poll()
        self.refresh_health_async()

    def _create_health_card(self, parent, key: str, title: str, row: int, column: int) -> None:
        card = self.tk.Frame(
            parent, background="#f8fafc", highlightbackground="#cbd5e1",
            highlightthickness=1, padx=12, pady=10,
        )
        card.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)
        heading = self.tk.Frame(card, background="#f8fafc")
        heading.pack(fill="x")
        indicator = self.tk.Label(
            heading, text="●", foreground=STATUS_COLORS["unknown"],
            background="#f8fafc", font=("Segoe UI", 12, "bold"),
        )
        indicator.pack(side="left")
        self.tk.Label(
            heading, text=title, background="#f8fafc",
            foreground="#334155", font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(6, 0))
        value = self.tk.Label(
            card, text="Comprobando…", anchor="w", background="#f8fafc",
            foreground="#0f172a", font=("Segoe UI", 13, "bold"),
        )
        value.pack(fill="x", pady=(5, 1))
        detail = self.tk.Label(
            card, text="", anchor="w", background="#f8fafc",
            foreground="#64748b", font=("Segoe UI", 9),
        )
        detail.pack(fill="x")
        self.health_cards[key] = {"indicator": indicator, "value": value, "detail": detail}

    def _set_health_card(self, key: str, state: str, value: str, detail: str) -> None:
        card = self.health_cards.get(key)
        if not card:
            return
        card["indicator"].configure(foreground=STATUS_COLORS.get(state, STATUS_COLORS["unknown"]))
        card["value"].configure(text=value)
        card["detail"].configure(text=detail)

    def set_status(self, message: str) -> None:
        self.status.set(message)

    def set_auto(self) -> None:
        mode = self.policy.set_automatic()
        self.set_status(f"Control automático · {mode}")
        self.refresh_health_async()

    def set_alarm_mode(self, mode: str) -> None:
        self.policy.set_manual(mode)
        self.set_status(f"Control manual · {mode}")
        self.refresh_health_async()

    def pause_notifications(self, seconds: float) -> None:
        self.policy.pause_for(seconds)
        self.set_status("Notificaciones de detección pausadas por 1 h")
        self.refresh_health_async()

    def resume_notifications(self) -> None:
        self.policy.resume_notifications()
        self.set_status("Notificaciones de detección reanudadas")
        self.refresh_health_async()

    def acknowledge_incidents(self) -> None:
        count = self.policy.acknowledge()
        self.set_status(f"{count} incidente(s) reconocido(s)")
        self.refresh_health_async()

    def start_monitor(self) -> None:
        from tkinter import messagebox

        if self.process and self.process.poll() is None:
            return
        if not (self.project / CONFIG_FILE).is_file():
            messagebox.showerror(APP_TITLE, "Falta multi-camera.json. Ejecute setup_camera_windows.bat.")
            return
        try:
            command = monitor_command(self.project)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)
            self.log_handle.write("\n--- Security Cam iniciado desde la aplicación de Windows ---\n")
            options = {
                "cwd": str(self.project),
                "stdout": self.log_handle,
                "stderr": subprocess.STDOUT,
            }
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            self.process = subprocess.Popen(command, **options)
            self.keep_monitor_running = True
            self.restart_pending = False
            self.set_status(f"Monitor activo · PID {self.process.pid}")
        except Exception as error:
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            messagebox.showerror(APP_TITLE, f"No se pudo iniciar el monitor:\n{error}")

    def _stop_worker(self, closing: bool = False) -> None:
        process = self.process
        if process and process.poll() is None:
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
                process.wait(timeout=6)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=3)
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
        self.process = None
        if closing:
            self.root.after(0, self.root.destroy)
        else:
            self.root.after(0, lambda: self.set_status("Monitor detenido"))

    def stop_monitor(self) -> None:
        self.keep_monitor_running = False
        self.restart_pending = False
        self.set_status("Deteniendo monitor…")
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def run_diagnostics(self) -> None:
        from tkinter import messagebox

        if not (self.project / CONFIG_FILE).is_file():
            messagebox.showerror(APP_TITLE, "Falta multi-camera.json. Ejecute setup_camera_windows.bat.")
            return

        def worker() -> None:
            try:
                command = [
                    str(python_executable(self.project)),
                    str(self.project / "diagnose_cameras.py"),
                    "--config", str(self.project / CONFIG_FILE),
                ]
                completed = subprocess.run(
                    command, cwd=self.project, capture_output=True, text=True,
                    timeout=90, check=False,
                )
                details = (completed.stdout + completed.stderr).strip() or "Sin salida."
                title = "Diagnóstico correcto" if completed.returncode == 0 else "Diagnóstico con errores"
                self.root.after(0, lambda: messagebox.showinfo(title, details[-5000:]))
            except Exception as error:
                message = f"Falló el diagnóstico:\n{error}"
                self.root.after(0, lambda text=message: messagebox.showerror(APP_TITLE, text))

        threading.Thread(target=worker, daemon=True).start()

    def open_path(self, path: Path) -> None:
        from tkinter import messagebox

        try:
            if path == self.log_dir:
                path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as error:
            messagebox.showerror(APP_TITLE, f"No se pudo abrir {path}:\n{error}")

    def refresh_log(self) -> None:
        if not self.log_path.is_file():
            return
        try:
            with self.log_path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                size = source.tell()
                source.seek(max(0, size - 100_000))
                text = source.read().decode("utf-8", errors="replace")
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", text)
            self.log.see("end")
            self.log.configure(state="disabled")
        except OSError:
            pass

    def _collect_health(self) -> dict[str, tuple[str, str, str]]:
        environment = windows_environment()
        log_text = recent_log_text(self.log_path)
        monitor_online = bool(self.process and self.process.poll() is None)
        results: dict[str, tuple[str, str, str]] = {
            "monitor": (
                "online" if monitor_online else "offline",
                "Activo" if monitor_online else "Detenido",
                f"PID {self.process.pid}" if monitor_online and self.process else "Use Iniciar monitor",
            )
        }

        policy = self.policy.snapshot()
        mode_labels = {
            "disarmed": ("online", "Desarmada"),
            "armed_home": ("warning", "Armada en casa"),
            "armed_away": ("offline", "Armada fuera"),
        }
        alarm_state, alarm_label = mode_labels.get(policy["mode"], ("unknown", policy["mode"]))
        results["alarm"] = (alarm_state, alarm_label, "Autoridad local en Python")
        results["control"] = (
            "online" if policy["control"] == "automatic" else "warning",
            "Automático" if policy["control"] == "automatic" else "Manual",
            "Presencia + horario" if policy["control"] == "automatic" else "Modo fijado por el usuario",
        )
        devices = policy.get("presence_devices", {})
        device_detail = " · ".join(
            f"{name}: {'presente' if value is True else 'ausente' if value is False else 'desconocido'}"
            for name, value in devices.items()
        ) or "Sin lecturas de celulares"
        presence_state = policy.get("presence", "unavailable")
        results["presence"] = (
            "online" if presence_state == "home" else "warning" if presence_state == "away" else "unknown",
            "En casa" if presence_state == "home" else "Fuera" if presence_state == "away" else "Desconocida",
            device_detail,
        )
        routers = policy.get("router_sources", {})
        router_detail = " · ".join(
            f"{name}: {'online' if value.get('available') else 'offline'}"
            for name, value in routers.items() if isinstance(value, dict)
        ) or "Sin lecturas de routers"
        router_online = [bool(value.get("available")) for value in routers.values() if isinstance(value, dict)]
        results["routers"] = (
            "online" if router_online and all(router_online) else "warning" if any(router_online) else "unknown",
            f"{sum(router_online)}/{len(router_online)} disponibles" if router_online else "Desconocidos",
            router_detail,
        )
        incidents = policy.get("active_incidents", [])
        results["incidents"] = (
            "warning" if incidents else "online",
            f"{len(incidents)} activo(s)" if incidents else "Sin actividad",
            " · ".join(
                f"{item.get('camera')}{' (reconocido)' if item.get('acknowledged') else ''}"
                for item in incidents
            ) or "No hay incidentes abiertos",
        )
        paused = policy.get("notifications_paused", False)
        results["notifications"] = (
            "warning" if paused else "online",
            "Pausadas" if paused else "Activas",
            (
                f"Restan {int(policy.get('notifications_pause_remaining', 0) // 60) + 1} min"
                if paused else "Las alertas técnicas nunca se pausan"
            ),
        )

        endpoints = camera_endpoints(self.project, environment)
        log_states = parse_dvrip_log(log_text, [name for name, _host, _port in endpoints])
        for name, host, port in endpoints:
            key = name.lower()
            if not host:
                results[key] = ("unknown", "Sin configurar", "Falta el host de la cámara")
                continue
            reachable = tcp_reachable(host, port)
            logged = log_states.get(name, "unknown")
            if logged == "online" and reachable:
                results[key] = ("online", "Conectado", f"DVRIP activo · {host}:{port}")
            elif reachable:
                results[key] = ("warning", "Cámara accesible", f"Puerto DVRIP {port} disponible")
            else:
                results[key] = ("offline", "Sin conexión", f"No responde {host}:{port}")

        internet_ok = internet_reachable()
        results["internet"] = (
            "online" if internet_ok else "offline",
            "En línea" if internet_ok else "Sin Internet",
            f"Actualizado {datetime.now().strftime('%H:%M:%S')}",
        )
        results["telegram"] = telegram_reachable(environment)

        ha_host = environment.get("HA_MQTT_HOST") or "homeassistant.local"
        core_online = tcp_reachable(ha_host, 8123)
        mqtt_configured = bool(environment.get("HA_MQTT_HOST") and environment.get("HA_MQTT_USER"))
        mqtt_port = int(environment.get("HA_MQTT_PORT") or 1883)
        mqtt_online = mqtt_configured and tcp_reachable(ha_host, mqtt_port)
        if mqtt_online:
            results["home_assistant"] = ("online", "MQTT conectado", f"{ha_host}:{mqtt_port}")
        elif core_online:
            results["home_assistant"] = (
                "warning", "Core disponible", "MQTT pendiente de configuración",
            )
        else:
            results["home_assistant"] = ("offline", "Sin conexión", "Home Assistant no responde")
        return results

    def refresh_health_async(self) -> None:
        if self.health_check_running:
            return
        self.health_check_running = True

        def worker() -> None:
            try:
                results = self._collect_health()
            except Exception:
                results = {}

            def apply() -> None:
                for key, values in results.items():
                    self._set_health_card(key, *values)
                self.health_check_running = False
                self.root.after(HEALTH_REFRESH_MS, self.refresh_health_async)

            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True, name="dashboard-health").start()

    def poll(self) -> None:
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            self.process = None
            if self.keep_monitor_running:
                self.set_status(f"Monitor finalizó · código {code} · reinicio en 5 s")
                if not self.restart_pending:
                    self.restart_pending = True
                    self.root.after(5000, self._restart_monitor)
            else:
                self.set_status(f"Monitor detenido · código {code}")
        self.refresh_log()
        self.root.after(1000, self.poll)

    def _restart_monitor(self) -> None:
        self.restart_pending = False
        if self.keep_monitor_running and not (
            self.process and self.process.poll() is None
        ):
            self.start_monitor()

    def close(self) -> None:
        from tkinter import messagebox

        self.keep_monitor_running = False
        self.restart_pending = False
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno(APP_TITLE, "El monitor está activo. ¿Detenerlo y salir?"):
                return
            self.set_status("Deteniendo monitor…")
            threading.Thread(target=self._stop_worker, args=(True,), daemon=True).start()
        else:
            self.root.destroy()


def main() -> int:
    import tkinter as tk
    from tkinter import messagebox

    instance_lock = SingleInstanceLock("security-cam-windows-app")
    if not instance_lock.acquire():
        return 0
    root = tk.Tk()
    try:
        app = SecurityCamApp(root)
        if "--start" in sys.argv:
            root.after(500, app.start_monitor)
    except Exception as error:
        root.withdraw()
        messagebox.showerror(APP_TITLE, str(error))
        return 1
    try:
        root.mainloop()
        return 0
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
