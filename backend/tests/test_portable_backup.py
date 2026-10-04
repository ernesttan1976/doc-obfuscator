import io
import zipfile

import pytest

from backend.app.portable_backup import (
    PortableBackupError,
    build_archive,
    decrypt_backup,
    encrypt_backup,
    read_archive,
)

PASSPHRASE = "correct horse battery staple"


def test_portable_backup_encrypts_and_authenticates_archive_contents():
    archive = build_archive({"backup.json": b'{"format":1}', "originals/doc/file.md": b"synthetic data"})
    encrypted = encrypt_backup(archive, PASSPHRASE)

    assert archive not in encrypted
    assert b"synthetic data" not in encrypted
    assert read_archive(decrypt_backup(encrypted, PASSPHRASE)) == {
        "backup.json": b'{"format":1}',
        "originals/doc/file.md": b"synthetic data",
    }
    with pytest.raises(PortableBackupError, match="passphrase is incorrect"):
        decrypt_backup(encrypted, "this is the wrong passphrase")


def test_portable_backup_rejects_short_passphrases_and_unsafe_archive_paths():
    with pytest.raises(PortableBackupError, match="12 and 1024"):
        encrypt_backup(b"archive", "short")

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../outside.txt", b"no")
    with pytest.raises(PortableBackupError, match="unsafe path"):
        read_archive(unsafe.getvalue())
