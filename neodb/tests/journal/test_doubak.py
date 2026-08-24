import json
import os
import zipfile
from tempfile import TemporaryDirectory

import pytest
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from catalog.models import Edition, ExternalResource, IdType, Movie
from journal.exporters import NdjsonExporter
from journal.importers import CsvImporter, DoubakImporter, NdjsonImporter
from journal.models import (
    Article,
    Collection,
    Comment,
    Mark,
    Note,
    Rating,
    Review,
    ShelfLogEntry,
    ShelfType,
    Tag,
    TagMember,
)
from users.models import Task, User

OLD = "2021-01-01T00:00:00Z"
NEW = "2023-01-01T00:00:00Z"


def write_archive(directory: str, catalog: list[dict], journal: list[dict]) -> str:
    """Write a Doubak-shaped NDJSON zip.

    The header line is what ``parse_header`` looks for (it only requires a
    truthy ``server``), and ``parse_catalog`` skips any line without an ``id``,
    so the same header is safe at the top of both files.
    """
    header = {"server": "doubak.com", "generator": "doubak-export-adapters"}
    path = os.path.join(directory, "doubak.zip")
    with zipfile.ZipFile(path, "w") as zipref:
        for name, records in (("catalog.ndjson", catalog), ("journal.ndjson", journal)):
            body = "".join(json.dumps(r) + "\n" for r in [header] + records)
            zipref.writestr(name, body)
    return path


