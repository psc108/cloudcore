from __future__ import annotations

from tests.lib.framework import assert_in, req


class TestSSHKey:
    def test_returns_200(self):
        req("GET", "/v1/ssh-key", expected=200)

    def test_public_key_field_present(self):
        _, data = req("GET", "/v1/ssh-key")
        assert_in("public_key", data, "public_key field")

    def test_key_path_field_present(self):
        _, data = req("GET", "/v1/ssh-key")
        assert_in("key_path", data, "key_path field")

    def test_public_key_starts_with_ssh(self):
        _, data = req("GET", "/v1/ssh-key")
        if not data["public_key"].startswith("ssh-"):
            raise AssertionError(
                f"unexpected key format: {data['public_key'][:40]}")

    def test_public_key_is_ed25519(self):
        _, data = req("GET", "/v1/ssh-key")
        if "ed25519" not in data["public_key"].lower():
            raise AssertionError(
                f"expected ed25519 key, got: {data['public_key'][:60]}")

    def test_key_path_not_empty(self):
        _, data = req("GET", "/v1/ssh-key")
        if not data["key_path"]:
            raise AssertionError("key_path is empty")

    def test_key_path_points_to_ed25519_file(self):
        _, data = req("GET", "/v1/ssh-key")
        if "cloudcore_ed25519" not in data["key_path"]:
            raise AssertionError(
                f"unexpected key_path: {data['key_path']}")
