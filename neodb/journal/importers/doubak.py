import datetime
import zipfile
from typing import Any

from loguru import logger

from .ndjson import NdjsonImporter


class DoubakImporter(NdjsonImporter):
    """Import a Douban archive produced by Doubak.

    Three similar names meet here: this carries Douban data, captured by
    Doubak (https://doubak.com), and is unrelated to :class:`DoubanImporter`,
    which reads a Doufen workbook.

    The archive is the same NDJSON :class:`NdjsonExporter` writes, so
    :class:`NdjsonImporter` parses it unchanged. What this adds is
    ``OVERWRITE``: Douban stamps a mark with the day it was marked rather than
    last edited, so an archive's records often look older than the shelf entry
    they should still replace.
    """

    class Meta:
        app_label = "journal"  # workaround bug in TypedModel

    MERGE = 0
    OVERWRITE = 1

    DefaultMetadata = NdjsonImporter.DefaultMetadata | {"mode": MERGE}

    #: an archive is recognised by holding this file, the same name the
    #: upload page's own format detection looks for
    JournalFile = "journal.ndjson"

    @classmethod
    def validate_file(cls, uploaded_file) -> bool:
        """Whether the upload is a zip holding a journal.ndjson.

        The seeks are the pattern :class:`NdjsonImporter` and
        :class:`CsvImporter` already use, and they are not decoration: this
        reads the upload before the view saves it, and whether that read
        leaves the pointer where it found it is the caller's business, not
        Django's. ``chunks()`` happens to rewind today.
        """
        try:
            if not zipfile.is_zipfile(uploaded_file):
                return False
            uploaded_file.seek(0)
            with zipfile.ZipFile(uploaded_file, "r") as zipref:
                return cls.JournalFile in zipref.namelist()
        except Exception as e:
            logger.error(
                f"unable to validate zip file {uploaded_file}",
                extra={"exception": e},
            )
            return False
        finally:
            try:
                uploaded_file.seek(0)
            except Exception:
                pass

    @property
    def overwrite(self) -> bool:
        """Whether records in the archive win over what is already stored."""
        return self.metadata.get("mode", self.MERGE) == self.OVERWRITE

    def _is_current(
        self,
        existing: Any,
        updated_dt: datetime.datetime | None,
        published_dt: datetime.datetime | None,
    ) -> bool:
        """Never treat the destination as current in overwrite mode.

        Every skip decision in :class:`NdjsonImporter` runs through here, so
        this one override covers marks, ratings, comments, reviews, notes and
        collections alike. Records the archive does not carry are untouched in
        either mode: this makes the archive authoritative where it speaks, not
        a replacement for the shelf.
        """
        if self.overwrite:
            return False
        return super()._is_current(existing, updated_dt, published_dt)