@pytest.mark.django_db(databases="__all__")
class TestDoubakImportMode:
    """MERGE keeps whichever record is newer; OVERWRITE applies every record."""

    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.movie = Movie.objects.create(
            localized_title=[{"lang": "en", "text": "Inception"}],
            primary_lookup_id_type=IdType.IMDB,
            primary_lookup_id_value="tt1375666",
        )
        self.user = User.register(email="doubak@test.com", username="doubak_importer")
        self.owner = self.user.identity
        self.url = self.movie.absolute_url

    def catalog(self):
        return [{"id": self.url, "type": "Movie", "title": "Inception"}]

    def shelf_member(self, status="complete", published=OLD):
        return {
            "type": "ShelfMember",
            "metadata": {},
            "content": {
                "type": "Status",
                "status": status,
                "published": published,
                "withRegardTo": self.url,
            },
        }

    def comment(self, text, published=OLD):
        return {
            "type": "Comment",
            "metadata": {},
            "content": {
                "type": "Comment",
                "content": text,
                "published": published,
                "withRegardTo": self.url,
            },
        }

    def rating(self, value, published=OLD):
        return {
            "type": "Rating",
            "metadata": {},
            "content": {
                "type": "Rating",
                "best": 10,
                "worst": 1,
                "value": value,
                "published": published,
                "withRegardTo": self.url,
            },
        }

    def review(self, title, body, published=OLD):
        return {
            "type": "Review",
            "metadata": {},
            "content": {
                "type": "Review",
                "name": title,
                "content": body,
                "mediaType": "text/markdown",
                "published": published,
                "withRegardTo": self.url,
            },
        }

    def run(self, journal, mode=None, catalog=None):
        with TemporaryDirectory() as d:
            path = write_archive(d, catalog or self.catalog(), journal)
            kwargs = {} if mode is None else {"mode": mode}
            importer = DoubakImporter.create(
                user=self.user, file=path, visibility=0, **kwargs
            )
            importer.run()
            return importer

    def existing_mark(self, status=ShelfType.WISHLIST, published=NEW, comment=None):
        mark = Mark(self.owner, self.movie)
        mark.update(
            shelf_type=status,
            comment_text=comment,
            created_time=parse_datetime(published),
        )
        return mark

    # -- marks ------------------------------------------------------------

    def test_mode_defaults_to_merge(self):
        importer = DoubakImporter.create(user=self.user, file="x.zip", visibility=0)
        assert importer.metadata["mode"] == DoubakImporter.MERGE
        assert importer.overwrite is False

    def test_merge_keeps_the_newer_existing_mark(self):
        self.existing_mark(status=ShelfType.WISHLIST, published=NEW)
        self.run([self.shelf_member(status="complete", published=OLD)])
        assert Mark(self.owner, self.movie).shelf_type == ShelfType.WISHLIST

    def test_overwrite_replaces_the_newer_existing_mark(self):
        self.existing_mark(status=ShelfType.WISHLIST, published=NEW)
        self.run(
            [self.shelf_member(status="complete", published=OLD)],
            mode=DoubakImporter.OVERWRITE,
        )
        assert Mark(self.owner, self.movie).shelf_type == ShelfType.COMPLETE

    def test_merge_still_applies_a_newer_record(self):
        # merge is not "never touch what is there" -- a newer archive record
        # wins, which is the inherited rule and must survive the subclass
        self.existing_mark(status=ShelfType.WISHLIST, published=OLD)
        self.run([self.shelf_member(status="complete", published=NEW)])
        assert Mark(self.owner, self.movie).shelf_type == ShelfType.COMPLETE

    # -- ratings and comments ---------------------------------------------

    def test_overwrite_replaces_a_newer_rating(self):
        Rating.objects.create(
            owner=self.owner,
            item=self.movie,
            grade=4,
            created_time=parse_datetime(NEW),
        )
        self.run([self.rating(10, published=OLD)], mode=DoubakImporter.OVERWRITE)
        assert Rating.objects.get(owner=self.owner, item=self.movie).grade == 10

    def test_merge_keeps_a_newer_rating(self):
        Rating.objects.create(
            owner=self.owner,
            item=self.movie,
            grade=4,
            created_time=parse_datetime(NEW),
        )
        self.run([self.rating(10, published=OLD)])
        assert Rating.objects.get(owner=self.owner, item=self.movie).grade == 4

    def test_overwrite_replaces_a_newer_comment(self):
        self.existing_mark(published=NEW, comment="written here")
        self.run(
            [self.comment("from the archive", published=OLD)],
            mode=DoubakImporter.OVERWRITE,
        )
        assert (
            Comment.objects.get(owner=self.owner, item=self.movie).text
            == "from the archive"
        )

    # -- reviews -----------------------------------------------------------

    def test_merge_keeps_the_newer_existing_review(self):
        Review.objects.create(
            owner=self.owner,
            item=self.movie,
            title="mine",
            body="written here",
            created_time=parse_datetime(NEW),
        )
        self.run([self.review("mine", "from the archive", published=OLD)])
        assert Review.objects.get(owner=self.owner, item=self.movie).body == (
            "written here"
        )

    def test_overwrite_replaces_the_newer_existing_review(self):
        Review.objects.create(
            owner=self.owner,
            item=self.movie,
            title="mine",
            body="written here",
            created_time=parse_datetime(NEW),
        )
        self.run(
            [self.review("mine", "from the archive", published=OLD)],
            mode=DoubakImporter.OVERWRITE,
        )
        assert Review.objects.get(owner=self.owner, item=self.movie).body == (
            "from the archive"
        )

    # -- notes -------------------------------------------------------------

    def test_a_note_is_identified_by_its_published_time(self):
        # NdjsonImporter keys a note on (owner, item, created_time) because a
        # user may hold several notes on one item. Re-importing the same
        # archive must therefore not stack copies, in either mode.
        note = {
            "type": "Note",
            "metadata": {},
            "content": {
                "type": "Note",
                "title": "ch. 1",
                "content": "some thoughts",
                "sensitive": False,
                "published": OLD,
                "withRegardTo": self.url,
            },
        }
        self.run([note])
        self.run([note], mode=DoubakImporter.OVERWRITE)
        assert Note.objects.filter(owner=self.owner, item=self.movie).count() == 1

    # -- the parent importer is untouched ----------------------------------

    def test_ndjson_importer_is_unaffected_by_the_override(self):
        # NdjsonImporter has no mode, so it must keep merging even when handed
        # an archive that the subclass would overwrite with
        self.existing_mark(status=ShelfType.WISHLIST, published=NEW)
        with TemporaryDirectory() as d:
            path = write_archive(
                d, self.catalog(), [self.shelf_member("complete", OLD)]
            )
            NdjsonImporter.create(user=self.user, file=path, visibility=0).run()
        assert Mark(self.owner, self.movie).shelf_type == ShelfType.WISHLIST

    # -- progress ----------------------------------------------------------

    def test_every_record_is_counted(self):
        importer = self.run(
            [
                self.shelf_member(),
                self.rating(8),
                self.comment("hi"),
                self.review("t", "b"),
            ]
        )
        assert importer.metadata["total"] == 4
        assert importer.metadata["processed"] == 4

    def test_progress_is_saved_as_it_goes_not_only_at_the_end(self):
        # the poller re-reads the row; counters kept only in memory would leave
        # the bar at zero for the whole run and then jump straight to done
        importer = self.run([self.shelf_member(), self.rating(8)])
        stored = DoubakImporter.objects.get(pk=importer.pk)
        assert stored.metadata["processed"] == 2
        assert stored.metadata["total"] == 2

    def test_status_endpoint_renders_the_bar(self, client):
        # run() is called directly rather than through _execute, so the task is
        # still pending -- which is what the mid-run render looks like
        importer = self.run([self.shelf_member(), self.rating(8)])
        client.force_login(self.user, backend="mastodon.auth.OAuth2Backend")
        response = client.get(reverse("users:user_task_status", args=[importer.type]))
        assert response.status_code == 200
        assert '<progress value="2" max="2">' in response.content.decode()

    def test_type_is_the_string_the_status_view_matches_on(self):
        # users.views.data.user_task_status matches on this literal; a rename
        # would leave the progress bar permanently blank and raise nothing
        importer = DoubakImporter.create(user=self.user, file="x.zip", visibility=0)
        assert importer.type == "journal.doubakimporter"


