#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
module: nfs_server
short_description: Manage CloudCore NFS servers
options:
  api_url:
    type: str
  api_token:
    type: str
    no_log: true
  name:
    type: str
    required: true
  vpc_id:
    type: str
  flavor:
    type: str
    default: standard.medium
  disk_gb:
    type: int
    default: 20
  shares:
    type: list
    elements: dict
    default: []
    suboptions:
      name:
        type: str
        required: true
      clients:
        type: raw
        default: vpc
  tags:
    type: dict
    default: {}
  state:
    type: str
    choices: [present, absent]
    default: present
"""

EXAMPLES = r"""
- name: Create NFS server
  cloudcore.cloudcore.nfs_server:
    name: "{{ project }}-{{ env }}-{{ build_suffix }}-nfs"
    vpc_id: "{{ vpc.vpc.id }}"
    flavor: standard.medium
    disk_gb: 50
    shares:
      - name: shared-data
        clients: vpc
      - name: config
        clients:
          - 10.10.0.5
          - 10.10.0.6
    tags:
      Environment: "{{ env }}"
      Project: "{{ project }}"
"""

RETURN = r"""
nfs_server:
  description: NFS server object returned by the API.
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
            flavor=dict(type="str", default="standard.medium"),
            disk_gb=dict(type="int", default=20),
            shares=dict(type="list", elements="dict", default=[]),
            tags=dict(type="dict", default={}),
            state=dict(type="str", default="present", choices=["present", "absent"]),
        ),
        supports_check_mode=True,
    )

    try:
        client = CloudCoreClient.from_module_params(module.params)
    except (ImportError, ValueError) as e:
        module.fail_json(msg=str(e))

    name  = module.params["name"]
    state = module.params["state"]
    existing = client.find_by_name("/v1/nfs-servers", name)

    if state == "absent":
        if not existing:
            module.exit_json(changed=False)
        if not module.check_mode:
            client.delete(f"/v1/nfs-servers/{existing['id']}")
        module.exit_json(changed=True)

    if existing:
        module.exit_json(changed=False, nfs_server=existing)

    if module.check_mode:
        module.exit_json(changed=True, nfs_server={})

    body = {
        "name": name,
        "vpc_id": module.params["vpc_id"],
        "flavor": module.params["flavor"],
        "disk_gb": module.params["disk_gb"],
        "shares": module.params["shares"],
        "tags": module.params["tags"],
    }
    result = client.post("/v1/nfs-servers", body)
    module.exit_json(changed=True, nfs_server=result)


def main():
    run_module()


if __name__ == "__main__":
    main()
