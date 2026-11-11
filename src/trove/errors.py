"""Failure levels. Each one says how far a failure should stop the work.

A failure on one file is counted by the engine and the run moves on. The classes
here cover the wider cases.
"""


class TroveError(Exception):
    """Base class for errors reported to the user without a traceback."""


class ConfigError(TroveError):
    """The config file or a destination can't be used."""


class ShareError(TroveError):
    """This share can't be processed. A batch moves on to the next one.

    ``gone`` means the link itself looks dead rather than temporarily failing.
    """

    def __init__(self, message: str, gone: bool = False):
        super().__init__(message)
        self.gone = gone


class ManifestError(ShareError):
    """A manifest exists, or might, but couldn't be read.

    Treating that as an empty manifest would discard its recorded entries on the
    next save, so the share stops instead.
    """


class AbortRun(TroveError):
    """Every remaining share would fail too (quota, outage, refused auth)."""


class PermanentTransferError(TroveError):
    """A file's transfer failed in a way retrying won't change (401, 404)."""


class CorruptDownload(PermanentTransferError):
    """The downloaded bytes failed the site's integrity check. The partial file is
    discarded, so a later run can't resume from bad data."""


class StalePartial(TroveError):
    """The server rejected the resume offset, so the partial file is unusable."""