@pytest.mark.django_db(databases="__all__")
class TestDoubakValidateFile:
    def test_accepts_an_archive_holding_a_journal(self):
        with TemporaryDirectory() as d:
            path = write_archive(d, [], [])
            with open(path, "rb") as f:
                assert DoubakImporter.validate_file(f) is True

    def test_rejects_a_zip_without_one(self):
        # the upload page's own format detection looks for exactly this name,
        # so a zip without it could not be submitted through the UI either
        with TemporaryDirectory() as d:
            path = os.path.join(d, "other.zip")
            with zipfile.ZipFile(path, "w") as zipref:
                zipref.writestr("notes.txt", "hello")
            with open(path, "rb") as f:
                assert DoubakImporter.validate_file(f) is False

    def test_rejects_a_csv_archive(self):
        # a Doubak CSV export is a different shape; NdjsonImporter would find
        # no journal.ndjson and fail with a bare message instead of a 400
        with TemporaryDirectory() as d:
            path = os.path.join(d, "csv.zip")
            with zipfile.ZipFile(path, "w") as zipref:
                zipref.writestr("movie_mark.csv", "title,info,links\n")
            with open(path, "rb") as f:
                assert DoubakImporter.validate_file(f) is False

    def test_rejects_something_that_is_not_a_zip(self):
        with TemporaryDirectory() as d:
            path = os.path.join(d, "not.zip")
            with open(path, "w") as f:
                f.write("not a zip at all")
            with open(path, "rb") as f:
                assert DoubakImporter.validate_file(f) is False

    def test_rejects_a_nested_journal(self):
        # run() opens <tempdir>/journal.ndjson and nothing else, so a nested
        # copy would validate and then import nothing. The upload page's own
        # detection keys on the same root-level name (`name === 'journal.ndjson'`),
        # so this is not a hypothetical shape -- it is what a user gets by
        # zipping the folder instead of its contents.
        with TemporaryDirectory() as d:
            path = os.path.join(d, "nested.zip")
            with zipfile.ZipFile(path, "w") as zipref:
                zipref.writestr("export/journal.ndjson", "{}")
            with open(path, "rb") as f:
                assert DoubakImporter.validate_file(f) is False

    def test_rejects_a_missing_file(self):
        assert DoubakImporter.validate_file(None) is False


@pytest.mark.django_db(databases="__all__")
class TestImportDoubakView:
    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.user = User.register(email="view@test.com", username="doubak_view")

    def upload(self, client, path, **post):
        client.force_login(self.user, backend="mastodon.auth.OAuth2Backend")
        with open(path, "rb") as f:
            return client.post(
                reverse("users:import_doubak"), {"file": f, **post}, follow=False
            )

    def latest(self):
        return DoubakImporter.latest_task(self.user)

    def test_import_mode_reaches_the_task(self, client):
        with TemporaryDirectory() as d:
            path = write_archive(d, [], [])
            resp = self.upload(client, path, import_mode="1", visibility="0")
        assert resp.status_code == 302
        assert self.latest().metadata["mode"] == DoubakImporter.OVERWRITE

    def test_omitting_import_mode_merges(self, client):
        with TemporaryDirectory() as d:
            path = write_archive(d, [], [])
            self.upload(client, path, visibility="0")
        assert self.latest().metadata["mode"] == DoubakImporter.MERGE

    def test_visibility_reaches_the_task(self, client):
        # the stock NeoDB archive form hides its visibility radios as soon as
        # it detects ndjson, so this form is the only way to pick one
        with TemporaryDirectory() as d:
            path = write_archive(d, [], [])
            self.upload(client, path, visibility="2")
        assert self.latest().metadata["visibility"] == 2

    def test_a_zip_without_a_journal_is_rejected(self, client):
        with TemporaryDirectory() as d:
            path = os.path.join(d, "bad.zip")
            with zipfile.ZipFile(path, "w") as zipref:
                zipref.writestr("readme.txt", "nothing here")
            resp = self.upload(client, path, visibility="0")
        assert resp.status_code == 400
        assert self.latest() is None

    def test_get_redirects_instead_of_importing(self, client):
        client.force_login(self.user, backend="mastodon.auth.OAuth2Backend")
        resp = client.get(reverse("users:import_doubak"))
        assert resp.status_code == 302
        assert self.latest() is None

    def test_anonymous_upload_lands_on_the_login_page(self, client):
        with TemporaryDirectory() as d:
            path = write_archive(d, [], [])
            with open(path, "rb") as f:
                resp = client.post(reverse("users:import_doubak"), {"file": f})
        assert resp.status_code == 302
        assert reverse("users:login") in resp.headers["Location"]


