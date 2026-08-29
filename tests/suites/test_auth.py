from __future__ import annotations

import urllib.error
import urllib.request

from tests.lib.framework import API_BASE, API_TOKEN, assert_eq, req


class TestAuth:
    def test_rejected_without_token(self):
        r = urllib.request.Request(API_BASE + "/v1/vpcs", method="GET")
        try:
            urllib.request.urlopen(r)
            raise AssertionError("Expected 401, got 200")
        except urllib.error.HTTPError as e:
            assert_eq(e.code, 401, "status without token")

    def test_rejected_wrong_token(self):
        r = urllib.request.Request(API_BASE + "/v1/vpcs", method="GET",
            headers={"Authorization": "Bearer wrong-token"})
        try:
            urllib.request.urlopen(r)
            raise AssertionError("Expected 401, got 200")
        except urllib.error.HTTPError as e:
            assert_eq(e.code, 401, "status wrong token")

    def test_accepted_with_token(self):
        req("GET", "/v1/vpcs", expected=200)
