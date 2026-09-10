#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: lb_target_group
short_description: Manage CloudCore load balancer target groups
description:
  - Create, update or delete a target group on a CloudCore load balancer.
  - Target groups are looked up by name within the parent load balancer's
    own target-group collection (not a flat top-level resource).
options:
  api_url:
    description: CloudCore API URL. Defaults to CLOUDCORE_API_URL env var.
    type: str
  api_token:
    description: CloudCore API token. Defaults to CLOUDCORE_API_TOKEN env var.
    type: str
    no_log: true
  lb_id:
    description: ID of the parent load balancer.
    type: str
    required: true
  name:
    description: Target group name.
    type: str
    required: true
  port:
    description: Port targets are reached on. Required when state=present.
    type: int
  protocol:
    description: Target group protocol.
    type: str
    default: tcp
  targets:
    description: List of targets, each an instance_id and optional port override.
    type: list
    elements: dict
    default: []
  health_check:
    description: Health check config (path/interval/healthy_threshold/unhealthy_threshold).
    type: dict
    default: {}
  state:
    description: Desired state.
    type: str
    choices: [present, absent]
    default: present
"""

EXAMPLES = r"""
- name: Create target group
  cloudcore.cloudcore.lb_target_group:
    lb_id: "{{ lb.load_balancer.id }}"
    name: myapp-tg01
    port: 80
    protocol: tcp
    targets:
      - instance_id: "{{ instance.instance.id }}"
        port: 80

- name: Delete target group
  cloudcore.cloudcore.lb_target_group:
    lb_id: "{{ lb.load_balancer.id }}"
    name: myapp-tg01
    state: absent
"""

RETURN = r"""
target_group:
  description: Target group object returned by the API.
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
            lb_id=dict(type="str", required=True),
            name=dict(type="str", required=True),
            port=dict(type="int"),
            protocol=dict(type="str", default="tcp"),
            targets=dict(type="list", elements="dict", default=[]),
            health_check=dict(type="dict", default={}),
            state=dict(type="str", default="present", choices=["present", "absent"]),
        ),
        required_if=[("state", "present", ["port"])],
        supports_check_mode=True,
    )

    try:
        client = CloudCoreClient.from_module_params(module.params)
    except (ImportError, ValueError) as e:
        module.fail_json(msg=str(e))

    lb_id = module.params["lb_id"]
    name = module.params["name"]
    state = module.params["state"]
    base = f"/v1/load-balancers/{lb_id}/target-groups"

    target_groups = client.get(base).get("items", [])
    existing = next((tg for tg in target_groups if tg.get("name") == name), None)

    if state == "absent":
        if not existing:
            module.exit_json(changed=False)
        if not module.check_mode:
            client.delete(f"{base}/{existing['id']}")
        module.exit_json(changed=True)

    body = {
        "name": name,
        "port": module.params["port"],
        "protocol": module.params["protocol"],
        "targets": module.params["targets"],
        "health_check": module.params["health_check"],
    }

    if not existing:
        if module.check_mode:
            module.exit_json(changed=True, target_group={})
        result = client.post(base, body)
        module.exit_json(changed=True, target_group=result)

    # health_check is intentionally excluded: the API fills in defaults
    # server-side for any key left out of the input (path/interval/
    # healthy_threshold/unhealthy_threshold), so a stored value can never
    # equal a plain {} re-sent on every run — that would make this
    # perpetually "changed" for the common case of not overriding it.
    changed = (
        existing.get("port") != body["port"]
        or existing.get("protocol") != body["protocol"]
        or existing.get("targets") != body["targets"]
    )
    if not changed:
        module.exit_json(changed=False, target_group=existing)
    if module.check_mode:
        module.exit_json(changed=True, target_group=existing)
    result = client.put(f"{base}/{existing['id']}", body)
    module.exit_json(changed=True, target_group=result)


def main():
    run_module()


if __name__ == "__main__":
    main()