@pytest.mark.django_db(databases="__all__")
class TestDoubakAgreesWithCsv:
    """The same account, described twice, must land the same way.

    Only the CSV path has been imported for real, which makes it the
    known-good side: a field that lands differently under NDJSON is a
    verified behaviour that changed with the format, and neither format's
    own tests would notice. Both archives are written from one description
    so they cannot drift apart.
    """

    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.movie = Movie.objects.create(
            localized_title=[{"lang": "en", "text": "Inception"}],
            primary_lookup_id_type=IdType.IMDB,
            primary_lookup_id_value="tt1375666",
        )
        self.csv_user = User.register(email="agree_csv@test.com", username="agree_csv")
        self.nd_user = User.register(email="agree_nd@test.com", username="agree_nd")
        self.url = self.movie.absolute_url
        # deliberately awkward: a comma, a quote, a newline and a pipe. The
        # first three are what CSV quoting has to survive and the fourth is
        # what NeoDB's parse_tags splits on -- so a tag may not contain one.
        self.comment = 'first line, with a "quote"\nsecond line'
        self.tags = ["sci-fi", "2010", "重看"]
        self.marked_at = "2019-05-04T00:00:00Z"
        self.review_title = "a title that must survive"
        self.review_body = "para one\n\npara two"

    def csv_archive(self, directory: str) -> str:
        import csv as csvmod
        import io

        path = os.path.join(directory, "csv.zip")
        with zipfile.ZipFile(path, "w") as zipref:
            buf = io.StringIO()
            w = csvmod.writer(buf)
            w.writerow(
                [
                    "title",
                    "info",
                    "links",
                    "timestamp",
                    "status",
                    "rating",
                    "comment",
                    "tags",
                ]
            )
            w.writerow(
                [
                    "Inception",
                    "imdb:tt1375666",
                    self.url,
                    self.marked_at,
                    "complete",
                    "8",
                    self.comment,
                    "|".join(self.tags),
                ]
            )
            zipref.writestr("movie_mark.csv", buf.getvalue())
            buf = io.StringIO()
            w = csvmod.writer(buf)
            # the duplicate "title" is not a typo: DictReader is last-wins, so
            # the review title is the later column
            w.writerow(["title", "info", "links", "timestamp", "title", "content"])
            w.writerow(
                [
                    "Inception",
                    "",
                    self.url,
                    self.marked_at,
                    self.review_title,
                    self.review_body,
                ]
            )
            zipref.writestr("movie_review.csv", buf.getvalue())
        return path

    def ndjson_archive(self, directory: str) -> str:
        catalog = [
            {
                "id": self.url,
                "type": "Movie",
                "title": "Inception",
                "external_resources": [
                    {"url": "https://www.imdb.com/title/tt1375666/"}
                ],
            }
        ]
        journal: list[dict] = [
            {"type": "Tag", "name": t, "pinned": False} for t in self.tags
        ]
        journal += [
            {
                "type": "TagMember",
                "metadata": {},
                "content": {
                    "type": "Tag",
                    "tag": t,
                    "published": self.marked_at,
                    "withRegardTo": self.url,
                },
            }
            for t in self.tags
        ]
        journal += [
            {
                "type": "Rating",
                "metadata": {},
                "content": {
                    "type": "Rating",
                    "best": 10,
                    "worst": 1,
                    "value": 8,
                    "published": self.marked_at,
                    "withRegardTo": self.url,
                },
            },
            {
                "type": "Comment",
                "metadata": {},
                "content": {
                    "type": "Comment",
                    "content": self.comment,
                    "published": self.marked_at,
                    "withRegardTo": self.url,
                },
            },
            {
                "type": "ShelfMember",
                "metadata": {},
                "content": {
                    "type": "Status",
                    "status": "complete",
                    "published": self.marked_at,
                    "withRegardTo": self.url,
                },
            },
            {
                "type": "Review",
                "metadata": {},
                "content": {
                    "type": "Review",
                    "name": self.review_title,
                    "content": self.review_body,
                    "mediaType": "text/markdown",
                    "published": self.marked_at,
                    "withRegardTo": self.url,
                },
            },
        ]
        return write_archive(directory, catalog, journal)

    def landed(self, user):
        """What actually reached the database, for one user."""
        owner = user.identity
        mark = Mark(owner, self.movie)
        review = Review.objects.filter(owner=owner, item=self.movie).first()
        return {
            "shelf_type": mark.shelf_type,
            "rating_grade": mark.rating_grade,
            "comment_text": mark.comment_text,
            "tags": sorted(mark.tags),
            "created_time": mark.created_time,
            "review_title": review.title if review else None,
            "review_body": review.body if review else None,
        }

    def run_both(self):
        with TemporaryDirectory() as d:
            CsvImporter.create(
                user=self.csv_user, file=self.csv_archive(d), visibility=0
            ).run()
            DoubakImporter.create(
                user=self.nd_user, file=self.ndjson_archive(d), visibility=0
            ).run()
        return self.landed(self.csv_user), self.landed(self.nd_user)

    def test_the_two_formats_land_identically(self):
        csv_side, nd_side = self.run_both()
        assert csv_side == nd_side

    def test_the_comparison_is_not_vacuous(self):
        # if either import silently did nothing, the dicts would still match --
        # so pin that both actually carried the awkward payload through
        csv_side, nd_side = self.run_both()
        for side in (csv_side, nd_side):
            assert side["shelf_type"] == ShelfType.COMPLETE
            assert side["rating_grade"] == 8
            assert side["comment_text"] == self.comment
            assert "\n" in side["comment_text"]
            assert side["tags"] == sorted(self.tags)
            assert side["review_title"] == self.review_title
            assert side["review_body"] == self.review_body

    def test_tags_survive_the_format_change(self):
        # CSV carries tags on the mark row; NDJSON has no such field and needs
        # separate Tag/TagMember records. Emitting only ShelfMember loses every
        # tag with no error at all, which is why this is asserted on its own.
        csv_side, nd_side = self.run_both()
        assert nd_side["tags"] == csv_side["tags"] != []

    def test_the_marked_date_is_the_same_day_on_both_sides(self):
        # CSV reads `timestamp`, NDJSON reads `content.published`. Getting the
        # key wrong on either side stamps the record with the import time,
        # which reads as a plausible date rather than as an error.
        csv_side, nd_side = self.run_both()
        assert csv_side["created_time"] == nd_side["created_time"]
        assert csv_side["created_time"].year == 2019


