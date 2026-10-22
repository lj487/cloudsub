"""HTTP and WebDAV sources. Remote names never select local output paths."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx


MAX_DOWNLOAD_BYTES = 20 * 1024**3
MAX_XML_BYTES = 2 * 1024**2
MAX_ENTRIES = 10_000
MAX_SUBTITLE_BYTES = 16 * 1024**2
DAV = "{DAV:}"


class CloudSubError(Exception):
    """An error whose message is safe to show without remote URLs or secrets."""


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int | None = None


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    return url.scheme, url.host, url.port if url.port is not None else (443 if url.scheme == "https" else 80)


def _url(value: str, allow_http: bool) -> httpx.URL:
    try:
        parsed = urlsplit(value)
        if (
            any(ord(char) < 32 or ord(char) == 127 for char in value)
            or parsed.scheme.lower() not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "#" in value
        ):
            raise ValueError
        result = httpx.URL(value)
        if result.scheme == "http" and not allow_http:
            raise CloudSubError("HTTP requires explicit allow_http=true; prefer HTTPS.")
        return result
    except (ValueError, httpx.InvalidURL):
        raise CloudSubError("Invalid source URL; use HTTP(S) without embedded credentials or fragments.") from None


def _path(value: str) -> str:
    if not isinstance(value, str) or "\\" in value or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise CloudSubError("Invalid remote path.")
    value = value.strip("/")
    if value and any(part in ("", ".", "..") for part in value.split("/")):
        raise CloudSubError("Remote paths must stay within the configured endpoint.")
    return value


class HttpSource:
    def __init__(
        self, url: str, username: str = "", password: str = "",
        allow_http: bool = False, *, transport: httpx.BaseTransport | None = None,
    ):
        self.url = _url(url, allow_http)
        self.allow_http = allow_http
        if ":" in username:
            raise CloudSubError("Basic authentication usernames cannot contain a colon.")
        self._authorization = ""
        if username or password:
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            self._authorization = f"Basic {encoded}"
        self._client = httpx.Client(
            trust_env=False, follow_redirects=False, transport=transport,
            timeout=httpx.Timeout(60, connect=20),
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._client.close()

    @contextmanager
    def _response(self, method: str, url: httpx.URL, *, headers=None, content=None, redirects=False, accepted_statuses=()):
        response = None
        try:
            current = url
            for attempt in range(6):
                request_headers = {"Accept-Encoding": "identity", **(headers or {})}
                if self._authorization and _origin(current) == _origin(self.url):
                    request_headers["Authorization"] = self._authorization
                # Build a fresh Request: client cookies must not cross origins or ports.
                request = httpx.Request(method, current, headers=request_headers, content=content)
                response = self._client.send(request, stream=True)
                if not response.is_redirect:
                    break
                if not redirects or attempt == 5 or not response.headers.get("location"):
                    raise CloudSubError(f"{method} failed: unsupported redirect.")
                next_url = _url(urljoin(str(current), response.headers["location"]), self.allow_http)
                if current.scheme == "https" and next_url.scheme != "https":
                    raise CloudSubError("Download refused an HTTPS downgrade.")
                response.close()
                response = None
                current = next_url
            if not 200 <= response.status_code < 300 and response.status_code not in accepted_statuses:
                raise CloudSubError(f"{method} failed (HTTP {response.status_code}).")
            yield response
        except httpx.HTTPError as error:
            raise CloudSubError(f"{method} failed ({type(error).__name__}).") from None
        finally:
            if response is not None:
                response.close()

    def _download(self, url: httpx.URL, target: Path):
        target = Path(target)
        created = False
        try:
            with self._response("GET", url, redirects=True) as response:
                if response.status_code != 200:
                    raise CloudSubError(f"Download requires a complete response (HTTP {response.status_code}).")
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        length = int(length)
                    except ValueError:
                        raise CloudSubError("Download returned an invalid length.") from None
                    if not 0 <= length <= MAX_DOWNLOAD_BYTES:
                        raise CloudSubError("Download exceeds the size limit or has an invalid length.")
                with target.open("xb") as output:
                    created = True
                    total = 0
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise CloudSubError("Download exceeds the size limit.")
                        output.write(chunk)
                    if response.headers.get("content-encoding", "identity") == "identity" and length is not None and total != length:
                        raise CloudSubError("Download was incomplete.")
        except (CloudSubError, OSError) as error:
            if created:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    raise CloudSubError("Download failed and its temporary file could not be removed.") from None
            if isinstance(error, CloudSubError):
                raise
            raise CloudSubError(f"Local download failed ({type(error).__name__}).") from None

    def download(self, path: str, target: Path):
        if path:
            raise CloudSubError("HTTP sources download their configured URL; no remote path is accepted.")
        self._download(self.url, target)


class WebDAVSource(HttpSource):
    def __init__(self, url: str, username: str = "", password: str = "", allow_http: bool = False, *, transport=None):
        checked = _url(url, allow_http)
        parsed = urlsplit(str(checked))
        if parsed.query:
            raise CloudSubError("WebDAV endpoints cannot contain a query string.")
        try:
            base = _path(unquote(parsed.path, errors="strict"))
        except UnicodeError:
            raise CloudSubError("Invalid WebDAV endpoint path.") from None
        self._base_path = "/" + (base + "/" if base else "")
        endpoint = urlunsplit((parsed.scheme, parsed.netloc, quote(self._base_path, safe="/"), "", ""))
        super().__init__(endpoint, username, password, allow_http, transport=transport)

    def _resource_url(self, path: str, *, directory=False) -> httpx.URL:
        path = _path(path)
        parsed = urlsplit(str(self.url))
        encoded = quote(self._base_path + path + ("/" if directory and path else ""), safe="/")
        return httpx.URL(urlunsplit((parsed.scheme, parsed.netloc, encoded, "", "")))

    def _href_path(self, href: str, request_url: httpx.URL) -> str:
        try:
            raw_path = unquote(urlsplit(href).path, errors="strict")
        except (ValueError, UnicodeError):
            raise CloudSubError("WebDAV returned an invalid resource path.") from None
        _path(raw_path)
        resolved = _url(urljoin(str(request_url), href), self.allow_http)
        parsed = urlsplit(str(resolved))
        if _origin(resolved) != _origin(self.url) or parsed.query:
            raise CloudSubError("WebDAV returned a resource outside the configured endpoint.")
        try:
            decoded = unquote(parsed.path, errors="strict")
        except UnicodeError:
            raise CloudSubError("WebDAV returned an invalid resource path.") from None
        if decoded.rstrip("/") == self._base_path.rstrip("/"):
            return ""
        if not decoded.startswith(self._base_path):
            raise CloudSubError("WebDAV returned a resource outside the configured endpoint.")
        return _path(decoded[len(self._base_path):])

    def _entries(self, folder: str):
        url = self._resource_url(folder, directory=True)
        body = b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/></d:prop></d:propfind>'
        with self._response("PROPFIND", url, headers={"Depth": "1", "Content-Type": "application/xml"}, content=body) as response:
            if response.status_code != 207:
                raise CloudSubError(f"WebDAV listing requires HTTP 207 (received {response.status_code}).")
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_XML_BYTES:
                    raise CloudSubError("WebDAV listing exceeds the size limit.")
        if b"\x00" in data or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise CloudSubError("WebDAV listing contains unsupported XML declarations.")
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            raise CloudSubError("WebDAV returned invalid XML.") from None
        if root.tag != DAV + "multistatus":
            raise CloudSubError("WebDAV returned an invalid listing.")
        seen = set()
        for entry in root.findall(DAV + "response"):
            href = entry.findtext(DAV + "href")
            if href is None:
                raise CloudSubError("WebDAV returned a resource without a path.")
            path = self._href_path(href, url)
            if path == folder or path in seen:
                continue
            prefix = folder + "/" if folder else ""
            if not path.startswith(prefix) or "/" in path[len(prefix):]:
                raise CloudSubError("WebDAV returned a resource outside the requested directory.")
            seen.add(path)
            props = None
            for propstat in entry.findall(DAV + "propstat"):
                status = propstat.findtext(DAV + "status", "").split()
                if len(status) >= 2 and status[1].isdigit() and 200 <= int(status[1]) < 300:
                    group = propstat.find(DAV + "prop")
                    if group is not None:
                        if props is None:
                            props = ElementTree.Element(DAV + "prop")
                        props.extend(group)
            if props is None:
                continue
            directory = props.find(DAV + "resourcetype/" + DAV + "collection") is not None
            size = None if directory else props.findtext(DAV + "getcontentlength")
            if size is not None:
                try:
                    size = int(size)
                    if size < 0:
                        raise ValueError
                except ValueError:
                    raise CloudSubError("WebDAV returned an invalid resource size.") from None
            yield path, directory, size

    def files(self, folder: str = "", recursive: bool = False):
        pending = [_path(folder)]
        visited = set()
        count = 0
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            for path, directory, size in self._entries(current):
                count += 1
                if count > MAX_ENTRIES:
                    raise CloudSubError("WebDAV scan exceeds the entry limit; select a smaller directory.")
                if directory:
                    if recursive:
                        pending.append(path)
                else:
                    yield RemoteFile(path, size)

    def download(self, path: str, target: Path):
        if not _path(path):
            raise CloudSubError("Select a WebDAV file to download.")
        self._download(self._resource_url(path), target)

    def upload(self, path: str, content: bytes):
        if Path(_path(path)).suffix.lower() != ".srt":
            raise CloudSubError("Only subtitle files may be uploaded.")
        if len(content) > MAX_SUBTITLE_BYTES:
            raise CloudSubError("Subtitle exceeds the upload size limit.")
        url = self._resource_url(path)
        with self._response("PUT", url, headers={"If-None-Match": "*", "Content-Type": "application/octet-stream"}, content=content, accepted_statuses=(412,)) as response:
            conflict = response.status_code == 412
        with self._response("GET", url, redirects=True) as response:
            if response.status_code != 200:
                raise CloudSubError("Subtitle upload could not be verified.")
            actual = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                actual.extend(chunk)
                if len(actual) > MAX_SUBTITLE_BYTES:
                    raise CloudSubError("Uploaded subtitle verification exceeds the size limit.")
            if actual != content:
                if conflict:
                    raise CloudSubError("A different remote subtitle already exists; the local copy is retained.")
                raise CloudSubError("Uploaded subtitle content did not match; the local copy is retained.")
