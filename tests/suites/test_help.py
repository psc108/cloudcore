from __future__ import annotations

from tests.lib.framework import assert_eq, assert_in, assert_not_in, req
from tests.lib.helpers import cleanup_help_by_prefix, delete_help_article, make_help_article

PFX = "t-help-"


class TestHelp:
    def setUp(self):
        cleanup_help_by_prefix(PFX)

    # ── Create ────────────────────────────────────────────────────────────────
    def test_create_returns_201(self):
        status, body = req("POST", "/v1/help/articles", {
            "title": PFX + "create",
        }, expected=201)
        assert_eq(status, 201, "create status")
        delete_help_article(body["id"])

    def test_create_title_stored(self):
        a = make_help_article(PFX + "title")
        assert_eq(a["title"], PFX + "title", "title stored")
        delete_help_article(a["id"])

    def test_create_default_category(self):
        a = make_help_article(PFX + "cat-default")
        assert_eq(a["category"], "General", "default category")
        delete_help_article(a["id"])

    def test_create_custom_category(self):
        a = make_help_article(PFX + "cat-custom", category="Networking")
        assert_eq(a["category"], "Networking", "custom category")
        delete_help_article(a["id"])

    def test_create_content_stored(self):
        a = make_help_article(PFX + "content", content="Some body text.")
        assert_eq(a["content"], "Some body text.", "content stored")
        delete_help_article(a["id"])

    def test_create_id_present(self):
        a = make_help_article(PFX + "id")
        assert_in("id", a, "id field present")
        if not a["id"]:
            raise AssertionError("id is empty")
        delete_help_article(a["id"])

    def test_create_slug_auto_derived(self):
        a = make_help_article(PFX + "Auto Slug")
        assert_in("slug", a, "slug field present")
        if not a["slug"]:
            raise AssertionError("slug is empty")
        delete_help_article(a["id"])

    def test_create_missing_title_returns_400(self):
        req("POST", "/v1/help/articles", {}, expected=400)

    def test_create_duplicate_slug_rejected(self):
        a = make_help_article(PFX + "dup", content="first")
        req("POST", "/v1/help/articles",
            {"title": "different title", "slug": a["slug"]}, expected=409)
        delete_help_article(a["id"])

    # ── Read ──────────────────────────────────────────────────────────────────
    def test_list_includes_created(self):
        a = make_help_article(PFX + "list")
        _, data = req("GET", "/v1/help/articles")
        assert_in(a["id"], [x["id"] for x in data["items"]], "article in list")
        delete_help_article(a["id"])

    def test_list_excludes_deleted(self):
        a = make_help_article(PFX + "listdel")
        delete_help_article(a["id"])
        _, data = req("GET", "/v1/help/articles")
        assert_not_in(a["id"], [x["id"] for x in data["items"]], "deleted not in list")

    def test_get_by_id(self):
        a = make_help_article(PFX + "get")
        _, got = req("GET", f"/v1/help/articles/{a['id']}")
        assert_eq(got["id"], a["id"], "id matches")
        delete_help_article(a["id"])

    def test_get_missing_returns_404(self):
        req("GET", "/v1/help/articles/does-not-exist", expected=404)

    # ── Update ────────────────────────────────────────────────────────────────
    def test_update_content_persists(self):
        a = make_help_article(PFX + "upd-content", content="old")
        req("PUT", f"/v1/help/articles/{a['id']}", {"content": "new"}, expected=200)
        _, got = req("GET", f"/v1/help/articles/{a['id']}")
        assert_eq(got["content"], "new", "content updated")
        delete_help_article(a["id"])

    def test_update_title_and_category(self):
        a = make_help_article(PFX + "upd-meta")
        req("PUT", f"/v1/help/articles/{a['id']}",
            {"title": PFX + "renamed", "category": "Storage"}, expected=200)
        _, got = req("GET", f"/v1/help/articles/{a['id']}")
        assert_eq(got["title"], PFX + "renamed", "title updated")
        assert_eq(got["category"], "Storage", "category updated")
        delete_help_article(a["id"])

    def test_update_missing_returns_404(self):
        req("PUT", "/v1/help/articles/does-not-exist", {"content": "x"}, expected=404)

    def test_update_empty_title_rejected(self):
        a = make_help_article(PFX + "upd-badtitle")
        req("PUT", f"/v1/help/articles/{a['id']}", {"title": ""}, expected=400)
        delete_help_article(a["id"])

    # ── Delete ────────────────────────────────────────────────────────────────
    def test_delete_returns_204(self):
        a = make_help_article(PFX + "del204")
        status, _ = req("DELETE", f"/v1/help/articles/{a['id']}", expected=204)
        assert_eq(status, 204, "delete status")

    def test_delete_removes_from_list(self):
        a = make_help_article(PFX + "del")
        delete_help_article(a["id"])
        _, data = req("GET", "/v1/help/articles")
        assert_not_in(a["id"], [x["id"] for x in data["items"]], "article removed")

    def test_delete_missing_returns_404(self):
        req("DELETE", "/v1/help/articles/does-not-exist", expected=404)

    # ── Search ────────────────────────────────────────────────────────────────
    def test_search_matches_title_term(self):
        a = make_help_article(PFX + "zzyzx", content="unrelated body text")
        _, data = req("GET", "/v1/help/articles?q=zzyzx")
        assert_in(a["id"], [x["id"] for x in data["items"]], "found by title term")
        delete_help_article(a["id"])

    def test_search_matches_content_term(self):
        a = make_help_article(PFX + "content-search", content="frobnicate the wobbulator")
        _, data = req("GET", "/v1/help/articles?q=wobbulator")
        assert_in(a["id"], [x["id"] for x in data["items"]], "found by content term")
        delete_help_article(a["id"])

    def test_search_excludes_deleted(self):
        a = make_help_article(PFX + "search-deleted", content="quibblesnort")
        delete_help_article(a["id"])
        _, data = req("GET", "/v1/help/articles?q=quibblesnort")
        assert_not_in(a["id"], [x["id"] for x in data["items"]], "deleted excluded from search")

    def test_search_no_match_returns_empty(self):
        _, data = req("GET", "/v1/help/articles?q=thistermdoesnotexistanywhereatall")
        assert_eq(data["items"], [], "no match returns empty list")

    def test_search_empty_query_returns_list(self):
        a = make_help_article(PFX + "empty-q")
        _, data = req("GET", "/v1/help/articles?q=")
        assert_in(a["id"], [x["id"] for x in data["items"]], "empty query behaves like list")
        delete_help_article(a["id"])
