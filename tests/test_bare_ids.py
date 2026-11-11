"""Bare IDs: only taken when a provider is named, or a configured site uses them."""

from __future__ import annotations

import pytest

from trove.errors import ShareError
from trove.providers import bare_links, gofile, is_provider_url, open_url

FOLDER_KEY, FILE_KEY = "A" * 22, "B" * 43


def test_each_provider_reads_its_bare_form():
    assert bare_links("Ima81tmu", "gofile") == ["https://gofile.io/d/Ima81tmu"]
    assert bare_links("M77vtAp9zG2", "workupload") == ["https://workupload.com/file/M77vtAp9zG2"]
    assert bare_links("AbCd1234", "ufile") == ["https://ufile.io/AbCd1234"]
    assert bare_links("f/some-folder", "ufile") == ["https://ufile.io/f/some-folder"]
    assert bare_links(f"NttDRITT#{FOLDER_KEY}", "mega") == [f"https://mega.nz/folder/NttDRITT#{FOLDER_KEY}"]
    assert bare_links(f"NttDRITT#{FILE_KEY}", "mega") == [f"https://mega.nz/file/NttDRITT#{FILE_KEY}"]
    assert bare_links("NttDRITT#short", "mega") == []
    assert bare_links("Ima81tmu", "koofr") == []


def test_without_a_provider_every_reading_is_offered_but_none_is_detected():
    links = bare_links("AbCd1234")
    assert "https://gofile.io/d/AbCd1234" in links and "https://ufile.io/AbCd1234" in links
    assert not is_provider_url("AbCd1234") and bare_links("gofile.io/d/AbCd1234") == []


def test_open_url_takes_a_bare_id_with_the_provider_named():
    assert open_url("AbC123", provider="gofile").identity == "gofile:AbC123"
    assert open_url(f"NttDRITT#{FOLDER_KEY}", provider="mega").identity == "mega:folder:NttDRITT"
    assert open_url("M77vtAp9zG2", provider="workupload").identity == "workupload:M77vtAp9zG2"
    assert open_url("AbCd1234", provider="ufile").identity == "ufile:file:AbCd1234"
    with pytest.raises(ShareError):
        open_url("AbC123")
    with pytest.raises(ValueError):
        gofile.open_share("AbC123")
