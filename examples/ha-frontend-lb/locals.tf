locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  # The Lab platform's bridged guest network (ccbr0) is one fixed L2/L3
  # segment (192.168.100.0/24, set up by api/setup-network.sh) shared by
  # every VPC on this host — bridged instances get their real address
  # from its DHCP pool regardless of the CloudCore VPC/subnet CIDR
  # objects declared below. The VIP and the VRRP security-group rule both
  # have to live in this real subnet, not var.cidr_block.
  bridge_cidr = "192.168.100.0/24"

  # VRRP is IP protocol 112, which this platform's security-group model
  # doesn't expose individually (tcp/udp/icmp/-1 only — see
  # modules/security-groups) — see the "-1"-scoped ingress rule in
  # main.tf's nginx security group.
  vrrp_router_id = 51
  vrrp_auth_pass = "changeme-vrrp" # lab-only placeholder, not a production secret

  # Both NGINX nodes run an identical NGINX/Keepalived setup differing
  # only in Keepalived's state/priority — multicast VRRP was empirically
  # validated to work over ccbr0, so no unicast_peer/per-node IP wiring
  # is needed (see haFullStack-LLD.md §1.3).
  nginx_roles = {
    a = { state = "MASTER", priority = 150 }
    b = { state = "BACKUP", priority = 100 }
  }

  nginx_frontend_conf = templatefile("${path.module}/files/nginx-frontend.conf.tftpl", {
    frontend_ips = values(module.frontend.private_ips_by_key)
  })

  nginx_instances = {
    for role, cfg in local.nginx_roles : "nginx-${role}${local.sfx}" => {
      image_id            = "ubuntu-22.04"
      flavor               = var.nginx_flavor
      vpc_id               = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id            = module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids   = [module.security_groups.security_group_ids_by_key["nginx${local.sfx}"]]
      user_data            = templatefile("${path.module}/files/nginx-cloud-init.yaml.tftpl", {
        keepalived_state    = cfg.state
        keepalived_priority = cfg.priority
        vip_address          = var.vip_address
        vrrp_router_id       = local.vrrp_router_id
        vrrp_auth_pass       = local.vrrp_auth_pass
        nginx_conf           = local.nginx_frontend_conf
      })
    }
  }

  frontend_user_data = file("${path.module}/files/frontend-cloud-init.yaml")
}
