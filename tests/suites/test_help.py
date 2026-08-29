from __future__ import annotations

import urllib.request

from tests.lib.framework import API_BASE, API_TOKEN, assert_eq


class TestHelp:
    def _get(self):
        r = urllib.request.Request(
            API_BASE + "/help", method="GET",
            headers={"Authorization": f"Bearer {API_TOKEN}"})
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), \
                   resp.read().decode()

    def test_returns_200(self):
        status, _, _ = self._get()
        assert_eq(status, 200, "status")

    def test_content_type_is_markdown(self):
        _, ct, _ = self._get()
        if "markdown" not in ct:
            raise AssertionError(f"unexpected Content-Type: {ct}")

    def test_body_not_empty(self):
        _, _, body = self._get()
        if not body.strip():
            raise AssertionError("help body is empty")

    def test_contains_cloudcore_heading(self):
        _, _, body = self._get()
        if "CloudCore" not in body:
            raise AssertionError("missing CloudCore heading")

    def test_contains_vpcs_section(self):
        _, _, body = self._get()
        if "VPCs" not in body:
            raise AssertionError("missing VPCs section")

    def test_contains_instances_section(self):
        _, _, body = self._get()
        if "Instances" not in body:
            raise AssertionError("missing Instances section")

    def test_contains_load_balancers_section(self):
        _, _, body = self._get()
        if "Load Balancers" not in body:
            raise AssertionError("missing Load Balancers section")

    def test_contains_terminal_section(self):
        _, _, body = self._get()
        if "Terminal" not in body:
            raise AssertionError("missing Terminal section")

    def test_contains_dns_section(self):
        _, _, body = self._get()
        if "DNS" not in body:
            raise AssertionError("missing DNS section")

    def test_contains_api_table(self):
        _, _, body = self._get()
        if "/v1/vpcs" not in body:
            raise AssertionError("missing API endpoint table")