@pytest.mark.django_db(databases="__all__")
class TestDoubakReadsNeodbsOwnArchive:
    """Whatever ``NdjsonExporter`` writes, this importer reads.

    The tests above build their archives by hand, which only proves the
    importer agrees with this file's idea of the shape. Running the real
    exporter removes that circularity and reaches every record type at once,
    rather than the four the hand-built cases touch.
    """

    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.source = User.register(email="src@test.com", username="doubak_src")
        self.dest = User.register(email="dst@test.com", username="doubak_dst")
        self.owner = self.source.identity
        self.book = Edition.objects.create(
            localized_title=[{"lang": "en", "text": "Hyperion"}],
            primary_lookup_id_type=IdType.ISBN,
            primary_lookup_id_value="9780553283686",
        )
        self.dt = parse_datetime("2021-01-01T00:00:00Z")

        Mark(self.owner, self.book).update(
            ShelfType.COMPLETE, "a comment", 8, ["tagged"], 0, created_time=self.dt
        )
        Review.update_item_review(self.book, self.owner, "R", "body", visibility=0)
        Note.objects.create(item=self.book, owner=self.owner, content="n", visibility=0)
        collection = Collection.objects.create(
            owner=self.owner, title="C", brief="brief", visibility=0
        )
        collection.append_item(self.book, metadata={"note": "member note"})
        Article.update_local_article(
            owner=self.owner, title="A", body="article body", visibility=0
        )

    def exported(self) -> str:
        exporter = NdjsonExporter.create(user=self.source)
        exporter.run()
        return exporter.metadata["file"]

    def test_validate_file_accepts_what_neodb_itself_exports(self):
        # the one assertion that cannot drift: it uses the real producer
        with open(self.exported(), "rb") as f:
            assert DoubakImporter.validate_file(f) is True

    def test_every_record_type_survives_the_round_trip(self):
        importer = DoubakImporter.create(
            user=self.dest, file=self.exported(), visibility=0
        )
        importer.run()
        dest = self.dest.identity
        mark = Mark(dest, self.book)
        assert mark.shelf_type == ShelfType.COMPLETE
        assert mark.rating_grade == 8
        assert mark.comment_text == "a comment"
        assert sorted(mark.tags) == ["tagged"]
        assert Review.objects.filter(owner=dest, item=self.book).count() == 1
        assert Note.objects.filter(owner=dest, item=self.book).count() == 1
        assert Article.objects.filter(owner=dest, title="A").count() == 1
        collection = Collection.objects.get(owner=dest, title="C")
        assert collection.get_member_for_item(self.book).note == "member note"
        assert ShelfLogEntry.objects.filter(owner=dest, item=self.book).exists()
        assert Tag.objects.filter(owner=dest, title="tagged").exists()
        assert TagMember.objects.filter(owner=dest, item=self.book).exists()
        # nothing may fail: a failure here is a record type this importer
        # cannot read, which is the whole claim being made
        assert importer.metadata["failed"] == 0

    def test_overwrite_reaches_every_type_the_hand_built_cases_miss(self):
        path = self.exported()
        DoubakImporter.create(user=self.dest, file=path, visibility=0).run()
        dest = self.dest.identity

        # edit the destination so every record is strictly newer than the
        # archive, which is exactly when merge and overwrite diverge
        Review.objects.filter(owner=dest).update(body="edited here")
        Comment.objects.filter(owner=dest).update(text="edited here")
        Rating.objects.filter(owner=dest).update(grade=2)
        for model in (Review, Comment, Rating, Note, Collection, Article):
            model.objects.filter(owner=dest).update(
                edited_time=parse_datetime("2030-01-01T00:00:00Z")
            )

        DoubakImporter.create(user=self.dest, file=path, visibility=0).run()
        assert Review.objects.get(owner=dest, item=self.book).body == "edited here"

        DoubakImporter.create(
            user=self.dest, file=path, visibility=0, mode=DoubakImporter.OVERWRITE
        ).run()
        assert Review.objects.get(owner=dest, item=self.book).body == "body"
        assert Comment.objects.get(owner=dest, item=self.book).text == "a comment"
        assert Rating.objects.get(owner=dest, item=self.book).grade == 8

    def test_import_funcs_are_inherited_untouched(self):
        # the subclass overrides one method; dispatching is not its business.
        # A handler dropped here would count records as skipped and still
        # report success, so pin that the two tables are the same object shape.
        importer = DoubakImporter.create(user=self.dest, file="x.zip", visibility=0)
        parent = NdjsonImporter.create(user=self.source, file="x.zip", visibility=0)
        assert set(importer.import_funcs()) == set(parent.import_funcs())


