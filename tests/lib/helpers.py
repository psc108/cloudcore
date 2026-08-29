from __future__ import annotations

import urllib.parse
from tests.lib.framework import req


def make_vpc(name: str, cidr: str = "10.99.0.0/16", tags: dict | None = None) -> dict:
    _, body = req("POST", "/v1/vpcs", {
        "name": name,
        "cidr_block": cidr,
        "dns_support": True,
        "tags": tags or {},
    }, expected=201)
    return body


def delete_vpc(vpc_id: str) -> None:
    req("DELETE", f"/v1/vpcs/{vpc_id}", expected=204)


def make_lb(name: str, vpc_id: str, lb_type: str = "application") -> dict:
    _, body = req("POST", "/v1/load-balancers", {
        "name": name,
        "type": lb_type,
        "vpc_id": vpc_id,
        "internal": False,
    }, expected=201)
    return body


def delete_lb(lb_id: str) -> None:
    req("DELETE", f"/v1/load-balancers/{lb_id}", expected=204)


def make_instance(name: str, vpc_id: str, flavor: str = "standard.nano",
                  image_id: str = "ubuntu-22.04", subnet: str = "subnet-test") -> dict:
    _, body = req("POST", "/v1/instances", {
        "name": name,
        "image_id": image_id,
        "flavor": flavor,
        "vpc_id": vpc_id,
        "subnet_id": subnet,
    }, expected=202)
    return body


def delete_instance(instance_id: str) -> None:
    req("DELETE", f"/v1/instances/{instance_id}", expected=204)


def make_dns_zone(name: str) -> dict:
    _, body = req("POST", "/v1/dns/zones", {"name": name}, expected=201)
    return body


def delete_dns_zone(name: str) -> None:
    req("DELETE",
        f"/v1/dns/zones/{urllib.parse.quote(name, safe='')}",
        expected=204)


def cleanup_by_prefix(resource: str, prefix: str) -> None:
    """Delete all resources whose name starts with prefix."""
    import time
    _, data = req("GET", f"/v1/{resource}")
    found = [item for item in data["items"] if item["name"].startswith(prefix)]
    for item in found:
        req("DELETE", f"/v1/{resource}/{item['id']}", expected=(204, 404))
    if found:
        time.sleep(1)  # allow async deletes to complete


def cleanup_dns_zones_by_prefix(prefix: str) -> None:
    _, data = req("GET", "/v1/dns/zones")
    for z in data["items"]:
        if z["name"].startswith(prefix):
            req("DELETE",
                f"/v1/dns/zones/{urllib.parse.quote(z['name'], safe='')}",
                expected=(204, 404))
