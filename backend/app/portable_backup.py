from __future__ import annotations

import io
import secrets
import zipfile
from pathlib import PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"BLOTBACKUP\x00\x01"
SALT_SIZE = 16
NONCE_SIZE = 12
MAX_BACKUP_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000


class PortableBackupError(ValueError):
    """The portable backup is invalid or cannot be decrypted."""


def encrypt_backup(archive: bytes, passphrase: str) -> bytes:
    _validate_passphrase(passphrase)
    if len(archive) > MAX_BACKUP_BYTES:
        raise PortableBackupError("The project backup exceeds the 1 GB portable-backup limit.")
    salt = secrets.token_bytes(SALT_SIZE)
    nonce = secrets.token_bytes(NONCE_SIZE)
    key = _derive_key(passphrase, salt)
    return MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, archive, MAGIC)


def decrypt_backup(payload: bytes, passphrase: str) -> bytes:
    _validate_passphrase(passphrase)
    minimum_size = len(MAGIC) + SALT_SIZE + NONCE_SIZE + 16
    if len(payload) > MAX_BACKUP_BYTES or len(payload) < minimum_size or not payload.startswith(MAGIC):
        raise PortableBackupError("This file is not a supported encrypted Blot backup.")
    offset = len(MAGIC)
    salt = payload[offset : offset + SALT_SIZE]
    offset += SALT_SIZE
    nonce = payload[offset : offset + NONCE_SIZE]
    ciphertext = payload[offset + NONCE_SIZE :]
    try:
        archive = AESGCM(_derive_key(passphrase, salt)).decrypt(nonce, ciphertext, MAGIC)
    except InvalidTag as exc:
        raise PortableBackupError("The backup passphrase is incorrect or the backup was damaged.") from exc
    if len(archive) > MAX_BACKUP_BYTES:
        raise PortableBackupError("The decrypted project backup exceeds the 1 GB size limit.")
    return archive


def build_archive(entries: dict[str, bytes]) -> bytes:
    if len(entries) > MAX_ARCHIVE_FILES:
        raise PortableBackupError("The project contains too many files for a portable backup.")
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, payload in sorted(entries.items()):
                _validate_archive_name(name)
                if len(payload) > 100 * 1024 * 1024:
                    raise PortableBackupError("A project version exceeds the 100 MB per-file backup limit.")
                archive.writestr(name, payload)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PortableBackupError("The project backup could not be assembled.") from exc
    if output.tell() > MAX_BACKUP_BYTES:
        raise PortableBackupError("The project backup exceeds the 1 GB portable-backup limit.")
    return output.getvalue()


def read_archive(payload: bytes) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise PortableBackupError("The backup contains too many files.")
            total_size = 0
            for info in infos:
                _validate_archive_name(info.filename)
                if info.is_dir():
                    continue
                if info.file_size > 100 * 1024 * 1024:
                    raise PortableBackupError("A backup entry exceeds the 100 MB per-file limit.")
                total_size += info.file_size
                if total_size > MAX_BACKUP_BYTES:
                    raise PortableBackupError("The expanded backup exceeds the 1 GB size limit.")
                if info.filename in entries:
                    raise PortableBackupError("The backup contains duplicate paths.")
                entries[info.filename] = archive.read(info)
    except PortableBackupError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, ValueError) as exc:
        raise PortableBackupError("The encrypted backup contents are invalid.") from exc
    return entries


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))


def _validate_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str) or len(passphrase) < 12 or len(passphrase) > 1024:
        raise PortableBackupError("Use a backup passphrase between 12 and 1024 characters.")


def _validate_archive_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or name.startswith("/")
    ):
        raise PortableBackupError("The backup contains an unsafe path.")
