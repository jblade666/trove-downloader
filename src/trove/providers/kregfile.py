"""kregfile rooms.

kregfile is a chat room with temporary file sharing, run on many hosts, often on a
non-standard port. A link looks like ``https://<host>[:port]/r/<roomid>``.

* The file list doesn't come over HTTP. The room page sets a ``kft`` cookie and names
  the client version, then a socket.io v4 connection at path ``/w`` sends one
  ``files`` snapshot ``{replace: true, files: [...]}``. Later updates are ignored.
  The room's name comes in the ``config`` event. This needs the optional
  ``python-socketio[client]`` package.
* A room is flat. Two uploads can share a name, so names stay stable by key in the
  manifest (``Share.stable_names``). Files download by key from ``/g/<key>``, which
  honours Range.
* Each file has a blake2b-240 hash in base64, so ``--verify`` checks content.
* Dates are the upload time, in milliseconds.
* Files expire, so orphans are normal here.
* Many hosts serve a valid certificate without its intermediate, which no client can
  verify. ``_resolve_verify`` fetches the missing intermediates from the certificate's
  "CA Issuers" URL and verifies against certifi's roots plus those, caching the
  bundle per host. Verification is never turned off. The bundle is passed on every
  request, because ``REQUESTS_CA_BUNDLE`` in the environment would otherwise
  override the session's, and the socket gets it through ``http_session``.
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import threading
from urllib.parse import quote, urlencode, urlparse

import certifi
import requests
from platformdirs import user_cache_dir

from trove.errors import ShareError
from trove.http import API_TIMEOUT, DEFAULT_UA, new_session, open_http_transfer, request_with_retry
from trove.model import Listing, RemoteFile, Share, Transfer

_ROOM_PATH = re.compile(r"^/r/([A-Za-z0-9_-]+)/?$")
SOCKET_TIMEOUT = 25  # seconds to connect
SNAPSHOT_TIMEOUT = 30  # seconds to wait for the file list after connecting


def _parse(url: str) -> tuple[str, str, str] | None:
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query:
        return None
    match = _ROOM_PATH.match(parsed.path)
    return (parsed.scheme, parsed.netloc.lower(), match.group(1)) if match else None


def matches(url: str) -> bool:
    return _parse(url) is not None


def open_share(url: str, password: str = "") -> "KregfileShare":
    parts = _parse(url)
    if not parts:
        raise ValueError("Expected a room link like https://host.example:8443/r/ROOMID")
    if password:
        raise ShareError("kregfile rooms don't take a password; invite-only rooms aren't supported.")
    return KregfileShare(*parts)


def _remote_name(entry: dict) -> str:
    """A room is flat, so a slash in an upload's name is part of the name."""
    return (entry.get("name") or entry["key"]).replace("/", "_")


class KregfileShare(Share):
    provider_name = "kregfile"
    colon = "underscore"
    manifest_name = ".kregfile-manifest.json"
    verify_kind = "blake2b-240"
    can_resume = True
    stable_names = True

    def __init__(self, scheme: str, host: str, room_id: str):
        self.scheme, self.host, self.room_id = scheme, host, room_id
        self.base = f"{scheme}://{host}"
        self.session = new_session()
        self._verify: str | bool | None = None

    @property
    def identity(self) -> str:
        return f"kregfile:{self.host}/{self.room_id}"

    def describe(self) -> list[str]:
        return [f"kregfile host: {self.host}", f"Room: {self.room_id}"]

    @property
    def verify(self) -> str | bool:
        if self._verify is None:
            self._verify = _resolve_verify(self.host) if self.scheme == "https" else True
            self.session.verify = self._verify
        return self._verify

    def _room_page(self) -> str:
        """Load the room page, which sets the ``kft`` cookie. Returns the client version."""
        response = request_with_retry(self.session, "GET", f"{self.base}/r/{self.room_id}", verify=self.verify)
        if not self.session.cookies.get("kft"):
            raise ShareError("The room page set no kft cookie; the room may not exist or may be invite-only.")
        match = re.search(r"client\.js\?v=([0-9a-zA-Z]+)", response.text)
        return match.group(1) if match else ""

    def probe(self) -> None:
        self._room_page()

    def list(self, subpath: str = "") -> Listing:
        if subpath.strip("/"):
            raise ShareError("kregfile rooms have no folders, so --path doesn't apply.")
        client_version = self._room_page()
        entries, room_name = self._snapshot(client_version)
        entries.sort(key=lambda e: e.get("uploaded") or 0)  # the order that decides who keeps a shared name
        files = [
            RemoteFile(
                _remote_name(e),
                e.get("size"),
                e["uploaded"] / 1000.0 if e.get("uploaded") else None,
                e.get("hash"),
                key=e["key"],
            )
            for e in entries
        ]
        print(f"  Files in room: {len(files)}")
        return Listing(files, [], share_name=room_name or None)

    def _snapshot(self, client_version: str) -> tuple[list[dict], str]:
        try:
            import socketio
        except ImportError:
            raise ShareError(
                "kregfile needs python-socketio: pip install 'trove-downloader[kregfile]'"
            ) from None

        query = {"roomid": self.room_id}
        if client_version:
            query["cv"] = client_version
        headers = {
            "Cookie": "; ".join(f"{c.name}={c.value}" for c in self.session.cookies),
            "User-Agent": DEFAULT_UA,
        }
        # http_session hands the socket the same CA bundle as HTTP (engine.io uses a
        # string session.verify as the websocket's ca_certs).
        client = socketio.Client(
            ssl_verify=bool(self.verify),
            http_session=self.session,
            reconnection=False,
            request_timeout=SOCKET_TIMEOUT,
        )
        state: dict = {"files": None, "config": {}, "error": None}
        ready = threading.Event()

        @client.on("config")
        def _config(config):
            # Sent as a list of [key, value] pairs, possibly more than once.
            try:
                state["config"].update(dict(config))
            except (TypeError, ValueError):
                pass

        @client.on("files")
        def _files(payload):
            if isinstance(payload, dict) and payload.get("replace"):
                state["files"] = payload.get("files") or []
                ready.set()

        @client.event
        def connect_error(data):
            state["error"] = data
            ready.set()

        try:
            client.connect(
                f"{self.base}/?{urlencode(query)}",
                socketio_path="/w",
                transports=["websocket"],
                headers=headers,
                wait_timeout=SOCKET_TIMEOUT,
            )
        except socketio.exceptions.ConnectionError as e:
            raise ShareError(f"Could not connect to the room: {e}") from None
        try:
            ready.wait(timeout=SNAPSHOT_TIMEOUT)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

        if state["error"] is not None:
            raise ShareError(f"The room refused the connection: {state['error']}")
        if state["files"] is None:
            raise ShareError("Connected, but the room never sent its file list.")
        return list(state["files"]), str(state["config"].get("roomname") or "")

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        # The server finds the file by key; the name only makes the URL readable.
        name = quote(file.path, safe="")
        return open_http_transfer(self.session, f"{self.base}/g/{file.key}/{name}", offset, verify=self.verify)


