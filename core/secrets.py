"""Local secret storage: OS keychain first, private file as a fallback.

The file fallback exists so the app still works on headless Linux or in a
container where no Secret Service is running. It is written with 0600
permissions and never leaves the machine.

Names are namespaced: ``model.<profile>`` holds a model API key, tool keys use
their conventional environment-variable name (``TAVILY_API_KEY``).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

SERVICE = "ridgecode"
TOOL_KEYS = ("TAVILY_API_KEY",)
MAX_SECRET_CHARS = 4096


def data_dir() -> Path:
    override = os.getenv("MYCODEAGENT_DATA_DIR", "").strip()
    return Path(override) if override else Path.cwd() / ".mycodeagent"


def secrets_path() -> Path:
    return data_dir() / "settings" / "keys.json"


def _keyring() -> Any:
    try:
        import keyring

        return keyring
    except Exception:
        return None


def _keyring_works() -> bool:
    """A keyring module can import yet have no usable backend (headless Linux)."""
    keyring = _keyring()
    if keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
        name = type(backend).__name__.lower()
        return not any(marker in name for marker in ("fail", "null", "chainer"))
    except Exception:
        return False


def _read_file() -> dict[str, str]:
    path = secrets_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    values = raw.get("values") if isinstance(raw, dict) else None
    if not isinstance(values, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in values.items()
        if isinstance(value, str) and value
    }


def _write_file(values: dict[str, str]) -> None:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps({"values": values}, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _validate(name: str) -> str:
    key = str(name or "").strip()
    if not key or len(key) > 128 or any(ch.isspace() for ch in key):
        raise ValueError("Invalid secret name")
    return key


def get_secret(name: str) -> str | None:
    """Keychain value, then the private file, then the environment."""
    key = _validate(name)
    if _keyring_works():
        try:
            value = _keyring().get_password(SERVICE, key)
            if value:
                return value
        except Exception:
            pass
    value = _read_file().get(key)
    if value:
        return value
    env = os.getenv(key, "").strip()
    return env or None


def set_secret(name: str, value: str) -> str:
    """Store a secret; returns ``keyring`` or ``file`` for the actual backend."""
    key = _validate(name)
    text = str(value or "").strip()
    if not text:
        raise ValueError("Secret value is required")
    if len(text) > MAX_SECRET_CHARS:
        raise ValueError("Secret value is too large")
    # Keep the file copy in sync: whichever backend works now must be the one a
    # later read resolves, so a stale fallback can never shadow a rotated key.
    if _keyring_works():
        try:
            _keyring().set_password(SERVICE, key, text)
            values = _read_file()
            if key in values:
                values.pop(key)
                _write_file(values)
            return "keyring"
        except Exception:
            pass
    values = _read_file()
    values[key] = text
    _write_file(values)
    return "file"


def delete_secret(name: str) -> bool:
    key = _validate(name)
    removed = False
    if _keyring_works():
        try:
            _keyring().delete_password(SERVICE, key)
            removed = True
        except Exception:
            pass
    values = _read_file()
    if key in values:
        values.pop(key)
        _write_file(values)
        removed = True
    return removed


def mask(value: str | None) -> str | None:
    if not value:
        return None
    return "***" + value[-4:] if len(value) >= 12 else "***"


def secret_status(name: str) -> dict[str, Any]:
    value = get_secret(name)
    source = "none"
    if value:
        if _keyring_works():
            try:
                if _keyring().get_password(SERVICE, name):
                    source = "keyring"
            except Exception:
                pass
        if source == "none":
            source = "file" if name in _read_file() else "env"
    return {
        "name": name,
        "configured": bool(value),
        "source": source,
        "api_key": mask(value),
    }
