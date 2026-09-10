#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: usb_device_info
short_description: List USB devices attached to the CloudCore host
description:
  - Fetches the live list of USB devices attached to the CloudCore host,
    with safety/attachment status, from the CloudCore API.
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
- name: List host USB devices
  cloudcore.cloudcore.usb_device_info:
  register: usb

- name: Show devices
  ansible.builtin.debug:
    var: usb.devices
"""

RETURN = r"""
devices:
  description: >-
    List of USB devices on the host, each with id ("vendor_id:product_id"),
    vendor_id, product_id, description, bus, device, blocked, block_reason,
    attached_to, and likely_wifi_adapter.
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
        result = client.get("/v1/usb-devices")
    except Exception as e:
        module.fail_json(msg=f"Failed to list USB devices: {e}")

    module.exit_json(changed=False, devices=result.get("items", []))


def main():
    run_module()


if __name__ == "__main__":
    main()
