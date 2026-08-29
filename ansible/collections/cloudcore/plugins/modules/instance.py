#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: instance
short_description: Manage CloudCore compute instances
options:
  api_url:
    type: str
  api_token:
    type: str
    no_log: true
  name:
    type: str
    required: true
  image_id:
    type: str
  flavor:
    type: str
  vpc_id:
    type: str
  subnet_id:
    type: str
  security_group_ids:
    type: list
    elements: str
    default: []
  user_data:
    type: str
    no_log: true
  tags:
    type: dict
    default: {}
  state:
    type: str
    choices: [present, absent]
    default: present
"""

EXAMPLES = r"""
- name: Create instance
  cloudcore.cloudcore.instance:
    name: web-01
    image_id: ubuntu-22.04
    flavor: standard.small
    vpc_id: vpc-abc123
    subnet_id: subnet-abc123
    tags:
      Role: web
"""

RETURN = r"""
instance:
  description: Instance object returned by the API.
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
            image_id=dict(type="str"),
            flavor=dict(type="str"),
            vpc_id=dict(type="str"),
            subnet_id=dict(type="str"),
            security_group_ids=dict(type="list", elements="str", default=[]),
            user_data=dict(type="str", no_log=True),
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
    existing = client.find_by_name("/v1/instances", name)

    if state == "absent":
        if not existing:
            module.exit_json(changed=False)
        if not module.check_mode:
            client.delete(f"/v1/instances/{existing['id']}")
        module.exit_json(changed=True)

    body = {
        "name": name,
        "image_id": module.params["image_id"],
        "flavor": module.params["flavor"],
        "vpc_id": module.params["vpc_id"],
        "subnet_id": module.params["subnet_id"],
        "security_group_ids": module.params["security_group_ids"],
        "user_data": module.params["user_data"],
        "tags": module.params["tags"],
    }

    if not existing:
        if module.check_mode:
            module.exit_json(changed=True, instance={})
        result = client.post("/v1/instances", body)
        module.exit_json(changed=True, instance=result)

    changed = (
        existing.get("image_id") != body["image_id"]
        or existing.get("flavor") != body["flavor"]
        or existing.get("tags") != body["tags"]
    )
    if not changed:
        module.exit_json(changed=False, instance=existing)
    if module.check_mode:
        module.exit_json(changed=True, instance=existing)
    result = client.put(f"/v1/instances/{existing['id']}", body)
    module.exit_json(changed=True, instance=result)


def main():
    run_module()


if __name__ == "__main__":
    main()
