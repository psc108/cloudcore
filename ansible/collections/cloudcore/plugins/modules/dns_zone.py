#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: dns_zone
short_description: Manage CloudCore DNS zones
options:
  api_url:
    type: str
  api_token:
    type: str
    no_log: true
  name:
    description: Zone name (e.g. myapp.internal).
    type: str
    required: true
  state:
    type: str
    choices: [present, absent]
    default: present
"""

EXAMPLES = r"""
- name: Create DNS zone
  cloudcore.cloudcore.dns_zone:
    name: myapp.internal
    state: present

- name: Delete DNS zone
  cloudcore.cloudcore.dns_zone:
    name: myapp.internal
    state: absent
"""

RETURN = r"""
zone:
  description: Zone object returned by the API.
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
            name=dict(type="str", required=True),
            state=dict(type="str", default="present", choices=["present", "absent"]),
        ),
        supports_check_mode=True,
    )

    try:
        client = CloudCoreClient.from_module_params(module.params)
    except (ImportError, ValueError) as e:
        module.fail_json(msg=str(e))

    name = module.params["name"]
    state = module.params["state"]

    zones = client.get("/v1/dns/zones").get("items", [])
    existing = next((z for z in zones if z["name"] == name), None)

    if state == "absent":
        if not existing:
            module.exit_json(changed=False)
        if not module.check_mode:
            client.delete(f"/v1/dns/zones/{name}")
        module.exit_json(changed=True)

    if existing:
        module.exit_json(changed=False, zone=existing)
    if module.check_mode:
        module.exit_json(changed=True, zone={})
    result = client.post("/v1/dns/zones", {"name": name})
    module.exit_json(changed=True, zone=result)


def main():
    run_module()


if __name__ == "__main__":
    main()
