"""Local file streaming helpers for storage presigned targets."""

from __future__ import annotations

import hashlib
import http.client
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import url2pathname, urlopen

from ..kernel.utils import ValidationError


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size_bytes += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size_bytes


def upload_file_to_target(
    *,
    upload: dict[str, Any],
    file_path: Path,
    size_bytes: int,
    content_type: str,
) -> list[dict[str, Any]] | None:
    if upload.get("url"):
        headers = {
            "Content-Length": str(size_bytes),
            "Content-Type": content_type,
        }
        checksum = upload.get("checksum_sha256")
        if checksum:
            headers["x-amz-checksum-sha256"] = str(checksum)
        put_presigned_url(
            url=str(upload["url"]),
            file_path=file_path,
            offset=0,
            length=size_bytes,
            headers=headers,
        )
        return None
    parts = upload.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValidationError("storage upload target has no url or multipart parts")
    part_size = int(upload.get("part_size") or 0)
    if part_size <= 0:
        raise ValidationError("multipart storage upload target is missing part_size")
    completed: list[dict[str, Any]] = []
    expected_parts = (size_bytes + part_size - 1) // part_size
    if len(parts) != expected_parts:
        raise ValidationError(
            f"multipart storage upload expected {expected_parts} parts, got {len(parts)}"
        )
    for part in parts:
        part_number = int(part["part_number"])
        offset = (part_number - 1) * part_size
        length = min(part_size, size_bytes - offset)
        headers = {"Content-Length": str(length)}
        response_headers = put_presigned_url(
            url=str(part["url"]),
            file_path=file_path,
            offset=offset,
            length=length,
            headers=headers,
        )
        etag = response_headers.get("etag")
        if not etag:
            raise ValidationError(
                f"multipart storage upload part {part_number} did not return an ETag"
            )
        completed.append({"part_number": part_number, "etag": etag})
    return completed


def put_presigned_url(
    *,
    url: str,
    file_path: Path,
    offset: int,
    length: int,
    headers: dict[str, str],
) -> dict[str, str]:
    parsed = urlsplit(url)
    if parsed.scheme == "file":
        target = Path(url2pathname(parsed.path))
        target.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("rb") as source, target.open("wb") as dest:
            source.seek(offset)
            copy_limited(source=source, dest=dest, length=length)
        return {}
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError(
            f"unsupported presigned storage upload URL scheme: {parsed.scheme}"
        )
    if not parsed.hostname:
        raise ValidationError("presigned storage upload URL is missing a host")
    conn_cls = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    conn = conn_cls(parsed.hostname, parsed.port, timeout=60)
    try:
        with file_path.open("rb") as source:
            source.seek(offset)
            body = _LimitedReader(source=source, length=length)
            conn.request("PUT", request_target, body=body, headers=headers)
            response = conn.getresponse()
            response_body = response.read(4096)
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status < 200 or response.status >= 300:
                detail = response_body.decode("utf-8", errors="replace").strip()
                raise ValidationError(
                    "presigned storage upload failed: "
                    f"HTTP {response.status} {response.reason}"
                    + (f": {detail}" if detail else "")
                )
            return response_headers
    finally:
        conn.close()


def download_target_to_file(*, download: dict[str, Any], path: Path) -> None:
    url = str(download.get("url") or "")
    parsed = urlsplit(url)
    if parsed.scheme == "file":
        source = Path(url2pathname(parsed.path))
        shutil.copyfile(source, path)
        return
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError(
            f"unsupported presigned storage download URL scheme: {parsed.scheme}"
        )
    with urlopen(url, timeout=60) as response, path.open("wb") as target:
        shutil.copyfileobj(response, target, length=1024 * 1024)


def copy_limited(*, source: Any, dest: Any, length: int) -> None:
    remaining = int(length)
    while remaining > 0:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        dest.write(chunk)
        remaining -= len(chunk)
    if remaining:
        raise ValidationError(
            f"storage upload source ended {remaining} bytes before expected size"
        )


class _LimitedReader:
    def __init__(self, *, source: Any, length: int) -> None:
        self.source = source
        self.remaining = int(length)

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0 or size > self.remaining:
            size = self.remaining
        data = self.source.read(size)
        self.remaining -= len(data)
        return data
