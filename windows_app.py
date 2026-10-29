"""Small Windows desktop dashboard for the multi-camera monitor.

The packaged executable intentionally stays lightweight: it controls the tested
Python environment created by ``setup_windows.bat`` instead of bundling a second
copy of PyTorch, OpenCV, and the YOLO runtime.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


APP_TITLE = "Security Cam"
MONITOR_SCRIPT = "multi_camera_monitor.py"
CONFIG_FILE = "multi-camera.json"


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
        self.status = tk.StringVar(value="Monitor detenido")

        root.title(APP_TITLE)
        root.geometry("900x620")
        root.minsize(720, 480)
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=APP_TITLE, font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.status, font=("Segoe UI", 11)).pack(anchor="w", pady=(2, 14))

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(0, 12))
        self.start_button = ttk.Button(actions, text="Iniciar monitor", command=self.start_monitor)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="Detener", command=self.stop_monitor)
        self.stop_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Diagnosticar", command=self.run_diagnostics).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Abrir configuración", command=lambda: self.open_path(self.project / CONFIG_FILE)).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Abrir carpeta de logs", command=lambda: self.open_path(self.log_dir)).pack(side="left")

        ttk.Label(frame, text="Actividad reciente", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.log = tk.Text(frame, wrap="none", font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        self.poll()

    def set_status(self, message: str) -> None:
        self.status.set(message)

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

    def poll(self) -> None:
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            self.process = None
            self.set_status(f"Monitor detenido · código {code}")
        self.refresh_log()
        self.root.after(1000, self.poll)

    def close(self) -> None:
        from tkinter import messagebox

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

    root = tk.Tk()
    try:
        SecurityCamApp(root)
    except Exception as error:
        root.withdraw()
        messagebox.showerror(APP_TITLE, str(error))
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
