"""Links read from a landing document. The PDF is built here."""

from __future__ import annotations

import zlib

import pytest

from conftest import FakeShare
from trove import documents
from trove.errors import ShareError
from trove.model import Listing, RemoteFile
from trove.providers import select_share


def landing_pdf() -> bytes:
    compressed = zlib.compress(b"<< /A << /S /URI /URI (https://short.example/inner\\051) >> >>")
    return (
        b"%PDF-1.4\n1 0 obj << /A << /S /URI /URI (https://donate.example/give) >> >> endobj\n"
        b"2 0 obj << /A << /S /URI /URI <68747470733a2f2f6b2e6578616d706c652f6e6577> >> >> endobj\n"  # https://k.example/new
        b"3 0 obj << /Length 9 /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream endobj\n%%EOF"
    )


def test_links_are_found_in_literal_hex_and_compressed_form():
    assert documents.pdf_links(landing_pdf()) == [
        "https://donate.example/give",
        "https://k.example/new",
        "https://short.example/inner)",
    ]


def test_the_link_is_picked_by_host_including_subdomains():
    links = ["https://donate.example/x", "https://app.k.example/links/1", "https://k.example/2"]
    assert documents.pick_link(links, ["k.example"]) == "https://app.k.example/links/1"
    with pytest.raises(ShareError):
        documents.pick_link(links, ["other.example"])


def test_escapes_in_literal_strings():
    assert documents._unescape(rb"a\(b\)c\\d\101\
e") == b"a(b)c\\dAe"


class DocumentShare(FakeShare):
    def __init__(self, data: bytes, files: int = 1):
        super().__init__({f"doc{i}.pdf": data for i in range(files)})

    def list(self, subpath=""):
        return Listing([RemoteFile(p, len(d)) for p, d in self.contents.items()], [])


def fake_open(monkeypatch, share):
    import trove.providers as providers

    monkeypatch.setattr(providers, "open_url", lambda url, *a, **k: share)
    # Lands on a share, which a document is read from, rather than on a web page.
    monkeypatch.setattr("trove.http.resolve_shortener", lambda url, is_final, **k: "https://doc.example/s/landing")


def test_link_in_document_reads_the_one_file_and_returns_its_link(monkeypatch):
    fake_open(monkeypatch, DocumentShare(landing_pdf()))
    assert documents.link_in_document("https://stable.example/landing", ["k.example"]) == "https://k.example/new"


@pytest.mark.parametrize("share", [DocumentShare(b"not a pdf"), DocumentShare(landing_pdf(), files=2)])
def test_a_document_that_isnt_one_pdf_is_refused(monkeypatch, share):
    fake_open(monkeypatch, share)
    with pytest.raises(ShareError):
        documents.link_in_document("https://stable.example/landing", ["k.example"])


def test_a_failing_document_falls_back_to_the_last_known_link(monkeypatch, capsys):
    import trove.providers as providers

    def broken(url, hosts):
        raise ShareError("the landing document is gone", gone=True)

    opened = []
    monkeypatch.setattr(documents, "link_in_document", broken)
    monkeypatch.setattr(providers, "resolve_shortener", lambda url, is_final, **k: url)
    monkeypatch.setattr(providers, "open_url", lambda url, *a, **k: opened.append(url) or FakeShare())
    share = select_share(["https://stable.example/landing", "https://share.example/s/last"], link_in_document=["k.example"])
    assert opened == ["https://share.example/s/last"] and isinstance(share, FakeShare)
    assert "landing document is gone" in capsys.readouterr().out


LANDING_PAGE = """<html><body>
<a href="#top">top</a> <a href='mailto:x@example.com'>mail</a>
<a href="/style.css">style</a>
<a href="https://www.k.example/folder/abc/Share_Thread?x=1&amp;y=2">Share Thread Folder Link</a>
</body></html>"""


def test_hrefs_on_a_page_are_absolute_and_unescaped():
    assert documents.page_links(LANDING_PAGE, "https://page.example/") == [
        "https://page.example/style.css",
        "https://www.k.example/folder/abc/Share_Thread?x=1&y=2",
    ]


def test_a_shortener_leading_to_a_web_page_takes_its_link(monkeypatch):
    walked = []

    def resolve(url, is_final, **kwargs):
        walked.append(kwargs.get("landing_page"))
        return "https://page.example/"

    monkeypatch.setattr("trove.http.resolve_shortener", resolve)
    monkeypatch.setattr(documents, "_fetch_page", lambda url: LANDING_PAGE)
    link = documents.link_in_document("https://short.example/Landing", ["k.example"])
    assert link == "https://www.k.example/folder/abc/Share_Thread?x=1&y=2" and walked == [True]


def test_a_page_without_a_link_on_the_hosts_fails(monkeypatch):
    monkeypatch.setattr("trove.http.resolve_shortener", lambda url, is_final, **k: "https://page.example/")
    monkeypatch.setattr(documents, "_fetch_page", lambda url: LANDING_PAGE)
    with pytest.raises(ShareError, match="no link on other.example"):
        documents.link_in_document("https://short.example/Landing", ["other.example"])