@pytest.mark.django_db(databases="__all__")
class TestDoubakReadsWhatDoubakWrites:
    """Shapes Doubak emits that ``NdjsonExporter`` does not.

    It carries no ``updated`` stamp, fills ``ShelfLog`` metadata from Douban
    broadcasts, and omits ``published`` for the marks Douban gave no date for.
    """

    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.movie = Movie.objects.create(
            localized_title=[{"lang": "en", "text": "Inception"}],
            primary_lookup_id_type=IdType.IMDB,
            primary_lookup_id_value="tt1375666",
        )
        # what a catalogued item looks like, not a convenience: get_item()
        # needs a ready resource (metadata and scraped_time both set), which
        # a primary_lookup_id alone does not create, and preferred_model
        # decides the class -- an IMDb id may name a film or a series, so
        # IMDB has no DEFAULT_MODEL to fall back on
        ExternalResource.objects.create(
            item=self.movie,
            id_type=IdType.IMDB,
            id_value="tt1375666",
            url="https://www.imdb.com/title/tt1375666/",
            metadata={
                "preferred_model": "Movie",
                "localized_title": [{"lang": "en", "text": "Inception"}],
            },
            scraped_time=parse_datetime(OLD),
        )
        self.user = User.register(email="shapes@test.com", username="doubak_shapes")
        self.owner = self.user.identity
        self.url = self.movie.absolute_url

    def run(self, catalog, journal, **kwargs):
        with TemporaryDirectory() as d:
            path = write_archive(d, catalog, journal)
            importer = DoubakImporter.create(
                user=self.user, file=path, visibility=0, **kwargs
            )
            importer.run()
            return importer

    def test_shelf_log_carries_the_star_of_that_day(self):
        # Douban overwrites a mark's rating on every edit and keeps no history;
        # a broadcast is frozen at post time. So the log's rating_grade is a
        # different fact from the mark's, and losing it loses the only record.
        importer = self.run(
            [{"id": self.url}],
            [
                {
                    "type": "ShelfMember",
                    "metadata": {},
                    "content": {
                        "type": "Status",
                        "status": "complete",
                        "published": NEW,
                        "withRegardTo": self.url,
                    },
                },
                {
                    "type": "ShelfLog",
                    "item": self.url,
                    "status": "progress",
                    "timestamp": OLD,
                    "metadata": {"rating_grade": 10, "comment_text": "then a 5"},
                },
            ],
        )
        assert importer.metadata["failed"] == 0
        log = ShelfLogEntry.objects.get(
            owner=self.owner, item=self.movie, shelf_type=ShelfType.PROGRESS
        )
        assert log.rating_grade == 10
        assert log.comment_text == "then a 5"

    def test_a_mark_with_no_published_still_imports(self):
        # 8 of 2950 real marks carry no date at all. Omitting the key is the
        # honest encoding; inventing one would stamp a plausible lie.
        importer = self.run(
            [{"id": self.url}],
            [
                {
                    "type": "ShelfMember",
                    "metadata": {},
                    "content": {
                        "type": "Status",
                        "status": "wishlist",
                        "withRegardTo": self.url,
                    },
                }
            ],
        )
        assert importer.metadata["failed"] == 0
        assert Mark(self.owner, self.movie).shelf_type == ShelfType.WISHLIST

    def test_an_item_resolves_through_external_resources(self):
        # Doubak writes the Douban URL as the catalog id and the IMDb URL
        # alongside it. IMDb sorts ahead of Douban in _PREFERRED_SITES, so a
        # catalogued item is found without any request going out -- which is
        # also what keeps a work resolvable after Douban deletes its page.
        importer = self.run(
            [
                {
                    "id": "https://movie.douban.com/subject/1375666/",
                    "external_resources": [
                        {"url": "https://www.imdb.com/title/tt1375666/"}
                    ],
                }
            ],
            [
                {
                    "type": "ShelfMember",
                    "metadata": {},
                    "content": {
                        "type": "Status",
                        "status": "complete",
                        "published": OLD,
                        "withRegardTo": "https://movie.douban.com/subject/1375666/",
                    },
                }
            ],
        )
        assert importer.metadata["failed"] == 0
        assert Mark(self.owner, self.movie).shelf_type == ShelfType.COMPLETE

    def test_a_collection_member_note_survives(self):
        importer = self.run(
            [{"id": self.url}],
            [
                {
                    "type": "Collection",
                    "metadata": {},
                    "collaborative": 0,
                    "query": None,
                    "cover": None,
                    "content": {"name": "买过的", "content": "简介", "published": OLD},
                    "items": [{"item": self.url, "metadata": {"note": "A$49.21"}}],
                }
            ],
        )
        assert importer.metadata["failed"] == 0
        collection = Collection.objects.get(owner=self.owner, title="买过的")
        assert collection.get_member_for_item(self.movie).note == "A$49.21"

    def test_a_private_doulist_arrives_restricted(self):
        # the upload page hides its visibility radios for ndjson, so a private
        # 豆列 can only stay private by saying so in the file
        self.run(
            [{"id": self.url}],
            [
                {
                    "type": "Collection",
                    "visibility": 2,
                    "metadata": {},
                    "content": {"name": "私密", "content": "", "published": OLD},
                    "items": [],
                }
            ],
        )
        assert Collection.objects.get(owner=self.owner, title="私密").visibility == 2

    def test_an_unattached_diary_becomes_an_article(self):
        importer = self.run(
            [],
            [
                {
                    "type": "Article",
                    "metadata": {},
                    "cover": None,
                    "content": {
                        "type": "Article",
                        "name": "日记",
                        "summary": "",
                        "sensitive": False,
                        "tag": [],
                        "source": {
                            "content": "- 一\n- 二",
                            "mediaType": "text/markdown",
                        },
                        "published": OLD,
                    },
                }
            ],
        )
        assert importer.metadata["failed"] == 0
        # the markdown body must come from source, not the rendered fallback
        assert Article.objects.get(owner=self.owner, title="日记").body == "- 一\n- 二"

    def test_a_record_type_we_do_not_emit_is_counted_not_fatal(self):
        # a future Doubak writing something this NeoDB does not know must not
        # stall the progress bar short of 100%
        importer = self.run(
            [{"id": self.url}],
            [
                {"type": "SomethingNew", "content": {}},
                {
                    "type": "ShelfMember",
                    "metadata": {},
                    "content": {
                        "type": "Status",
                        "status": "wishlist",
                        "published": OLD,
                        "withRegardTo": self.url,
                    },
                },
            ],
        )
        m = importer.metadata
        assert m["total"] == 2
        assert m["processed"] == m["total"]
        assert m["imported"] == 1 and m["skipped"] == 1

    def mark_with_comment(self, comment, updated=None):
        content = {
            "type": "Comment",
            "content": comment,
            "published": OLD,
            "withRegardTo": self.url,
        }
        if updated:
            content["updated"] = updated
        return [
            {
                "type": "ShelfMember",
                "metadata": {},
                "content": {
                    "type": "Status",
                    "status": "complete",
                    "published": OLD,
                    "withRegardTo": self.url,
                },
            },
            {"type": "Comment", "metadata": {}, "content": content},
        ]

    def test_an_edit_made_after_the_first_import_replays(self):
        # Douban's marked_at is the day a mark was made and does not move when
        # the comment is rewritten, so published is identical in both archives
        # and updated is the only thing that can carry the edit.
        self.run([{"id": self.url}], self.mark_with_comment("first", updated=OLD))
        self.run([{"id": self.url}], self.mark_with_comment("second", updated=NEW))
        assert Comment.objects.get(owner=self.owner, item=self.movie).text == "second"

    def test_without_updated_the_same_edit_is_dropped(self):
        # the control for the test above: this is what the exporter did before
        # it emitted updated, and it fails silently -- the import reports
        # success and the new comment is simply not there
        self.run([{"id": self.url}], self.mark_with_comment("first"))
        self.run([{"id": self.url}], self.mark_with_comment("second"))
        assert Comment.objects.get(owner=self.owner, item=self.movie).text == "first"

    def test_reimporting_an_unchanged_archive_changes_nothing(self):
        # updated must not move when a record is merely observed again, or
        # every import rewrites everything and stamps it with the import time
        self.run([{"id": self.url}], self.mark_with_comment("only", updated=OLD))
        before = Comment.objects.get(owner=self.owner, item=self.movie).edited_time
        importer = self.run(
            [{"id": self.url}], self.mark_with_comment("only", updated=OLD)
        )
        after = Comment.objects.get(owner=self.owner, item=self.movie)
        assert after.text == "only"
        assert after.edited_time == before
        assert importer.metadata["skipped"] >= 1

    def test_a_collection_keeps_its_identity_across_exports(self):
        # import_collection matches on (owner, title, created_time), and
        # created_time is published. A published that moved with every crawl
        # would make the second import build a second collection of the same
        # name rather than update the first.
        def archive(updated):
            return [
                {
                    "type": "Collection",
                    "metadata": {},
                    "content": {
                        "name": "买过的",
                        "content": "简介",
                        "published": OLD,
                        "updated": updated,
                    },
                    "items": [{"item": self.url, "metadata": {}}],
                }
            ]

        self.run([{"id": self.url}], archive(OLD))
        self.run([{"id": self.url}], archive(NEW))
        assert Collection.objects.filter(owner=self.owner, title="买过的").count() == 1


