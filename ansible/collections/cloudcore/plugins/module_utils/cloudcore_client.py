from __future__ import annotations

import os
from typing import Any

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class CloudCoreClient:
    def __init__(self, api_url: str, api_token: str) -> None:
        if not HAS_REQUESTS:
            raise ImportError("The 'requests' library is required for this module.")
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @classmethod
    def from_module_params(cls, params: dict) -> "CloudCoreClient":
        api_url = params.get("api_url") or os.environ.get("CLOUDCORE_API_URL", "")
        api_token = params.get("api_token") or os.environ.get("CLOUDCORE_API_TOKEN", "")
        if not api_url:
            raise ValueError("api_url is required (or set CLOUDCORE_API_URL)")
        if not api_token:
            raise ValueError("api_token is required (or set CLOUDCORE_API_TOKEN)")
        return cls(api_url, api_token)

    def get(self, path: str) -> dict:
        resp = self.session.get(f"{self.api_url}{path}", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: dict) -> dict:
        resp = self.session.post(f"{self.api_url}{path}", json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def put(self, path: str, body: dict) -> dict:
        resp = self.session.put(f"{self.api_url}{path}", json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str) -> None:
        resp = self.session.delete(f"{self.api_url}{path}", timeout=30)
        resp.raise_for_status()

    def find_by_name(self, path: str, name: str) -> dict | None:
        items = self.get(path)
        for item in items.get("items", []):
            if item.get("name") == name:
                return item
        return None
