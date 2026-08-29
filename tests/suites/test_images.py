from __future__ import annotations

from tests.lib.framework import assert_eq, assert_in, req


class TestImages:
    def test_returns_items_key(self):
        _, data = req("GET", "/v1/images")
        assert_in("items", data, "items key")

    def test_catalogue_not_empty(self):
        _, data = req("GET", "/v1/images")
        if not data["items"]:
            raise AssertionError("image catalogue is empty")

    def test_all_known_images_present(self):
        _, data = req("GET", "/v1/images")
        ids = [i["id"] for i in data["items"]]
        for eid in ("ubuntu-22.04", "ubuntu-24.04", "debian-12", "rocky-9"):
            assert_in(eid, ids, f"{eid} in catalogue")

    def test_each_image_has_id(self):
        _, data = req("GET", "/v1/images")
        for img in data["items"]:
            assert_in("id", img, "id field")

    def test_each_image_has_name(self):
        _, data = req("GET", "/v1/images")
        for img in data["items"]:
            assert_in("name", img, f"name on {img['id']}")
            if not img["name"]:
                raise AssertionError(f"name empty on {img['id']}")

    def test_each_image_has_available_flag(self):
        _, data = req("GET", "/v1/images")
        for img in data["items"]:
            assert_in("available", img, f"available on {img['id']}")
            if not isinstance(img["available"], bool):
                raise AssertionError(f"available not bool on {img['id']}")

    def test_each_image_has_fetch_url(self):
        _, data = req("GET", "/v1/images")
        for img in data["items"]:
            assert_in("fetch_url", img, f"fetch_url on {img['id']}")

    def test_each_image_has_min_disk_gb(self):
        _, data = req("GET", "/v1/images")
        for img in data["items"]:
            assert_in("min_disk_gb", img, f"min_disk_gb on {img['id']}")

    def test_ubuntu_2204_available(self):
        _, data = req("GET", "/v1/images")
        img = next((i for i in data["items"] if i["id"] == "ubuntu-22.04"), None)
        if img is None:
            raise AssertionError("ubuntu-22.04 not in catalogue")
        assert_eq(img["available"], True, "ubuntu-22.04 available on disk")

    def test_ubuntu_2204_distro_field(self):
        _, data = req("GET", "/v1/images")
        img = next((i for i in data["items"] if i["id"] == "ubuntu-22.04"), None)
        assert_eq(img["distro"], "ubuntu", "ubuntu-22.04 distro field")
