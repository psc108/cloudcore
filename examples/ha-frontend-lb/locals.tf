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

  nginx_stream_conf = templatefile("${path.module}/files/nginx-stream.conf.tftpl", {
    proxysql_ips = values(module.proxysql.private_ips_by_key)
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
        nginx_stream_conf    = local.nginx_stream_conf
      })
    }
  }

  # Lab-only placeholders, not production secrets — same convention as
  # vrrp_auth_pass above.
  mysql_group_name     = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
  mysql_repl_password  = "changeme-repl"
  mysql_monitor_password = "changeme-monitor"
  mysql_app_password   = "changeme-app"

  # Node "a" only — modules/compute's per-key user_data can't reference
  # that same module call's own private_ips_by_key output, so it's split
  # into two module calls (bootstrap node, then joiners referencing its
  # now-known IP) — see haFullStack-LLD.md §2.3.2/§2.7. Joiners seed off
  # node "a" alone; Group Replication's gossip protocol discovers full
  # membership once joined.
  mysql_bootstrap_instances = {
    "mysql-a${local.sfx}" = {
      image_id           = "ubuntu-22.04"
      flavor             = var.mysql_flavor
      vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = [module.security_groups.security_group_ids_by_key["mysql${local.sfx}"]]
      user_data = templatefile("${path.module}/files/mysql-cloud-init.yaml.tftpl", {
        server_id        = 1
        is_bootstrap      = true
        group_name        = local.mysql_group_name
        group_seeds       = ""
        repl_password     = local.mysql_repl_password
        monitor_password  = local.mysql_monitor_password
        app_password      = local.mysql_app_password
      })
    }
  }

  mysql_replica_roles = {
    b = { server_id = 2 }
    c = { server_id = 3 }
  }

  mysql_replica_instances = {
    for role, cfg in local.mysql_replica_roles : "mysql-${role}${local.sfx}" => {
      image_id           = "ubuntu-22.04"
      flavor             = var.mysql_flavor
      vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = [module.security_groups.security_group_ids_by_key["mysql${local.sfx}"]]
      user_data = templatefile("${path.module}/files/mysql-cloud-init.yaml.tftpl", {
        server_id        = cfg.server_id
        is_bootstrap      = false
        group_name        = local.mysql_group_name
        group_seeds       = "${values(module.mysql_bootstrap.private_ips_by_key)[0]}:33061"
        repl_password     = local.mysql_repl_password
        monitor_password  = local.mysql_monitor_password
        app_password      = local.mysql_app_password
      })
    }
  }

  proxysql_deb_version = "3.0.11"
  proxysql_deb_url      = "https://github.com/sysown/proxysql/releases/download/v${local.proxysql_deb_version}/proxysql_${local.proxysql_deb_version}-ubuntu22_amd64.deb"
  proxysql_deb_sha256    = "f06a27b32fb96e42aa1b7f71bedd39600101b920f6f1e44e9fd5945c92a2b5b0"

  # All 3 MySQL nodes' IPs — proxysql is created after both mysql module
  # calls, so both are already known real addresses by this point (same
  # mechanism as frontend -> nginx in §1).
  mysql_all_ips = concat(
    values(module.mysql_bootstrap.private_ips_by_key),
    values(module.mysql_replicas.private_ips_by_key),
  )

  proxysql_user_data = templatefile("${path.module}/files/proxysql-cloud-init.yaml.tftpl", {
    proxysql_deb_url     = local.proxysql_deb_url
    proxysql_deb_sha256  = local.proxysql_deb_sha256
    mysql_ips             = local.mysql_all_ips
    monitor_password      = local.mysql_monitor_password
    app_password           = local.mysql_app_password
  })

  frontend_user_data = templatefile("${path.module}/files/frontend-cloud-init.yaml.tftpl", {
    vip_address  = var.vip_address
    app_password = local.mysql_app_password
  })
}