# -- TLS chain repair ---------------------------------------------------------


def _split_host(host: str) -> tuple[str, int]:
    name, sep, port = host.rpartition(":")
    return (name, int(port)) if sep and port.isdigit() else (host, 443)


def _verifies(host: str, port: int, cafile: str) -> bool:
    context = ssl.create_default_context(cafile=cafile)
    try:
        with socket.create_connection((host, port), timeout=15) as sock:
            with context.wrap_socket(sock, server_hostname=host):
                return True
    except (ssl.SSLError, OSError):
        return False


def _issuer_urls(der: bytes) -> list[str]:
    """The certificate's "CA Issuers" URLs. They're plain ASCII in the DER, so a scan
    finds them without an ASN.1 parser."""
    found = re.findall(rb"https?://[\x21-\x7e]+?\.(?:crt|cer)", der)
    return list(dict.fromkeys(u.decode("ascii") for u in found))


def _intermediates(host: str, port: int, max_depth: int = 5) -> list[str]:
    """Walk up from the leaf through each certificate's issuer URL."""
    der = ssl.PEM_cert_to_DER_cert(ssl.get_server_certificate((host, port), timeout=15))
    pems: list[str] = []
    seen: set[str] = set()
    for _ in range(max_depth):
        urls = [u for u in _issuer_urls(der) if u not in seen]
        if not urls:
            break
        seen.add(urls[0])
        response = requests.get(urls[0], timeout=API_TIMEOUT, headers={"User-Agent": DEFAULT_UA})
        response.raise_for_status()
        data = response.content
        der = data if data[:1] == b"\x30" else ssl.PEM_cert_to_DER_cert(data.decode("ascii", "ignore"))
        pems.append(ssl.DER_cert_to_PEM_cert(der))
    return pems


def _cache_path(host: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]", "_", host)
    return os.path.join(user_cache_dir("trove-downloader", appauthor=False), "ca", f"{safe}.pem")


def _resolve_verify(host_port: str) -> str:
    """A CA bundle path that verifies this host, repairing a missing intermediate."""
    host, port = _split_host(host_port)
    roots = certifi.where()
    if _verifies(host, port, roots):
        return roots
    cached = _cache_path(host_port)
    if os.path.exists(cached) and _verifies(host, port, cached):
        return cached

    print("  The server's certificate chain is incomplete; fetching the missing intermediate...")
    try:
        extras = _intermediates(host, port)
    except (OSError, ValueError, requests.exceptions.RequestException) as e:
        raise ShareError(f"Could not repair the server's certificate chain: {e}") from None
    if extras:
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        tmp = cached + ".tmp"
        with open(tmp, "w", encoding="ascii") as fp:
            fp.write(certifi.contents().rstrip("\n") + "\n")
            for pem in extras:
                fp.write(pem if pem.endswith("\n") else pem + "\n")
        os.replace(tmp, cached)
        if _verifies(host, port, cached):
            print("  Certificate chain repaired and verified.")
            return cached
    raise ShareError("Could not verify the server's certificate, even after fetching its intermediates.")
