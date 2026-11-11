"""MEGA public folder and file links, natively: no MEGAcmd and no login.

Links: ``mega.nz/folder/<id>#<key>`` (optionally ``/folder/<sub>`` after the key for a
subfolder), ``mega.nz/file/<id>#<key>``, and the older ``#F!<id>!<key>`` and
``#!<id>!<key>``. The key is in the fragment and never goes to the server, so it
must never be printed.

Listing: POST ``/cs?n=<folder id>`` with ``[{"a":"f","c":1,"r":1}]`` gives every
node. Don't send ``ca:1``: it can answer from a stale snapshot that's missing files.
A node's ``k`` is ``owner:key`` parts separated by ``/``; the part owned by the folder
link or its root decrypts with the folder key (AES-ECB). A file key is 32 bytes: the
AES key is its two halves XORed, then an 8-byte IV and an 8-byte MAC. Attributes
(``a``) are AES-CBC with a zero IV, ``MEGA{json}``.

* The file date is the mtime in the fingerprint attribute ``c``: a 16-byte CRC, one
  length byte, then a little-endian mtime. ``ts`` is the upload time and doesn't
  match files already written. Folders get no date, as MEGAcmd gave them none.
* Nodes that don't decrypt (MEGAcmd's ``NO_KEY``) can never be fetched, and
  ``.getxfer.*`` files are MEGAcmd's transfer leftovers inside some shares. Both are
  left out.
* Names are written the way MEGAcmd wrote them (naming style ``percent``), and
  existing trees are matched loosely (``Share.loose_names``), since they hold names
  in other casing, NFD, or with trailing dots and spaces.
* Downloads: ``{"a":"g","g":1,"n":<node>}`` gives a URL. The content is AES-CTR, so a
  download resumes from any offset. A download from the start is checked against
  the file's chunked CBC-MAC as it streams. A resumed one can't be, because the
  bytes before the resume point aren't read again.
* HTTP 509, or API error -17, means the transfer quota is used up, which stops the
  run. A storage server that sends nothing is usually just slow, so that download is
  tried again with a fresh URL. Only if every try stalls does the run stop, since
  that's also how the quota can show itself when there's no 509.
* API error -9 means the link is dead, and -16 that it was taken down.
"""

from __future__ import annotations

import base64
import binascii
import json
import random
import re
import struct
import time
from typing import Iterator
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from trove.errors import AbortRun, CorruptDownload, PermanentTransferError, ShareError
from trove.http import API_TIMEOUT, CHUNK_SIZE, new_session, request_with_retry
from trove.model import Listing, RemoteFile, RemoteFolder, Share, Transfer

API = "https://g.api.mega.co.nz/cs"
HOSTS = ("mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz", "mega.io", "www.mega.io")
API_RETRIES = 6
STALL_TRIES = 3  # a download that sends nothing: tries, each with a fresh URL
STALL_PAUSE = 10  # seconds between them
TRANSIENT = {-3: "try again", -4: "rate limited", -6: "too many connections", -18: "temporarily unavailable"}
GONE = {-9: "the link is dead", -16: "the link was taken down", -8: "the link has expired"}
OVER_QUOTA = -17
MAC_CHUNK_START = 128 * 1024
MAC_CHUNK_MAX = 1024 * 1024


SC = "https://g.api.mega.co.nz/sc"
MAX_PACKET_BATCHES = 200


def _apply_packet(nodes: dict[str, dict], packet: dict) -> None:
    """Apply one action packet to the nodes by handle: ``t`` adds or replaces nodes,
    ``d`` removes one with everything under it, ``u`` updates attributes. Others
    (thumbnails, shares, contacts) don't change the listing."""
    kind = packet.get("a")
    if kind == "t":
        for node in (packet.get("t") or {}).get("f", []):
            nodes[node["h"]] = node
    elif kind == "d":
        children: dict[str, list[str]] = {}
        for handle, node in nodes.items():
            children.setdefault(node.get("p"), []).append(handle)
        pending = [packet.get("n")]
        while pending:
            handle = pending.pop()
            nodes.pop(handle, None)
            pending.extend(children.get(handle, []))
    elif kind == "u" and packet.get("n") in nodes:
        node = nodes[packet["n"]]
        if "at" in packet:
            node["a"] = packet["at"]
        if packet.get("k"):
            node["k"] = packet["k"]
        if "ts" in packet:
            node["ts"] = packet["ts"]


class MegaApiError(Exception):
    def __init__(self, code: int):
        super().__init__(f"MEGA API error {code}")
        self.code = code