@pytest.mark.django_db(databases="__all__")
class TestDoubakIsWiredIn:
    """The pieces with nothing between them to catch a mismatch: a migration
    never written, a form field the view reads by string."""

    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.user = User.register(email="wired@test.com", username="doubak_wired")

    def test_the_proxy_model_and_task_type_are_migrated(self):
        # a missing (or misnumbered) migration does not raise on its own --
        # the task simply stores a type the database will not accept
        from django.apps import apps

        assert apps.get_model("journal", "DoubakImporter") is DoubakImporter
        # getattr rather than .choices: get_field is typed as returning
        # Field | ForeignObjectRel and only one of those has the attribute
        field = Task._meta.get_field("type")
        choices = dict(getattr(field, "choices", None) or [])
        assert "journal.doubakimporter" in choices

    def test_the_data_page_renders_the_upload_form(self, client):
        # the form's action, its file input and both mode values are read by
        # string in the view; a typo in the template silently posts a default
        client.force_login(self.user, backend="mastodon.auth.OAuth2Backend")
        body = client.get(reverse("users:data")).content.decode()
        assert reverse("users:import_doubak") in body
        assert 'name="import_mode"' in body
        assert 'name="visibility"' in body
        assert "doubak.com" in body

    def test_an_out_of_range_mode_falls_back_to_merge(self):
        # the view casts whatever the form posted; anything that is not
        # OVERWRITE must be treated as the safe option, not as overwrite
        importer = DoubakImporter.create(
            user=self.user, file="x.zip", visibility=0, mode=7
        )
        assert importer.overwrite is False
