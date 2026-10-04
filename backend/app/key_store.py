from __future__ import annotations

import base64
import secrets
from typing import Protocol

import keyring

SERVICE_NAME = "Blot Local Project Key"


class KeyStoreUnavailable(RuntimeError):
    """The configured OS credential store is unavailable or insecure."""


class ProjectKeyStore(Protocol):
    def get_or_create(self, project_id: str) -> bytes: ...

    def get(self, project_id: str) -> bytes: ...

    def delete(self, project_id: str) -> None: ...


class OSKeyringProjectKeyStore:
    """Keep project data keys in the operating system credential store."""

    def __init__(self) -> None:
        try:
            backend = keyring.get_keyring()
            priority = float(getattr(backend, "priority", 0))
        except Exception as exc:
            raise KeyStoreUnavailable("No operating-system credential store is available.") from exc
        module = backend.__class__.__module__.lower()
        is_system_backend = any(name in module for name in ("macos", "windows", "win_cred"))
        if priority <= 0 or not is_system_backend:
            raise KeyStoreUnavailable(
                "Blot requires the macOS Keychain or Windows Credential Manager; plaintext keyring backends are not accepted."
            )

    def get_or_create(self, project_id: str) -> bytes:
        try:
            stored = keyring.get_password(SERVICE_NAME, project_id)
            if stored is None:
                key = secrets.token_bytes(32)
                encoded = base64.b64encode(key).decode("ascii")
                keyring.set_password(SERVICE_NAME, project_id, encoded)
                stored = keyring.get_password(SERVICE_NAME, project_id)
            if stored is None:
                raise KeyStoreUnavailable("The operating-system credential store did not retain the project key.")
            key = base64.b64decode(stored, validate=True)
            if len(key) != 32:
                raise KeyStoreUnavailable("The stored project key has an invalid length.")
            return key
        except KeyStoreUnavailable:
            raise
        except Exception as exc:
            raise KeyStoreUnavailable("Could not access the operating-system credential store.") from exc

    def get(self, project_id: str) -> bytes:
        try:
            stored = keyring.get_password(SERVICE_NAME, project_id)
            if stored is None:
                raise KeyStoreUnavailable("The project key is missing from the operating-system credential store.")
            key = base64.b64decode(stored, validate=True)
            if len(key) != 32:
                raise KeyStoreUnavailable("The stored project key has an invalid length.")
            return key
        except KeyStoreUnavailable:
            raise
        except Exception as exc:
            raise KeyStoreUnavailable("Could not access the operating-system credential store.") from exc

    def delete(self, project_id: str) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, project_id)
        except keyring.errors.PasswordDeleteError:
            return
        except Exception as exc:
            raise KeyStoreUnavailable("Could not remove the project key from the operating-system credential store.") from exc