def b64d(text: str) -> bytes:
    text = text.replace("-", "+").replace("_", "/").replace(",", "")
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


# -- links ----------------------------------------------------------------------


def _parse(url: str) -> dict | None:
    """``{"kind", "id", "key", "sub"}``, with ``kind`` folder, file or upload."""
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if parsed.netloc.lower() not in HOSTS:
        return None
    path, fragment = parsed.path.rstrip("/"), parsed.fragment
    parts = path.split("/")
    if len(parts) >= 2 and parts[1] in ("megadrop", "filerequest"):
        return {"kind": "upload", "id": parts[2] if len(parts) > 2 else "", "key": "", "sub": ""}
    if len(parts) == 3 and parts[1] in ("folder", "file"):
        key, _, rest = fragment.partition("/")
        sub = ""
        if parts[1] == "folder" and rest:
            match = re.match(r"^(?:folder|file)/([\w-]+)", rest)
            sub = match.group(1) if match else ""
        return {"kind": parts[1], "id": parts[2], "key": key, "sub": sub}
    if path in ("", "/") and fragment.startswith(("F!", "!")):
        bits = fragment.split("!")
        if fragment.startswith("F!") and len(bits) >= 3:
            return {"kind": "folder", "id": bits[1], "key": bits[2], "sub": bits[3] if len(bits) > 3 else ""}
        if len(bits) >= 3:
            return {"kind": "file", "id": bits[1], "key": bits[2], "sub": ""}
    return None


def matches(url: str) -> bool:
    return _parse(url) is not None


def bare_url(text: str) -> str | None:
    """The link ``ID#KEY`` stands for (see providers.bare_links). A folder key is 16
    bytes (22 characters) and a file key 32 (43), so the key says which it is."""
    match = re.fullmatch(r"([\w-]{8})#([\w-]+)(/.*)?", text)
    if not match:
        return None
    kind = {22: "folder", 43: "file"}.get(len(match.group(2)))
    return f"https://mega.nz/{kind}/{text}" if kind else None


def open_share(url: str, password: str = "") -> "MegaShare":
    link = _parse(url)
    if not link:
        raise ValueError("Expected a link like https://mega.nz/folder/ID#KEY")
    if link["kind"] == "upload":
        raise ShareError("This is a MEGA upload link (a drop or file request): it takes files and lists none.")
    if password:
        raise ShareError("Password-protected MEGA links (#P!) aren't supported.")
    if not link["key"]:
        raise ShareError("This MEGA link has no key. The full link, including the part after #, is needed.")
    try:
        key = b64d(link["key"])
    except (binascii.Error, ValueError):
        raise ShareError("The key in this MEGA link isn't valid base64.") from None
    wanted = 16 if link["kind"] == "folder" else 32
    if len(key) != wanted:
        raise ShareError(f"The key in this MEGA {link['kind']} link should be {wanted} bytes, not {len(key)}.")
    return MegaShare(link["kind"], link["id"], key, link["sub"])


# -- crypto -----------------------------------------------------------------------


def _ecb_decrypt(key: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _file_aes_key(key: bytes) -> bytes:
    a = struct.unpack(">8I", key)
    return struct.pack(">4I", a[0] ^ a[4], a[1] ^ a[5], a[2] ^ a[6], a[3] ^ a[7])


def decrypt_attributes(aes_key: bytes, encrypted: str) -> dict | None:
    data = b64d(encrypted)
    data += b"\0" * (-len(data) % 16)
    decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(b"\0" * 16)).decryptor()
    raw = decryptor.update(data) + decryptor.finalize()
    if not raw.startswith(b"MEGA{"):
        return None
    try:
        return json.loads(raw[4:].rstrip(b"\0").decode("utf-8", "replace"))
    except ValueError:
        return None


def fingerprint_mtime(fingerprint: str) -> float | None:
    """The mtime in a ``c`` attribute: CRC (16 bytes), a length byte, then the
    mtime, little-endian."""
    try:
        data = b64d(fingerprint)
    except (binascii.Error, ValueError):
        return None
    if len(data) < 18 or not 1 <= data[16] <= 8 or len(data) < 17 + data[16]:
        return None
    return float(int.from_bytes(data[17 : 17 + data[16]], "little"))


def _mac_chunk_ends(size: int) -> Iterator[int]:
    """Where MEGA's MAC chunks end: 128 KiB, then 256 KiB more, growing by 128 KiB
    up to 1 MiB, then every 1 MiB."""
    end, step = 0, MAC_CHUNK_START
    while end < size:
        end = min(end + step, size)
        yield end
        step = min(step + MAC_CHUNK_START, MAC_CHUNK_MAX)


