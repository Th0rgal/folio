import http.client
import json
import socket
import ssl
import threading
import urllib.parse
from typing import Any, Iterator, Optional


class HttpPoolError(RuntimeError):
    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


class HttpPool:
    """Thread-safe HTTP/1.1 & TLS keep-alive connection pool using Python stdlib.
    Eliminates repeated TCP + TLS handshakes across Hindsight, Notion, and Orb Router calls,
    and transparently retries once if an upstream server closes an idle keep-alive socket.
    """

    def __init__(self, max_per_origin: int = 6) -> None:
        self._max_per_origin = max_per_origin
        self._lock = threading.Lock()
        self._pools: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}
        self._ssl_ctx: Optional[ssl.SSLContext] = None
        self._closed = False

    def _get_ssl_ctx(self) -> ssl.SSLContext:
        with self._lock:
            if self._ssl_ctx is None:
                self._ssl_ctx = ssl.create_default_context()
            return self._ssl_ctx

    def _checkout(self, scheme: str, host: str, port: int, timeout: float) -> tuple[http.client.HTTPConnection, bool]:
        key = (scheme, host, port)
        with self._lock:
            bucket = self._pools.get(key)
            if bucket:
                conn = bucket.pop()
                conn.timeout = timeout
                if conn.sock is not None:
                    try:
                        conn.sock.settimeout(timeout)
                    except OSError:
                        pass
                return conn, True
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._get_ssl_ctx()), False
        return http.client.HTTPConnection(host, port, timeout=timeout), False

    def _checkin(self, scheme: str, host: str, port: int, conn: http.client.HTTPConnection) -> None:
        if conn.sock is None:
            return
        key = (scheme, host, port)
        with self._lock:
            if self._closed:
                try:
                    conn.close()
                except Exception:
                    pass
                return
            bucket = self._pools.setdefault(key, [])
            if len(bucket) < self._max_per_origin:
                bucket.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for bucket in pools:
            for conn in bucket:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str, int, str]:
        parsed = urllib.parse.urlsplit(url)
        scheme = (parsed.scheme or "https").lower()
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return scheme, host, port, path

    def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[dict[str, str]] = None,
        payload: Optional[Any] = None,
        timeout: float = 15.0,
    ) -> Any:
        scheme, host, port, path = self._parse_url(url)
        body_bytes: Optional[bytes] = None
        req_headers = {"Connection": "keep-alive", **(headers or {})}
        if payload is not None:
            req_headers.setdefault("Content-Type", "application/json")
            body_bytes = (
                payload
                if isinstance(payload, bytes)
                else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
            req_headers["Content-Length"] = str(len(body_bytes))

        for attempt in range(2):
            conn, reused = self._checkout(scheme, host, port, timeout)
            try:
                conn.request(method, path, body=body_bytes, headers=req_headers)
                resp = conn.getresponse()
                raw_bytes = resp.read()
                status = resp.status
                ctype = resp.getheader("Content-Type") or ""
                conn_hdr = (resp.getheader("Connection") or "").lower()
                if "close" not in conn_hdr and status < 500:
                    self._checkin(scheme, host, port, conn)
                else:
                    conn.close()

                raw = raw_bytes.decode("utf-8", errors="replace")
                if status >= 400:
                    raise HttpPoolError(status, resp.reason or "Error", raw[:1000])
                if "text/event-stream" in ctype or raw.lstrip().startswith("event:") or raw.lstrip().startswith("data: "):
                    for line in raw.splitlines():
                        if line.startswith("data: "):
                            return json.loads(line[6:])
                if not raw.strip():
                    return {}
                return json.loads(raw)
            except (
                http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                BrokenPipeError,
                ConnectionResetError,
            ):
                try:
                    conn.close()
                except Exception:
                    pass
                if reused and attempt == 0:
                    continue
                raise
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                raise
        raise RuntimeError(f"Unreachable retry loop for {url}")

    def stream_sse(
        self,
        url: str,
        *,
        method: str = "POST",
        headers: Optional[dict[str, str]] = None,
        payload: Optional[Any] = None,
        timeout: float = 60.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> Iterator[str]:
        scheme, host, port, path = self._parse_url(url)
        req_headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            **(headers or {}),
        }
        body_bytes: Optional[bytes] = None
        if payload is not None:
            req_headers.setdefault("Content-Type", "application/json")
            body_bytes = (
                payload
                if isinstance(payload, bytes)
                else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
            req_headers["Content-Length"] = str(len(body_bytes))

        conn: Optional[http.client.HTTPConnection] = None
        resp: Optional[http.client.HTTPResponse] = None
        for attempt in range(2):
            conn, reused = self._checkout(scheme, host, port, timeout)
            try:
                conn.request(method, path, body=body_bytes, headers=req_headers)
                resp = conn.getresponse()
                break
            except (
                http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                BrokenPipeError,
                ConnectionResetError,
            ):
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
                if reused and attempt == 0:
                    continue
                raise
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                raise

        if conn is None or resp is None:
            raise RuntimeError(f"Failed to connect to {url}")

        try:
            if resp.status >= 400:
                err_body = resp.read().decode("utf-8", errors="replace")
                conn.close()
                raise HttpPoolError(resp.status, resp.reason or "Error", err_body[:1000])

            ctype = (resp.getheader("Content-Type") or "").lower()
            if "text/event-stream" not in ctype:
                # Non-streaming JSON fallback from an upstream provider
                raw = resp.read().decode("utf-8", errors="replace")
                self._checkin(scheme, host, port, conn)
                conn = None
                if raw.strip():
                    yield raw.strip()
                return

            while True:
                if cancel_event is not None and cancel_event.is_set():
                    conn.close()
                    conn = None
                    return
                raw_line = resp.fp.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data_str = line[5:].lstrip(" ")
                    if data_str == "[DONE]":
                        # Drain trailing newline so keep-alive socket stays clean if possible
                        conn.close()
                        conn = None
                        return
                    if data_str:
                        yield data_str
        except socket.timeout:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


SHARED_HTTP_POOL = HttpPool()
