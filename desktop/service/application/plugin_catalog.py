"""Transport-independent plugin installation and enablement use cases.

The plugin archive is local and bounded; install never executes code, fetches
packages or modifies user hook settings. Activation of executable plugin
components requires explicit trust plus RIDGE_ALLOW_PLUGIN_CODE.
"""
from __future__ import annotations

from pathlib import Path

from core.plugin_loader import PluginCatalog
from .errors import ConflictError, ForbiddenError, InvalidInputError, NotFoundError, PayloadTooLargeError


class PluginCatalogService:
    def __init__(self, project_root: str | Path):
        self.catalog = PluginCatalog(project_root)

    def list(self) -> list[dict]:
        return self.catalog.list_plugins()

    def install(self, archive: bytes) -> dict:
        try:
            return self.catalog.install_zip(archive)
        except FileExistsError as exc:
            raise ConflictError("Plugin is already installed") from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        except (OSError, PermissionError) as exc:
            raise ForbiddenError("Unable to install plugin archive") from exc

    def enable(self, name: str, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise InvalidInputError("enabled must be boolean")
        try:
            return self.catalog.set_enabled(name, enabled)
        except KeyError as exc:
            raise NotFoundError("Plugin not found") from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def trust(self, name: str, trusted: bool) -> dict:
        if type(trusted) is not bool:
            raise InvalidInputError("trusted must be boolean")
        try:
            return self.catalog.set_trusted(name, trusted)
        except KeyError as exc:
            raise NotFoundError("Plugin not found") from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def update_zip(self, name: str, archive: bytes, sha256: str | None = None) -> dict:
        try:
            return self.catalog.update_zip(name, archive, expected_sha256=sha256)
        except KeyError as exc:
            raise NotFoundError("Plugin not found") from exc
        except PermissionError as exc:
            raise ForbiddenError("Unpin plugin before updating") from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        except OSError as exc:
            raise ForbiddenError("Unable to update plugin") from exc

    def pin(self, name: str, pinned: bool) -> dict:
        if type(pinned) is not bool:
            raise InvalidInputError("pinned must be boolean")
        try:
            return self.catalog.set_pinned(name, pinned)
        except KeyError as exc:
            raise NotFoundError("Plugin not found") from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def rollback(self, name: str) -> dict:
        try:
            return self.catalog.rollback(name)
        except KeyError as exc:
            raise NotFoundError("Previous plugin revision not found") from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        except OSError as exc:
            raise ForbiddenError("Unable to rollback plugin") from exc

    def delete(self, name: str) -> dict:
        try:
            return self.catalog.uninstall(name)
        except KeyError as exc:
            raise NotFoundError("Plugin not found") from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
