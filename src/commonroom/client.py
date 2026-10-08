from __future__ import annotations

import json
from typing import Any

from commonroom.transport.base import Connection


def http_exchange(
    connection: Connection,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int = 2_000_000,
) -> tuple[int, Any]:
    """Speak one HTTP request over a transport connection.

    The Commonroom protocol is what this carries. The connection does not
    know whether it came from loopback, a private rendezvous plugin, or a
    future plugin.
    """
    payload = b""
    header_map = {"Host": "commonroom", "Connection": "close", "Accept": "application/json"}
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        header_map["Content-Type"] = "application/json"
        header_map["Content-Length"] = str(len(payload))
    if headers:
        header_map.update(headers)
    lines = [f"{method} {path} HTTP/1.1"]
    lines.extend(f"{key}: {value}" for key, value in header_map.items())
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload
    connection.send(raw)
    data = _read_http(connection, max_bytes)
    return _parse_http(data)


def _read_http(connection: Connection, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < max_bytes:
        try:
            chunk = connection.receive()
        except Exception:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        blob = b"".join(chunks)
        if _response_complete(blob):
            break
    return b"".join(chunks)


def _response_complete(blob: bytes) -> bool:
    head, separator, body = blob.partition(b"\r\n\r\n")
    if not separator:
        return False
    length = _content_length(head)
    if length is None:
        return False
    return len(body) >= length


def _content_length(head: bytes) -> int | None:
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def _parse_http(blob: bytes) -> tuple[int, Any]:
    head, separator, body = blob.partition(b"\r\n\r\n")
    if not separator:
        return 0, {"message": blob.decode("utf-8", errors="replace")}
    status_line = head.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
    parts = status_line.split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    length = _content_length(head)
    if length is not None:
        body = body[:length]
    if not body:
        return status, {}
    try:
        return status, json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return status, {"message": body.decode("utf-8", errors="replace")}
