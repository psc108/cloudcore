#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: peer_info
short_description: List CloudCore peers this host has paired with
description:
  - Fetches the live list of paired CloudCore hosts (cross-host peering)
    from the API — see the dashboard's own Peers section for discovering
    and approving new pairings; this module only lists already-established
    ones.
  - Read-only — this module is declarative and never changes anything.
options:
  api_url:
    description: CloudCore API URL. Defaults to CLOUDCORE_API_URL env var.
    type: str
  api_token:
    description: CloudCore API token. Defaults to CLOUDCORE_API_TOKEN env var.
    type: str
    no_log: true
"""

EXAMPLES = r"""
- name: List paired peers
  cloudcore.cloudcore.peer_info:
  register: peers

- name: Create an instance on the first approved peer
  cloudcore.cloudcore.instance:
    name: cluster-node-02
    image_id: ubuntu-22.04
    flavor: standard.small
    vpc_id: "{{ remote_vpc_id }}"
    subnet_id: "{{ remote_subnet_id }}"
    peer_id: "{{ (peers.peers | selectattr('status', 'eq', 'approved') | first).id }}"
"""

RETURN = r"""
peers:
  description: >-
    List of peers this host has ever paired with, any status. Each has id
    (pass as instance's peer_id), hostname, status (pending_outbound |
    approved | rejected | revoked), pubkey_fpr, wg_tunnel_status
    (up | down | unknown), and wg_bridge_subnet (the peer's own bridge
    subnet — the range an instance placed there gets its address from).
  returned: always
  type: list
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.cloudcore.cloudcore.plugins.module_utils.cloudcore_client import CloudCoreClient


def run_module():
    module = AnsibleModule(
        argument_spec=dict(
            api_url=dict(type="str"),
            api_token=dict(type="str", no_log=True),
        ),
        supports_check_mode=True,
    )

    try:
        client = CloudCoreClient.from_module_params(module.params)
    except (ImportError, ValueError) as e:
        module.fail_json(msg=str(e))

    try:
        result = client.get("/v1/peers")
    except Exception as e:
        module.fail_json(msg=f"Failed to list peers: {e}")

    module.exit_json(changed=False, peers=result.get("items", []))


def main():
    run_module()


if __name__ == "__main__":
    main()