class ChunkMac:
    """MEGA's file MAC, fed as the plaintext streams.

    Each chunk is CBC-MAC'd with IV ``iv + iv`` (zero-padded at the end of the file),
    the chunk MACs are CBC-MAC'd together with a zero IV, and the 16-byte result is
    condensed to 8 by XORing word pairs.
    """

    def __init__(self, aes_key: bytes, iv: bytes, size: int):
        self.key, self.iv, self.size = aes_key, iv, size
        self.ends = _mac_chunk_ends(size)
        self.chunk_end = next(self.ends, 0)
        self.pos = 0
        self.file_mac = Cipher(algorithms.AES(aes_key), modes.CBC(b"\0" * 16)).encryptor()
        self.file_chain = b"\0" * 16
        self._new_chunk()

    def _new_chunk(self) -> None:
        self.chunk = Cipher(algorithms.AES(self.key), modes.CBC(self.iv + self.iv)).encryptor()
        self.chain = self.iv + self.iv
        self.pending = b""

    def _feed_chunk(self, data: bytes) -> None:
        data = self.pending + data
        usable = len(data) - len(data) % 16
        if usable:
            self.chain = self.chunk.update(data[:usable])[-16:]
        self.pending = data[usable:]

    def _close_chunk(self) -> None:
        if self.pending:
            self.chain = self.chunk.update(self.pending + b"\0" * (16 - len(self.pending)))[-16:]
        self.file_chain = self.file_mac.update(self.chain)
        self._new_chunk()
        self.chunk_end = next(self.ends, self.chunk_end)

    def update(self, data: bytes) -> None:
        while data:
            take = min(len(data), self.chunk_end - self.pos)
            self._feed_chunk(data[:take])
            self.pos += take
            data = data[take:]
            if self.pos == self.chunk_end:
                self._close_chunk()

    def digest(self) -> bytes:
        w = struct.unpack(">4I", self.file_chain)
        return struct.pack(">2I", w[0] ^ w[1], w[2] ^ w[3])


# -- the share --------------------------------------------------------------------


