#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: nfs_mount
short_description: Inject NFS mount into a CloudCore instance at launch
description:
  - Fetches mount config from the NFS server and returns the cloud-init
    mounts entry and runcmd snippet to embed in an instance's user_data.
  - This module is declarative — it does not SSH into a running instance.
    Use the returned values in the instance module's user_data parameter.
options:
  api_url:
    type: str
  api_token:
    type: str
    no_log: true
  nfs_server_id:
    type: str
    required: true
  share_name:
    type: str
    required: true
  mount_point:
    type: str
    default: ""
"""

EXAMPLES = r"""
- name: Get NFS mount config for shared-data share
  cloudcore.cloudcore.nfs_mount:
    nfs_server_id: "{{ nfs.nfs_server.id }}"
    share_name: shared-data
  register: mount_cfg

- name: Launch instance with NFS share mounted
  cloudcore.cloudcore.instance:
    name: "{{ project }}-{{ env }}-{{ build_suffix }}-web-01"
    image_id: ubuntu-22.04
    flavor: standard.small
    vpc_id: "{{ vpc.vpc.id }}"
    subnet_id: subnet-local-01
    user_data: |
      #cloud-config
      packages:
        - nfs-common
      mounts:
        - {{ mount_cfg.cloud_init_entry | to_json }}
      runcmd:
        - mkdir -p {{ mount_cfg.mount_point }}
"""

RETURN = r"""
nfs_server_ip:
  description: Private IP of the NFS server.
  type: str
export_path:
  description: Export path on the NFS server (e.g. /exports/shared-data).
  type: str
mount_point:
  description: Mount point on the instance.
  type: str
mount_command:
  description: Ready-to-run mount command.
  type: str
cloud_init_entry:
  description: List entry for cloud-init mounts block.
  type: list
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.cloudcore.cloudcore.plugins.module_utils.cloudcore_client import CloudCoreClient


def run_module():
    module = AnsibleModule(
        argument_spec=dict(
            api_url=dict(type="str"),
            api_token=dict(type="str", no_log=True),
            nfs_server_id=dict(type="str", required=True),
            share_name=dict(type="str", required=True),
            mount_point=dict(type="str", default=""),
        ),
        supports_check_mode=True,
    )

    try:
        client = CloudCoreClient.from_module_params(module.params)
    except (ImportError, ValueError) as e:
        module.fail_json(msg=str(e))

    nfs_id     = module.params["nfs_server_id"]
    share_name = module.params["share_name"]

    try:
        cfg = client.get(f"/v1/nfs-servers/{nfs_id}/shares/{share_name}/mount-config")
    except Exception as e:
        module.fail_json(msg=f"Failed to get mount config: {e}")

    # Allow caller to override mount point
    if module.params["mount_point"]:
        cfg["mount_point"] = module.params["mount_point"]
        cfg["cloud_init_entry"][1] = module.params["mount_point"]

    module.exit_json(changed=False, **cfg)


def main():
    run_module()


if __name__ == "__main__":
    main()
