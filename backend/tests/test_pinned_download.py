import hashlib
import io
import threading

import pytest

from backend.app.model_manager import ModelFile
from backend.app.pinned_download import DownloadIntegrityError, resumable_download


def test_pinned_model_download_resumes_a_verified_http_range(tmp_path):
    content = b"synthetic model weights"
    artifact = ModelFile("weights.bin", len(content), sha256=hashlib.sha256(content).hexdigest())
    requests = []

    def interrupted_opener(request, timeout):
        requests.append((request, timeout))
        return io.BytesIO(content[:8])

    with pytest.raises(DownloadIntegrityError, match="can be resumed"):
        resumable_download(
            (artifact,),
            tmp_path / "partial",
            lambda _artifact: "https://models.example.invalid/weights.bin",
            threading.Event(),
            lambda _count: None,
            opener=interrupted_opener,
        )

    partial = tmp_path / "partial" / "weights.bin.partial"
    assert partial.read_bytes() == content[:8]

    class RangeResponse(io.BytesIO):
        def __init__(self, body):
            super().__init__(body)
            self.status = 206
            self.headers = {"Content-Range": f"bytes 8-{len(content) - 1}/{len(content)}"}

    def resume_opener(request, timeout):
        requests.append((request, timeout))
        return RangeResponse(content[8:])

    assert resumable_download(
        (artifact,),
        tmp_path / "partial",
        lambda _artifact: "https://models.example.invalid/weights.bin",
        threading.Event(),
        lambda _count: None,
        opener=resume_opener,
    ) == len(content)

    request, timeout = requests[-1]
    assert request.get_header("Range") == "bytes=8-"
    assert timeout == 30
    assert (tmp_path / "partial" / "weights.bin").read_bytes() == content
    assert not partial.exists()