class MegaShare(Share):
    provider_name = "mega"
    colon = "percent"
    can_resume = True
    loose_names = True
    junk_prefixes = (".getxfer.",)  # MEGAcmd's transfer leftovers

    def __init__(self, kind: str, link_id: str, key: bytes, sub: str = ""):
        self.kind, self.link_id, self._key, self.sub = kind, link_id, key, sub
        self.session = new_session()
        self._seq = random.randint(0, 2**31)
        self._file_keys: dict[str, bytes] = {}  # node handle -> 32-byte file key

    def __repr__(self) -> str:
        return f"MegaShare({self.kind}={self.link_id!r})"

    @property
    def identity(self) -> str:
        return f"mega:{self.kind}:{self.link_id}" + (f"/{self.sub}" if self.sub else "")

    def describe(self) -> list[str]:
        line = f"MEGA {self.kind}: {self.link_id}"
        return [line + (f" (subfolder {self.sub})" if self.sub else "")]

    # -- API ------------------------------------------------------------------

    def _api(self, command: dict, folder: bool = True):
        params = {"id": self._seq}
        self._seq += 1
        if folder and self.kind == "folder":
            params["n"] = self.link_id
        for attempt in range(API_RETRIES):
            response = request_with_retry(self.session, "POST", API, params=params, json=[command])
            data = response.json()
            code = data if isinstance(data, int) else (data[0] if isinstance(data, list) and data and isinstance(data[0], int) else None)
            if code is None and isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("e"):
                code = data[0]["e"]
            if code is None or code >= 0:
                return data[0] if isinstance(data, list) else data
            if code in TRANSIENT and attempt < API_RETRIES - 1:
                wait = min(2**attempt, 30)
                print(f"  MEGA: {TRANSIENT[code]}; retry in {wait}s...", flush=True)
                time.sleep(wait)
                continue
            raise MegaApiError(code)
        raise AssertionError("unreachable")

    def _command(self, command: dict, folder: bool = True):
        try:
            return self._api(command, folder)
        except MegaApiError as e:
            if e.code in GONE:
                raise ShareError(f"MEGA: {GONE[e.code]}.", gone=True) from None
            if e.code == OVER_QUOTA:
                raise AbortRun("MEGA's transfer quota is used up; try again later.") from None
            raise ShareError(f"MEGA refused the request (error {e.code}).") from None

    def probe(self) -> None:
        if self.kind == "folder":
            self._command({"a": "f", "c": 1, "r": 1})
        else:
            self._command({"a": "g", "p": self.link_id}, folder=False)

    # -- listing --------------------------------------------------------------

    def list(self, subpath: str = "") -> Listing:
        if self.kind == "file":
            if subpath.strip("/"):
                raise ShareError("A MEGA file link has no folders, so --path doesn't apply.")
            return self._list_file()
        listing = self._list_folder()
        sub = subpath.strip("/")
        if sub:
            prefix = sub + "/"
            listing.files = [f for f in listing.files if f.path.startswith(prefix)]
            listing.folders = [f for f in listing.folders if f.path == sub or f.path.startswith(prefix)]
        return listing

    def _list_file(self) -> Listing:
        info = self._command({"a": "g", "p": self.link_id}, folder=False)
        attrs = decrypt_attributes(_file_aes_key(self._key), info.get("at", ""))
        if not attrs or "n" not in attrs:
            raise ShareError("Could not decrypt the file's name; the key may be wrong.")
        name = attrs["n"].replace("/", "%2f")
        mtime = fingerprint_mtime(attrs["c"]) if attrs.get("c") else None
        self._file_keys[self.link_id] = self._key
        stem = name.rsplit(".", 1)[0] if "." in name.strip(".") else name
        # A one-file share gets its own folder, so the orphan check can't scan a whole library.
        return Listing([RemoteFile(name, info.get("s"), mtime, key=self.link_id)], [], share_name=stem)

    def _nodes(self) -> list[dict]:
        """The folder's nodes as they are now: the snapshot, then every change since.

        The ``f`` answer is a snapshot that can lag behind by recent uploads, renames
        and deletions. Its ``sn`` is where the action-packet stream (``/sc``) takes
        over; replaying it until the server says it's caught up (``w``) gives what
        MEGAcmd shows.
        """
        snapshot = self._command({"a": "f", "c": 1, "r": 1})
        nodes = {n["h"]: n for n in snapshot.get("f", [])}
        sn = snapshot.get("sn")
        for _ in range(MAX_PACKET_BATCHES):
            if not sn:
                break
            response = request_with_retry(self.session, "POST", SC, params={"sn": sn, "n": self.link_id, "ssl": 1})
            data = response.json()
            if isinstance(data, int):
                if data in TRANSIENT:
                    time.sleep(2)
                    continue
                print(f"  Warning: MEGA wouldn't send changes since its snapshot (error {data}); the listing may be behind.")
                break
            for packet in data.get("a", []):
                _apply_packet(nodes, packet)
            if "w" in data or not data.get("a"):
                break
            sn = data.get("sn", sn)
        else:
            print("  Warning: MEGA kept sending changes; the listing may be behind.")
        return list(nodes.values())

    def _list_folder(self) -> Listing:
        raw = self._nodes()
        handles = {n["h"] for n in raw}
        root = next((n["h"] for n in raw if n.get("t") == 1 and n.get("p") not in handles), None)
        owners = {self.link_id, root}

        nodes: dict[str, dict] = {}
        no_key = 0
        for n in raw:
            if n.get("t") not in (0, 1):
                continue
            key = None
            for part in (n.get("k") or "").split("/"):
                owner, sep, encrypted = part.partition(":")
                if sep and owner in owners:
                    try:
                        key = _ecb_decrypt(self._key, b64d(encrypted))
                    except (binascii.Error, ValueError):
                        key = None
                    break
            attrs = None
            if key and len(key) == (32 if n["t"] == 0 else 16):
                aes_key = _file_aes_key(key) if n["t"] == 0 else key
                try:
                    attrs = decrypt_attributes(aes_key, n.get("a", ""))
                except (binascii.Error, ValueError):
                    attrs = None
            if not attrs or not isinstance(attrs.get("n"), str):
                no_key += 1
                continue
            nodes[n["h"]] = {"t": n["t"], "p": n.get("p"), "name": attrs["n"].replace("/", "%2f"), "size": n.get("s"),
                             "c": attrs.get("c"), "key": key}

        top = self.sub or root
        if top not in nodes:
            raise ShareError("Could not decrypt the folder; the key may be wrong." if raw else "The MEGA folder is empty.")

        paths: dict[str, str | None] = {top: ""}

        def path_of(handle: str) -> str | None:
            if handle in paths:
                return paths[handle]
            chain, current = [], handle
            while current not in paths:
                node = nodes.get(current)
                if node is None:
                    for h in chain:
                        paths[h] = None
                    return None
                chain.append(current)
                current = node["p"]
            base = paths[current]
            for h in reversed(chain):
                base = None if base is None else (f"{base}/{nodes[h]['name']}" if base else nodes[h]["name"])
                paths[h] = base
            return paths[handle]

        files: list[RemoteFile] = []
        folders: list[RemoteFolder] = []
        skipped = outside = 0
        for handle, node in nodes.items():
            if handle == top:
                continue
            rel = path_of(handle)
            if rel is None:
                outside += 1
                continue
            if node["t"] == 1:
                folders.append(RemoteFolder(rel))
            elif node["name"].startswith(".getxfer."):
                skipped += 1
            else:
                self._file_keys[handle] = node["key"]
                mtime = fingerprint_mtime(node["c"]) if node["c"] else None
                files.append(RemoteFile(rel, node["size"], mtime, key=handle))

        notes = []
        if no_key:
            notes.append(f"{no_key} can't be decrypted (no key)")
        if skipped:
            notes.append(f"{skipped} MEGAcmd transfer leftovers")
        if outside and not self.sub:
            notes.append(f"{outside} outside the folder")
        if notes:
            print(f"  Left out: {', '.join(notes)}")
        files.sort(key=lambda f: f.path)
        folders.sort(key=lambda f: f.path)
        return Listing(files, folders, share_name=nodes[top]["name"])

    # -- downloads ------------------------------------------------------------

    def _download_url(self, file: RemoteFile) -> str:
        if self.kind == "folder":
            info = self._command({"a": "g", "g": 1, "ssl": 2, "n": file.key})
        else:
            info = self._command({"a": "g", "g": 1, "ssl": 2, "p": file.key}, folder=False)
        url = info.get("g")
        if not url or isinstance(url, list):
            raise PermanentTransferError("MEGA gave no download URL for this file")
        return url

    def open_transfer(self, file: RemoteFile, offset: int = 0) -> Transfer:
        key = self._file_keys[file.key]
        aes_key, iv, expected_mac = _file_aes_key(key), key[16:24], key[24:32]
        start = offset - offset % 16
        # A storage server that sends nothing is usually one slow server, so ask for a
        # fresh URL (often another server) before guessing it's the quota.
        for attempt in range(STALL_TRIES):
            url = self._download_url(file)
            try:
                response = self.session.get(f"{url}/{start}-" if start else url, stream=True, timeout=API_TIMEOUT)
                break
            except requests.exceptions.ReadTimeout:
                if attempt < STALL_TRIES - 1:
                    print(f"  MEGA's storage server sent nothing; retrying with a new link in {STALL_PAUSE}s...", flush=True)
                    time.sleep(STALL_PAUSE)
        else:
            raise AbortRun(f"MEGA sent nothing for this file on {STALL_TRIES} tries. The transfer quota may be "
                           "used up, though MEGA didn't say so (it normally answers 509).")
        status = response.status_code
        if status == 509:
            response.close()
            left = response.headers.get("X-MEGA-Time-Left")
            hint = f" (about {int(left) // 60} minutes until it resets)" if left and left.isdigit() else ""
            raise AbortRun(f"MEGA's transfer quota is used up{hint}.")
        if status >= 400:
            response.close()
            if status == 429 or status >= 500:
                response.raise_for_status()
            raise PermanentTransferError(f"HTTP {status} {response.reason}")

        counter = iv + (start // 16).to_bytes(8, "big")
        decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(counter)).decryptor()
        mac = ChunkMac(aes_key, iv, file.size or 0) if offset == 0 and file.size else None
        return Transfer(self._chunks(response, decryptor, offset - start, mac, expected_mac), offset)

    def _chunks(self, response, decryptor, skip: int, mac: ChunkMac | None, expected_mac: bytes) -> Iterator[bytes]:
        received = 0
        try:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                received += len(chunk)
                plain = decryptor.update(chunk)
                if skip:
                    plain, skip = plain[skip:], max(0, skip - len(plain))
                if mac:
                    mac.update(plain)
                if plain:
                    yield plain
        except requests.exceptions.ConnectionError as e:
            if received == 0 and "timed out" in str(e).lower():
                raise AbortRun("MEGA stopped sending; the transfer quota is likely used up.") from None
            raise
        finally:
            response.close()
        # A short download is left to the engine's size check, which retries it.
        if mac and mac.pos == mac.size and mac.digest() != expected_mac:
            raise CorruptDownload("the download doesn't match MEGA's MAC")
