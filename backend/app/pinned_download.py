from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from urllib.request import Request, urlopen


class DownloadCancelled(Exception):
    """An explicitly cancelled pinned-artifact download."""


class DownloadIntegrityError(RuntimeError):
    """A downloaded artifact failed its pinned size or digest check."""


def resumable_download(
    files,
    directory: Path,
    url_for_file: Callable[[object], str],
    cancelled,
    progress: Callable[[int], None],
    *,
    opener=urlopen,
) -> int:
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    completed = 0
    for artifact in files:
        if cancelled.is_set():
            raise DownloadCancelled
        target = directory / artifact.name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        partial = target.with_name(f"{target.name}.partial")
        if target.exists():
            if _valid_artifact(target, artifact):
                completed += artifact.size
                progress(completed)
                continue
            target.unlink()
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > artifact.size:
            partial.unlink()
            offset = 0
        if offset == artifact.size and offset:
            if _valid_artifact(partial, artifact):
                os.replace(partial, target)
                completed += artifact.size
                progress(completed)
                continue
            partial.unlink()
            offset = 0

        request = Request(
            url_for_file(artifact),
            headers={"Range": f"bytes={offset}-"} if offset else {},
        )
        with opener(request, timeout=30) as response:
            status = getattr(response, "status", None)
            if status is None:
                getcode = getattr(response, "getcode", None)
                status = getcode() if getcode else None
            if offset and status != 206:
                offset = 0
            content_range = response.headers.get("Content-Range") if hasattr(response, "headers") else None
            if offset and content_range and not content_range.startswith(f"bytes {offset}-"):
                raise DownloadIntegrityError("The model server returned an invalid byte range.")

            sha256 = hashlib.sha256()
            git_sha1 = hashlib.sha1(f"blob {artifact.size}\0".encode())
            if offset:
                with partial.open("rb") as prefix:
                    while chunk := prefix.read(1024 * 1024):
                        sha256.update(chunk)
                        git_sha1.update(chunk)
            received = 0
            mode = "ab" if offset else "wb"
            if not offset:
                progress(completed)
            with partial.open(mode) as destination:
                while True:
                    if cancelled.is_set():
                        raise DownloadCancelled
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if offset + received > artifact.size:
                        partial.unlink(missing_ok=True)
                        raise DownloadIntegrityError("A downloaded model artifact exceeded its pinned size.")
                    sha256.update(chunk)
                    git_sha1.update(chunk)
                    destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                    progress(completed + offset + received)
            total = offset + received
            if total != artifact.size:
                raise DownloadIntegrityError("A model artifact is incomplete and can be resumed.")
            if artifact.sha256 and sha256.hexdigest() != artifact.sha256:
                partial.unlink(missing_ok=True)
                raise DownloadIntegrityError("A model artifact failed its pinned SHA-256 check.")
            if artifact.git_sha1 and git_sha1.hexdigest() != artifact.git_sha1:
                partial.unlink(missing_ok=True)
                raise DownloadIntegrityError("A model artifact failed its pinned Git blob check.")
        os.replace(partial, target)
        completed += artifact.size
        progress(completed)
    return completed


def partial_download_size(files, directory: Path) -> int:
    total = 0
    for artifact in files:
        target = directory / artifact.name
        partial = target.with_name(f"{target.name}.partial")
        if target.is_file():
            total += min(target.stat().st_size, artifact.size)
        elif partial.is_file():
            total += min(partial.stat().st_size, artifact.size)
    return total


def _valid_artifact(path: Path, artifact) -> bool:
    if not path.is_file() or path.stat().st_size != artifact.size:
        return False
    sha256 = hashlib.sha256()
    git_sha1 = hashlib.sha1(f"blob {artifact.size}\0".encode())
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            sha256.update(chunk)
            git_sha1.update(chunk)
    return (
        (not artifact.sha256 or sha256.hexdigest() == artifact.sha256)
        and (not artifact.git_sha1 or git_sha1.hexdigest() == artifact.git_sha1)
    )
