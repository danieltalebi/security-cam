"""Create and restore encrypted, portable security-cam configuration backups."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


MAGIC = b"SECURITY-CAM-BACKUP\x00\x01"
KDF_ITERATIONS = 600_000
MAX_BACKUP_BYTES = 100 * 1024 * 1024
MAX_PAYLOAD_BYTES = 50 * 1024 * 1024
STATE_FILES = {
    ".security-cam-state.json",
    ".telegram-mute.json",
    ".connectivity-history.json",
}
ROOT_FILES = {
    "multi-camera.json",
    "smart-monitor.json",
    "run_smart_windows.local.bat",
    *STATE_FILES,
}
ENV_PREFIXES = (
    "CAMERA_",
    "HA_MQTT_",
    "ASUS_ROUTER_",
    "HUAWEI_ROUTER_",
    "TELEGRAM_",
)


class BackupError(RuntimeError):
    pass


def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(
        algorithm=SHA256(), length=32, salt=salt, iterations=iterations,
    ).derive(password.encode("utf-8"))


def _git_revision(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=5, check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def collect_config_files(root: Path) -> dict[str, bytes]:
    """Collect deployable configuration and small persistent state files."""
    root = root.resolve()
    candidates: set[Path] = {root / name for name in ROOT_FILES}
    candidates.update(root.glob("*-monitor.json"))
    home_assistant = root / "home-assistant"
    if home_assistant.is_dir():
        for pattern in ("*.yaml", "*.yml", "*.json"):
            candidates.update(home_assistant.rglob(pattern))

    files: dict[str, bytes] = {}
    for path in sorted(candidates):
        if not path.is_file():
            continue
        relative = path.resolve().relative_to(root).as_posix()
        if "example" in path.name.lower() or "_ old" in path.name.lower():
            continue
        data = path.read_bytes()
        if len(data) > 10 * 1024 * 1024:
            raise BackupError(f"Configuration file is unexpectedly large: {relative}")
        files[relative] = data
    if "multi-camera.json" not in files:
        raise BackupError("multi-camera.json was not found; there is no local deployment to back up")
    return files


def _walk_environment_names(value) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if (str(key).endswith("_env") or key == "stream_env") and isinstance(item, str):
                if item:
                    yield item
            yield from _walk_environment_names(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_environment_names(item)


def referenced_environment_names(files: dict[str, bytes]) -> set[str]:
    names: set[str] = set()
    for path, data in files.items():
        if not path.endswith(".json"):
            continue
        try:
            config = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        names.update(_walk_environment_names(config))
        camera = config.get("camera", {}) if isinstance(config, dict) else {}
        if isinstance(camera, dict):
            prefix = camera.get("credential_prefix")
            if isinstance(prefix, str) and prefix:
                names.update({
                    f"{prefix}_USER", f"{prefix}_PASSWORD",
                    f"{prefix}_HOST", f"{prefix}_RTSP_URL",
                })
    return names


def windows_user_environment() -> dict[str, str]:
    values = {name: value for name, value in os.environ.items() if value}
    if os.name == "nt":
        import winreg

        try:
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


def collect_environment(files: dict[str, bytes]) -> dict[str, str]:
    available = windows_user_environment()
    referenced = referenced_environment_names(files)
    names = {
        name for name in available
        if name in referenced or name.startswith(ENV_PREFIXES)
    }
    return {name: available[name] for name in sorted(names) if available[name]}


def _payload(root: Path, files: dict[str, bytes], environment: dict[str, str]) -> bytes:
    manifest = {
        "format": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "git_revision": _git_revision(root),
        "files": {
            path: {
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for path, data in files.items()
        },
        "environment_names": sorted(environment),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr(
            "windows-environment.json",
            json.dumps(environment, indent=2, sort_keys=True) + "\n",
        )
        for path, data in files.items():
            archive.writestr(f"files/{path}", data)
    payload = buffer.getvalue()
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise BackupError("Portable configuration payload exceeds the 50 MiB safety limit")
    return payload


def create_backup(
    root: Path, output: Path, password: str,
    environment: dict[str, str] | None = None,
) -> dict:
    if len(password) < 12:
        raise BackupError("Backup password must contain at least 12 characters")
    root = root.resolve()
    files = collect_config_files(root)
    environment = collect_environment(files) if environment is None else dict(environment)
    plaintext = _payload(root, files, environment)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = {
        "cipher": "AES-256-GCM",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    aad = MAGIC + header_bytes
    ciphertext = AESGCM(_derive_key(password, salt, KDF_ITERATIONS)).encrypt(
        nonce, plaintext, aad,
    )
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output.parent, prefix=output.name + ".", suffix=".tmp", delete=False,
    ) as temporary:
        temporary.write(MAGIC)
        temporary.write(struct.pack(">I", len(header_bytes)))
        temporary.write(header_bytes)
        temporary.write(ciphertext)
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, output)
    return {
        "output": str(output),
        "files": sorted(files),
        "environment_names": sorted(environment),
        "bytes": output.stat().st_size,
    }


def _safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise BackupError(f"Unsafe path in backup: {value!r}")
    normalized = path.as_posix()
    allowed = (
        normalized in ROOT_FILES
        or normalized.endswith("-monitor.json")
        or normalized.startswith("home-assistant/")
    )
    if not allowed:
        raise BackupError(f"Unexpected path in backup: {value!r}")
    return normalized


def read_backup(source: Path, password: str) -> tuple[dict, dict[str, bytes], dict[str, str]]:
    source = source.resolve()
    if source.stat().st_size > MAX_BACKUP_BYTES:
        raise BackupError("Backup exceeds the 100 MiB safety limit")
    data = source.read_bytes()
    prefix_length = len(MAGIC)
    if data[:prefix_length] != MAGIC or len(data) < prefix_length + 4:
        raise BackupError("This is not a supported security-cam backup")
    header_length = struct.unpack(">I", data[prefix_length:prefix_length + 4])[0]
    header_start = prefix_length + 4
    header_end = header_start + header_length
    if header_length > 4096 or header_end >= len(data):
        raise BackupError("Backup header is invalid")
    try:
        header_bytes = data[header_start:header_end]
        header = json.loads(header_bytes)
        salt = base64.b64decode(header["salt"], validate=True)
        nonce = base64.b64decode(header["nonce"], validate=True)
        iterations = int(header["iterations"])
        if header.get("cipher") != "AES-256-GCM" or iterations < 100_000:
            raise ValueError("unsupported encryption settings")
        plaintext = AESGCM(_derive_key(password, salt, iterations)).decrypt(
            nonce, data[header_end:], MAGIC + header_bytes,
        )
    except InvalidTag as error:
        raise BackupError("Incorrect password or damaged backup") from error
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BackupError("Backup encryption header is invalid") from error
    if len(plaintext) > MAX_PAYLOAD_BYTES:
        raise BackupError("Decrypted payload exceeds the safety limit")

    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
            total = sum(item.file_size for item in archive.infolist())
            if total > MAX_PAYLOAD_BYTES:
                raise BackupError("Expanded backup exceeds the safety limit")
            manifest = json.loads(archive.read("manifest.json"))
            environment = json.loads(archive.read("windows-environment.json"))
            if not isinstance(environment, dict):
                raise BackupError("Environment payload is invalid")
            for item in archive.infolist():
                if not item.filename.startswith("files/") or item.is_dir():
                    continue
                relative = _safe_path(item.filename[6:])
                files[relative] = archive.read(item)
    except (KeyError, OSError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise BackupError("Decrypted backup payload is invalid") from error

    expected = manifest.get("files", {})
    if set(files) != set(expected):
        raise BackupError("Backup file list does not match its manifest")
    for path, data in files.items():
        if hashlib.sha256(data).hexdigest() != expected[path].get("sha256"):
            raise BackupError(f"Checksum mismatch for {path}")
    return manifest, files, {str(key): str(value) for key, value in environment.items()}


def _current_user_registry_environment() -> dict[str, str]:
    if os.name != "nt":
        return {}
    import winreg

    values = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                values[name] = str(value)
    except OSError:
        pass
    return values


def restore_environment(environment: dict[str, str], force: bool) -> list[str]:
    if os.name != "nt":
        raise BackupError("Automatic secret restoration is currently supported only on Windows")
    import winreg

    current = _current_user_registry_environment()
    conflicts = [
        name for name, value in environment.items()
        if name in current and current[name] != value
    ]
    if conflicts and not force:
        raise BackupError(
            "Existing Windows environment values differ: " + ", ".join(conflicts)
            + ". Re-run with --force only if they should be replaced."
        )
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        for name, value in environment.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            os.environ[name] = value
    try:
        import ctypes

        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000,
            ctypes.byref(result),
        )
    except (AttributeError, OSError):
        pass
    return sorted(environment)


def restore_backup(
    root: Path, source: Path, password: str, *,
    apply: bool = False, force: bool = False, files_only: bool = False,
) -> dict:
    root = root.resolve()
    manifest, files, environment = read_backup(source, password)
    conflicts = []
    unchanged = []
    pending = []
    for relative, data in files.items():
        destination = (root / Path(*PurePosixPath(relative).parts)).resolve()
        try:
            destination.relative_to(root)
        except ValueError as error:
            raise BackupError(f"Restore path escapes project root: {relative}") from error
        if destination.exists():
            if destination.read_bytes() == data:
                unchanged.append(relative)
            else:
                conflicts.append(relative)
        else:
            pending.append(relative)
    if conflicts and not force:
        raise BackupError(
            "Existing files differ: " + ", ".join(conflicts)
            + ". Re-run with --force only after reviewing them."
        )
    restored = []
    environment_restored = []
    if apply:
        for relative, data in files.items():
            destination = root / Path(*PurePosixPath(relative).parts)
            if destination.exists() and destination.read_bytes() == data:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".restore.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, destination)
            restored.append(relative)
        if environment and not files_only:
            environment_restored = restore_environment(environment, force)
    return {
        "manifest": manifest,
        "pending_files": pending,
        "conflicting_files": conflicts,
        "unchanged_files": unchanged,
        "restored_files": restored,
        "environment_names": sorted(environment),
        "environment_restored": environment_restored,
        "applied": apply,
    }


def _password(confirm: bool = False) -> str:
    configured = os.environ.get("SECURITY_CAM_BACKUP_PASSWORD")
    if configured:
        return configured
    value = getpass.getpass("Backup password: ")
    if confirm and value != getpass.getpass("Repeat backup password: "):
        raise BackupError("Passwords do not match")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export or restore encrypted security-cam configuration.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export", help="Create an encrypted portable backup")
    export_parser.add_argument("--output", type=Path)
    import_parser = subparsers.add_parser("import", help="Inspect or restore a portable backup")
    import_parser.add_argument("backup", type=Path)
    import_parser.add_argument("--apply", action="store_true", help="Actually restore files and Windows environment values")
    import_parser.add_argument("--force", action="store_true", help="Replace differing local files and environment values")
    import_parser.add_argument("--files-only", action="store_true", help="Do not restore Windows environment values")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent

    if args.command == "export":
        output = args.output or root / "backups" / (
            "security-cam-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".scbackup"
        )
        result = create_backup(root, output, _password(confirm=True))
        print(f"Encrypted backup created: {result['output']} ({result['bytes']:,} bytes)")
        print(f"Configuration/state files: {len(result['files'])}")
        print("Windows variables included: " + ", ".join(result["environment_names"]))
        print("Store this file and its password separately in a private location.")
        return 0

    result = restore_backup(
        root, args.backup, _password(),
        apply=args.apply, force=args.force, files_only=args.files_only,
    )
    action = "Restore completed" if args.apply else "Backup inspection completed (no changes made)"
    print(action + ".")
    print("Created or replaceable files: " + ", ".join(result["pending_files"] + result["conflicting_files"]))
    print("Unchanged files: " + ", ".join(result["unchanged_files"]))
    print("Windows variables: " + ", ".join(result["environment_names"]))
    if not args.apply:
        print("Run again with --apply to restore; add --force only for reviewed conflicts.")
    else:
        print("Open a new terminal or restart Security Cam so restored variables are inherited.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BackupError, FileNotFoundError, OSError) as error:
        print(f"BACKUP ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
