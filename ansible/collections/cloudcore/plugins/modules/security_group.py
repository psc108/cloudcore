#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: security_group
short_description: Manage CloudCore security groups
description:
  - Create, update or delete a CloudCore security group.
options:
  api_url:
    description: CloudCore API URL. Defaults to CLOUDCORE_API_URL env var.
    type: str
  api_token:
    description: CloudCore API token. Defaults to CLOUDCORE_API_TOKEN env var.
    type: str
    no_log: true
  name:
    description: Security group name.
    type: str
    required: true
  vpc_id:
    description: VPC this security group belongs to.
    type: str
  description:
    description: Security group description.
    type: str
    default: ""
  ingress_rules:
    description: >-
      List of ingress rules. Each rule accepts protocol (tcp/udp/icmp/-1,
      default -1), from_port/to_port (required unless protocol is -1),
      and exactly one of cidr/cidr_ipv6 or source_sg_id as the traffic
      source (cidr and cidr_ipv6 may both be set for dual-stack; neither
      may be combined with source_sg_id). Validated server-side.
    type: list
    elements: dict
    default: []
  egress_rules:
    description: Same shape as ingress_rules, for outbound traffic.
    type: list
    elements: dict
    default: []
  tags:
    description: Tags to apply to the security group.
    type: dict
    default: {}
  state:
    description: Desired state.
    type: str
    choices: [present, absent]
    default: present
"""

EXAMPLES = r"""
- name: Create security group
  cloudcore.cloudcore.security_group:
    name: payments-prod-web
    vpc_id: "{{ vpc.vpc.id }}"
    description: Web tier — SSH + HTTP
    ingress_rules:
      - protocol: tcp
        from_port: 22
        to_port: 22
        cidr: 203.0.113.4/32
      - protocol: tcp
        from_port: 80
        to_port: 80
        cidr: 0.0.0.0/0
    egress_rules:
      - protocol: "-1"
        cidr: 0.0.0.0/0

- name: Delete security group
  cloudcore.cloudcore.security_group:
    name: payments-prod-web
    state: absent
"""

RETURN = r"""
security_group:
  description: Security group object returned by the API.
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
            vpc_id=dict(type="str"),
            description=dict(type="str", default=""),
            ingress_rules=dict(type="list", elements="dict", default=[]),
            egress_rules=dict(type="list", elements="dict", default=[]),
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
    existing = client.find_by_name("/v1/security-groups", name)

    if state == "absent":
        if not existing:
            module.exit_json(changed=False)
        if not module.check_mode:
            client.delete(f"/v1/security-groups/{existing['id']}")
        module.exit_json(changed=True)

    body = {
        "name": name,
        "vpc_id": module.params["vpc_id"],
        "description": module.params["description"],
        "ingress_rules": module.params["ingress_rules"],
        "egress_rules": module.params["egress_rules"],
        "tags": module.params["tags"],
    }

    if not existing:
        if module.check_mode:
            module.exit_json(changed=True, security_group={})
        result = client.post("/v1/security-groups", body)
        module.exit_json(changed=True, security_group=result)

    # Update if needed — compare relevant fields. Rule-list comparison is
    # a plain equality check, not a semantic diff: a re-run may report
    # changed=True if the server echoes rules back with extra
    # server-filled defaults not present in the input. Acceptable here —
    # not worth a bespoke rule-aware differ for this collection's scope.
    changed = (
        existing.get("description") != body["description"]
        or existing.get("ingress_rules") != body["ingress_rules"]
        or existing.get("egress_rules") != body["egress_rules"]
        or existing.get("tags") != body["tags"]
    )
    if not changed:
        module.exit_json(changed=False, security_group=existing)
    if module.check_mode:
        module.exit_json(changed=True, security_group=existing)
    result = client.put(f"/v1/security-groups/{existing['id']}", body)
    module.exit_json(changed=True, security_group=result)


def main():
    run_module()


if __name__ == "__main__":
    main()
