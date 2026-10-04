from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EncryptedStateError(Exception):
    """Encrypted project state is invalid or cannot be authenticated."""


def encrypt_state(project_id: str, state: dict[str, Any], key: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    aad = f"blot-project-state:v1:{project_id}".encode()
    plaintext = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    envelope = {
        "version": 1,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("ascii")


def decrypt_state(project_id: str, payload: bytes, key: bytes) -> dict[str, Any]:
    try:
        envelope = json.loads(payload)
        if envelope.get("version") != 1:
            raise EncryptedStateError("Unsupported encrypted project state version.")
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        if len(nonce) != 12:
            raise EncryptedStateError("Invalid encrypted project state nonce.")
        aad = f"blot-project-state:v1:{project_id}".encode()
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
        state = json.loads(plaintext)
        if not isinstance(state, dict):
            raise EncryptedStateError("Encrypted project state must be an object.")
        return state
    except EncryptedStateError:
        raise
    except (InvalidTag, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise EncryptedStateError("Project state could not be authenticated or decoded.") from exc


def atomic_write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".state-", delete=False) as temp:
            temporary_path = Path(temp.name)
            temp.write(payload)
            temp.flush()
            os.fsync(temp.fileno())
        _set_private_permissions(temporary_path, directory=False)
        os.replace(temporary_path, path)
        _set_private_permissions(path, directory=False)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _set_private_permissions(path: Path, *, directory: bool) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
