#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: lb_listener
short_description: Manage CloudCore load balancer listeners
description:
  - Create, update or delete a listener on a CloudCore load balancer.
  - Listeners are looked up by port within the parent load balancer's own
    listener collection (a listener's port is unique per load balancer,
    the same constraint the API itself enforces on create).
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
  port:
    description: Port the listener binds on. Also the lookup key for idempotency.
    type: int
    required: true
  protocol:
    description: Listener protocol (http, https, tcp).
    type: str
    default: tcp
  target_group_id:
    description: Default target group to forward to.
    type: str
    default: ""
  routing_rules:
    description: Optional L7 routing rules (path_pattern/host_header based).
    type: list
    elements: dict
    default: []
  default_action:
    description: Default action when no routing rule matches.
    type: str
    default: forward
  state:
    description: Desired state.
    type: str
    choices: [present, absent]
    default: present
"""

EXAMPLES = r"""
- name: Create listener
  cloudcore.cloudcore.lb_listener:
    lb_id: "{{ lb.load_balancer.id }}"
    port: 8600
    protocol: tcp
    target_group_id: "{{ tg.target_group.id }}"

- name: Delete listener
  cloudcore.cloudcore.lb_listener:
    lb_id: "{{ lb.load_balancer.id }}"
    port: 8600
    state: absent
"""

RETURN = r"""
listener:
  description: Listener object returned by the API.
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
            port=dict(type="int", required=True),
            protocol=dict(type="str", default="tcp"),
            target_group_id=dict(type="str", default=""),
            routing_rules=dict(type="list", elements="dict", default=[]),
            default_action=dict(type="str", default="forward"),
            state=dict(type="str", default="present", choices=["present", "absent"]),
        ),
        supports_check_mode=True,
    )

    try:
        client = CloudCoreClient.from_module_params(module.params)
    except (ImportError, ValueError) as e:
        module.fail_json(msg=str(e))

    lb_id = module.params["lb_id"]
    port = module.params["port"]
    state = module.params["state"]
    base = f"/v1/load-balancers/{lb_id}/listeners"

    listeners = client.get(base).get("items", [])
    existing = next((l for l in listeners if l.get("port") == port), None)

    if state == "absent":
        if not existing:
            module.exit_json(changed=False)
        if not module.check_mode:
            client.delete(f"{base}/{existing['id']}")
        module.exit_json(changed=True)

    body = {
        "port": port,
        "protocol": module.params["protocol"],
        "target_group_id": module.params["target_group_id"],
        "routing_rules": module.params["routing_rules"],
        "default_action": module.params["default_action"],
    }

    if not existing:
        if module.check_mode:
            module.exit_json(changed=True, listener={})
        result = client.post(base, body)
        module.exit_json(changed=True, listener=result)

    # port/protocol are immutable via PUT on this resource (the API
    # silently ignores them in an update body) — only diff the fields
    # that actually change, so a mismatched port/protocol doesn't cause
    # a perpetual false "changed" that a PUT can never actually resolve.
    changed = (
        existing.get("target_group_id") != body["target_group_id"]
        or existing.get("routing_rules") != body["routing_rules"]
        or existing.get("default_action") != body["default_action"]
    )
    if not changed:
        module.exit_json(changed=False, listener=existing)
    if module.check_mode:
        module.exit_json(changed=True, listener=existing)
    result = client.put(f"{base}/{existing['id']}", body)
    module.exit_json(changed=True, listener=result)


def main():
    run_module()


if __name__ == "__main__":
    main()
