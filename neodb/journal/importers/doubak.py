import datetime
import zipfile
from typing import Any

from loguru import logger

from .ndjson import NdjsonImporter


class DoubakImporter(NdjsonImporter):
    """Import a Douban archive produced by Doubak.

    Three similar names meet here, so to be explicit: this carries **Douban**
    data, captured by **Doubak**, and is unrelated to :class:`DoubanImporter`,
    which reads a **Doufen** workbook. Same source account, different tools,
    different file formats.

    Doubak (https://doubak.com) captures a Douban account in the user's own
    browser and writes the same ``journal.ndjson`` / ``catalog.ndjson`` archive
    that :class:`NdjsonExporter` writes, so the records themselves are parsed by
    :class:`NdjsonImporter` and nothing about the format is restated here.

    What this importer adds is a choice about existing data. A Douban archive is
    a second, independent record of the same account, so the user may want it to
    defer to whatever is already on the shelf, or to replace it. ``MERGE`` is the
    inherited rule -- the newer record wins -- and ``OVERWRITE`` applies every
    record in the archive regardless of what is already there.

    The distinction matters more here than it would for a NeoDB-to-NeoDB
    migration. Douban stores current state only and stamps a mark with the day
    it was *marked*, not the day it was last edited, so an archive of a
    long-standing account is full of records that look older than a shelf entry
    the user created here last week -- while still holding the comment and
    rating they actually want.
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
        """Whether the upload is a zip holding a journal.ndjson."""
        try:
            with zipfile.ZipFile(uploaded_file) as zipref:
                return cls.JournalFile in zipref.namelist()
        except Exception as e:
            logger.error(
                f"unable to validate zip file {uploaded_file}",
                extra={"exception": e},
            )
        return False

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
