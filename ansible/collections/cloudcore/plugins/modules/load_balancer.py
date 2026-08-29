#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: load_balancer
short_description: Manage CloudCore load balancers
options:
  api_url:
    type: str
  api_token:
    type: str
    no_log: true
  name:
    type: str
    required: true
  type:
    description: "'network' for L4, 'application' for L7."
    type: str
    choices: [network, application]
  vpc_id:
    type: str
  subnet_ids:
    type: list
    elements: str
    default: []
  internal:
    type: bool
    default: false
  tags:
    type: dict
    default: {}
  state:
    type: str
    choices: [present, absent]
    default: present
"""

EXAMPLES = r"""
- name: Create application load balancer
  cloudcore.cloudcore.load_balancer:
    name: web-alb
    type: application
    vpc_id: vpc-abc123
    subnet_ids:
      - subnet-pub-a
      - subnet-pub-b
    tags:
      Role: ingress
"""

RETURN = r"""
load_balancer:
  description: Load balancer object returned by the API.
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
            type=dict(type="str", choices=["network", "application"]),
            vpc_id=dict(type="str"),
            subnet_ids=dict(type="list", elements="str", default=[]),
            internal=dict(type="bool", default=False),
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
    existing = client.find_by_name("/v1/load-balancers", name)

    if state == "absent":
        if not existing:
            module.exit_json(changed=False)
        if not module.check_mode:
            client.delete(f"/v1/load-balancers/{existing['id']}")
        module.exit_json(changed=True)

    body = {
        "name": name,
        "type": module.params["type"],
        "vpc_id": module.params["vpc_id"],
        "subnet_ids": module.params["subnet_ids"],
        "internal": module.params["internal"],
        "tags": module.params["tags"],
    }

    if not existing:
        if module.check_mode:
            module.exit_json(changed=True, load_balancer={})
        result = client.post("/v1/load-balancers", body)
        module.exit_json(changed=True, load_balancer=result)

    changed = (
        existing.get("subnet_ids") != body["subnet_ids"]
        or existing.get("internal") != body["internal"]
        or existing.get("tags") != body["tags"]
    )
    if not changed:
        module.exit_json(changed=False, load_balancer=existing)
    if module.check_mode:
        module.exit_json(changed=True, load_balancer=existing)
    result = client.put(f"/v1/load-balancers/{existing['id']}", body)
    module.exit_json(changed=True, load_balancer=result)


def main():
    run_module()


if __name__ == "__main__":
    main()
