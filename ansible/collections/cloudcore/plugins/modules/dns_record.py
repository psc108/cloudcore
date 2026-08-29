#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: dns_record
short_description: Manage CloudCore DNS records
options:
  api_url:
    type: str
  api_token:
    type: str
    no_log: true
  zone:
    description: Zone name the record belongs to.
    type: str
    required: true
  name:
    description: Record name (relative label, e.g. www).
    type: str
    required: true
  type:
    description: Record type.
    type: str
    choices: [A, CNAME, TXT]
    default: A
  value:
    description: Record value. Required when state=present.
    type: str
  ttl:
    description: TTL in seconds.
    type: int
    default: 300
  state:
    type: str
    choices: [present, absent]
    default: present
"""

EXAMPLES = r"""
- name: Create A record
  cloudcore.cloudcore.dns_record:
    zone: myapp.internal
    name: web
    type: A
    value: 192.168.100.10

- name: Create CNAME
  cloudcore.cloudcore.dns_record:
    zone: myapp.internal
    name: www
    type: CNAME
    value: web.myapp.internal

- name: Delete record
  cloudcore.cloudcore.dns_record:
    zone: myapp.internal
    name: web
    type: A
    state: absent
"""

RETURN = r"""
record:
  description: DNS record object returned by the API.
  returned: when state=present
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.cloudcore.cloudcore.plugins.module_utils.cloudcore_client import CloudCoreClient


def run_module():
    module = AnsibleModule(
        argument_spec=dict(
            api_url=dict(type="str"),
            api_token=dict(type="str", no_log=True),
            zone=dict(type="str", required=True),
            name=dict(type="str", required=True),
            type=dict(type="str", default="A", choices=["A", "CNAME", "TXT"]),
            value=dict(type="str"),
            ttl=dict(type="int", default=300),
            state=dict(type="str", default="present", choices=["present", "absent"]),
        ),
        required_if=[("state", "present", ["value"])],
        supports_check_mode=True,
    )

    try:
        client = CloudCoreClient.from_module_params(module.params)
    except (ImportError, ValueError) as e:
        module.fail_json(msg=str(e))

    zone = module.params["zone"]
    name = module.params["name"]
    rtype = module.params["type"]
    state = module.params["state"]

    records = client.get(f"/v1/dns/zones/{zone}/records").get("items", [])
    existing = next((r for r in records if r["name"] == name and r["type"] == rtype), None)

    if state == "absent":
        if not existing:
            module.exit_json(changed=False)
        if not module.check_mode:
            client.delete(f"/v1/dns/zones/{zone}/records/{name}/{rtype}")
        module.exit_json(changed=True)

    body = {
        "name": name,
        "type": rtype,
        "value": module.params["value"],
        "ttl": module.params["ttl"],
    }

    if existing:
        changed = existing.get("value") != body["value"] or existing.get("ttl") != body["ttl"]
        if not changed:
            module.exit_json(changed=False, record=existing)
        if module.check_mode:
            module.exit_json(changed=True, record=existing)
        result = client.post(f"/v1/dns/zones/{zone}/records", body)
        module.exit_json(changed=True, record=result)

    if module.check_mode:
        module.exit_json(changed=True, record={})
    result = client.post(f"/v1/dns/zones/{zone}/records", body)
    module.exit_json(changed=True, record=result)


def main():
    run_module()


if __name__ == "__main__":
    main()
