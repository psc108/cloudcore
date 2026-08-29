#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: vpc
short_description: Manage CloudCore VPCs
description:
  - Create, update or delete a CloudCore VPC.
options:
  api_url:
    description: CloudCore API URL. Defaults to CLOUDCORE_API_URL env var.
    type: str
  api_token:
    description: CloudCore API token. Defaults to CLOUDCORE_API_TOKEN env var.
    type: str
    no_log: true
  name:
    description: VPC name.
    type: str
    required: true
  cidr_block:
    description: IPv4 CIDR block for the VPC.
    type: str
  dns_support:
    description: Enable DNS resolution within the VPC.
    type: bool
    default: true
  tags:
    description: Tags to apply to the VPC.
    type: dict
    default: {}
  state:
    description: Desired state.
    type: str
    choices: [present, absent]
    default: present
"""

EXAMPLES = r"""
- name: Create VPC
  cloudcore.cloudcore.vpc:
    name: payments-prod-main
    cidr_block: 10.0.0.0/16
    tags:
      Environment: prod
      Project: payments

- name: Delete VPC
  cloudcore.cloudcore.vpc:
    name: payments-prod-main
    state: absent
"""

RETURN = r"""
vpc:
  description: VPC object returned by the API.
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
            cidr_block=dict(type="str"),
            dns_support=dict(type="bool", default=True),
            tags=dict(type="dict", default={}),
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
    existing = client.find_by_name("/v1/vpcs", name)

    if state == "absent":
        if not existing:
            module.exit_json(changed=False)
        if not module.check_mode:
            client.delete(f"/v1/vpcs/{existing['id']}")
        module.exit_json(changed=True)

    body = {
        "name": name,
        "cidr_block": module.params["cidr_block"],
        "dns_support": module.params["dns_support"],
        "tags": module.params["tags"],
    }

    if not existing:
        if module.check_mode:
            module.exit_json(changed=True, vpc={})
        result = client.post("/v1/vpcs", body)
        module.exit_json(changed=True, vpc=result)

    # Update if needed — compare relevant fields
    changed = (
        existing.get("cidr_block") != body["cidr_block"]
        or existing.get("dns_support") != body["dns_support"]
        or existing.get("tags") != body["tags"]
    )
    if not changed:
        module.exit_json(changed=False, vpc=existing)
    if module.check_mode:
        module.exit_json(changed=True, vpc=existing)
    result = client.put(f"/v1/vpcs/{existing['id']}", body)
    module.exit_json(changed=True, vpc=result)


def main():
    run_module()


if __name__ == "__main__":
    main()
