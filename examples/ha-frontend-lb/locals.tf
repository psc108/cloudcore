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
  # main.tf's proxysql security group.
  vrrp_router_id = 51
  vrrp_auth_pass = "changeme-vrrp" # lab-only placeholder, not a production secret

  # Both ProxySQL+NGINX nodes run identical setup differing only in
  # Keepalived's state/priority — multicast VRRP was empirically
  # validated to work over ccbr0, so no unicast_peer/per-node IP wiring
  # is needed (see haFullStack-LLD.md §1.3). Named proxysql_roles, not
  # nginx_roles — NGINX/Keepalived is co-located on the ProxySQL nodes
  # (haFullStack-LLD.md §1/§2), there's no separate nginx tier anymore.
  proxysql_roles = {
    a = { state = "MASTER", priority = 150 }
    b = { state = "BACKUP", priority = 100 }
  }

  nginx_frontend_conf = templatefile("${path.module}/files/nginx-frontend.conf.tftpl", {
    frontend_ips = values(module.frontend.private_ips_by_key)
    vip_address  = var.vip_address
  })

  # proxysql_ips is "127.0.0.1", not values(module.proxysql.private_ips_by_key)
  # — NGINX now runs co-located ON the ProxySQL nodes (haFullStack-LLD.md
  # §1/§2), so referencing module.proxysql's own output here would be a
  # circular dependency (this config is itself baked into that same
  # module's user_data). It's also more correct than the pre-merge
  # 2-node upstream list: Keepalived only ever routes real client
  # traffic to whichever node currently holds the VIP, and that node's
  # local ProxySQL instance is the only one that node's NGINX will ever
  # need to reach — the passive BACKUP node's NGINX never sees traffic to
  # fail over from in the first place.
  nginx_stream_conf = templatefile("${path.module}/files/nginx-stream.conf.tftpl", {
    proxysql_ips     = ["127.0.0.1"]
    rabbitmq_ips     = local.rabbitmq_all_ips
    keystone_tls_ips = values(module.keystone.private_ips_by_key)
  })

  # http{}-context server{} block for Keystone, sibling to the frontend
  # one — both end up in the same sites-available/default file (see
  # nginx_conf below), matching haFullStack-LLD.md §3.3.1's explicit
  # framing: dedicated port 5000, not vhost routing, per direct
  # confirmation this matches the real backend application's own
  # addressing scheme (not a DNS workaround — CloudCore's guest DNS is
  # fixed as of F-022, but that was never the reason for this choice).
  nginx_keystone_conf = templatefile("${path.module}/files/nginx-keystone.conf.tftpl", {
    keystone_ips = values(module.keystone.private_ips_by_key)
  })

  # Dedicated port (8080), not vhost routing — same reasoning and same
  # precedent as Keystone's own :5000 above, applied to backend now that
  # its tier actually exists (haFullStack-LLD.md §5.7's "Backend↔X mTLS"
  # open item). Upstream members listen on :80 (backend's own local
  # nginx, stock config) — haFullStack.md §3.1's old illustrative example
  # showed backend1:8080/backend2:8080 for the *upstream member* port;
  # corrected here since that's not what an unconfigured stock nginx
  # actually listens on, not a deviation from an established real value.
  nginx_backend_conf = templatefile("${path.module}/files/nginx-backend.conf.tftpl", {
    backend_ips = values(module.backend.private_ips_by_key)
  })

  # Concatenated, not separate files — sites-available/default only loads
  # once per vhost-style config in this setup, and all three are
  # http{}-context server{} blocks that coexist fine in one file (each
  # with its own listen directive: 80, 5000, 8080).
  nginx_conf = "${local.nginx_frontend_conf}\n${local.nginx_keystone_conf}\n${local.nginx_backend_conf}"

  # The application deployed onto these nodes afterward (not by this
  # template) is what would actually use these — provisioned now so that
  # step exists, following haFullStack-LLD.md §5.7's already-specified
  # design ("a backend service would request its own [cert] from the
  # same CA using the same mechanism") rather than inventing anything new.
  backend_user_data = templatefile("${path.module}/files/backend-cloud-init.yaml.tftpl", {
    ca_ip                   = local.ca_ip
    ca_provisioner_password = local.ca_provisioner_password
    step_cli_deb_sha256     = local.step_cli_deb_sha256
  })

  # Pinned, checksum-verified — same pattern as proxysql_deb_sha256 below.
  step_ca_deb_sha256  = "f8e43f0f2ba1e37121b75623993ea0bece5cc3a02b73eefc16e414d41c9fec71"
  step_cli_deb_sha256 = "5845c181251ffe43ca2331bc171e0b92324a71be9cf4ef76cd6fbbba4f2a3cc6"

  ca_instance = {
    "ca-a${local.sfx}" = {
      image_id           = "ubuntu-22.04"
      flavor             = var.ca_flavor
      vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = [module.security_groups.security_group_ids_by_key["ca${local.sfx}"]]
      user_data = templatefile("${path.module}/files/ca-cloud-init.yaml.tftpl", {
        step_ca_deb_sha256   = local.step_ca_deb_sha256
        step_cli_deb_sha256  = local.step_cli_deb_sha256
        provisioner_password = random_id.ca_provisioner_password.hex
      })
    }
  }

  # Every other tier's own cloud-init needs this — the CA's IP to wait on
  # and issue certificates against, and the shared provisioner password.
  ca_ip                   = values(module.ca.private_ips_by_key)[0]
  ca_provisioner_password = random_id.ca_provisioner_password.hex

  # Lab-only placeholders, not production secrets — same convention as
  # vrrp_auth_pass above.
  mysql_group_name        = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
  mysql_repl_password     = "changeme-repl"
  mysql_monitor_password  = "changeme-monitor"
  mysql_app_password      = "changeme-app"
  mysql_keystone_password = "changeme-keystone" # lab-only placeholder, not a production secret

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
        server_id               = 1
        is_bootstrap            = true
        group_name              = local.mysql_group_name
        group_seeds             = ""
        repl_password           = local.mysql_repl_password
        monitor_password        = local.mysql_monitor_password
        app_password            = local.mysql_app_password
        keystone_password       = local.mysql_keystone_password
        ca_ip                   = local.ca_ip
        step_cli_deb_sha256     = local.step_cli_deb_sha256
        ca_provisioner_password = local.ca_provisioner_password
        vip_address             = var.vip_address
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
        server_id               = cfg.server_id
        is_bootstrap            = false
        group_name              = local.mysql_group_name
        group_seeds             = "${values(module.mysql_bootstrap.private_ips_by_key)[0]}:33061"
        repl_password           = local.mysql_repl_password
        monitor_password        = local.mysql_monitor_password
        app_password            = local.mysql_app_password
        keystone_password       = local.mysql_keystone_password
        ca_ip                   = local.ca_ip
        step_cli_deb_sha256     = local.step_cli_deb_sha256
        ca_provisioner_password = local.ca_provisioner_password
        vip_address             = var.vip_address
      })
    }
  }

  proxysql_deb_version = "3.0.11"
  proxysql_deb_url     = "https://github.com/sysown/proxysql/releases/download/v${local.proxysql_deb_version}/proxysql_${local.proxysql_deb_version}-ubuntu22_amd64.deb"
  proxysql_deb_sha256  = "f06a27b32fb96e42aa1b7f71bedd39600101b920f6f1e44e9fd5945c92a2b5b0"

  # All 3 MySQL nodes' IPs — proxysql is created after both mysql module
  # calls, so both are already known real addresses by this point (same
  # mechanism as frontend -> nginx in §1).
  mysql_all_ips = concat(
    values(module.mysql_bootstrap.private_ips_by_key),
    values(module.mysql_replicas.private_ips_by_key),
  )

  # modules/compute (per-key instances map), not instance-group — the two
  # nodes need different Keepalived state/priority now that NGINX/
  # Keepalived is co-located here (haFullStack-LLD.md §1/§2), the same
  # reason the now-retired standalone "nginx" module used modules/compute
  # instead of instance-group.
  proxysql_instances = {
    for role, cfg in local.proxysql_roles : "proxysql-${role}${local.sfx}" => {
      image_id           = "ubuntu-22.04"
      flavor             = var.proxysql_flavor
      vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = [module.security_groups.security_group_ids_by_key["proxysql${local.sfx}"]]
      user_data = templatefile("${path.module}/files/proxysql-cloud-init.yaml.tftpl", {
        proxysql_deb_url        = local.proxysql_deb_url
        proxysql_deb_sha256     = local.proxysql_deb_sha256
        mysql_ips               = local.mysql_all_ips
        monitor_password        = local.mysql_monitor_password
        app_password            = local.mysql_app_password
        keystone_password       = local.mysql_keystone_password
        ca_ip                   = local.ca_ip
        ca_provisioner_password = local.ca_provisioner_password
        vip_address             = var.vip_address
        step_cli_deb_sha256     = local.step_cli_deb_sha256
        keepalived_state        = cfg.state
        keepalived_priority     = cfg.priority
        vrrp_router_id          = local.vrrp_router_id
        vrrp_auth_pass          = local.vrrp_auth_pass
        nginx_conf              = local.nginx_conf
        nginx_stream_conf       = local.nginx_stream_conf
      })
    }
  }

  frontend_user_data = templatefile("${path.module}/files/frontend-cloud-init.yaml.tftpl", {
    vip_address             = var.vip_address
    app_password            = local.mysql_app_password
    keystone_ips            = values(module.keystone.private_ips_by_key)
    ca_ip                   = local.ca_ip
    ca_provisioner_password = local.ca_provisioner_password
    step_cli_deb_sha256     = local.step_cli_deb_sha256
  })

  memcached_user_data = templatefile("${path.module}/files/memcached-cloud-init.yaml.tftpl", {})

  # keystone-manage db_sync/bootstrap connect through the VIP -> NGINX
  # stream{} -> ProxySQL -> MySQL path, same as appuser's clusterdemo
  # connection in §2 — proves the identity tier's DB dependency through
  # the same real path being validated elsewhere, not a direct backdoor
  # connection to one MySQL node.
  memcached_servers_csv = join(",", [for ip in values(module.memcached.private_ips_by_key) : "${ip}:11211"])

  # random_id.b64_url omits the trailing "=" padding that a 32-byte
  # value needs to be valid standard base64 (43 chars, 43 % 4 == 3, one
  # "=" short of a multiple of 4) — Python's base64.urlsafe_b64decode()
  # (used internally by keystone's Fernet token provider) requires it and
  # fails with "Incorrect padding" without it, found directly via a real
  # 500 on token issuance. keystone-manage fernet_setup's own generated
  # keys always carry this padding, which is what made the difference
  # obvious once compared side by side.
  keystone_user_data = templatefile("${path.module}/files/keystone-cloud-init.yaml.tftpl", {
    vip_address             = var.vip_address
    keystone_password       = local.mysql_keystone_password
    memcached_servers_csv   = local.memcached_servers_csv
    fernet_key0             = "${random_id.fernet_key0.b64_url}="
    fernet_key1             = "${random_id.fernet_key1.b64_url}="
    ca_ip                   = local.ca_ip
    ca_provisioner_password = local.ca_provisioner_password
    step_cli_deb_sha256     = local.step_cli_deb_sha256
  })

  # Seed node only — modules/compute's per-key user_data can't reference
  # that same module call's own private_ips_by_key output, so RabbitMQ
  # (like MySQL) is split into two module calls, seed then joiners
  # referencing its now-known IP — see haFullStack-LLD.md §4.1/§4.3.2.
  # Unlike MySQL, no gossip protocol discovers membership after joining;
  # each joiner explicitly targets the seed by IP, but that's still all
  # any single joiner needs — RabbitMQ clustering syncs full membership
  # to every node as each one joins.
  rabbitmq_seed_instance = {
    "rabbitmq-a${local.sfx}" = {
      image_id           = "ubuntu-22.04"
      flavor             = var.rabbitmq_flavor
      vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = [module.security_groups.security_group_ids_by_key["rabbitmq${local.sfx}"]]
      user_data = templatefile("${path.module}/files/rabbitmq-cloud-init.yaml.tftpl", {
        is_seed                 = true
        seed_ip                 = ""
        erlang_cookie           = random_id.erlang_cookie.b64_url
        ca_ip                   = local.ca_ip
        ca_provisioner_password = local.ca_provisioner_password
        vip_address             = var.vip_address
        step_cli_deb_sha256     = local.step_cli_deb_sha256
      })
    }
  }

  rabbitmq_joiner_roles = {
    b = {}
    c = {}
  }

  rabbitmq_joiner_instances = {
    for role, cfg in local.rabbitmq_joiner_roles : "rabbitmq-${role}${local.sfx}" => {
      image_id           = "ubuntu-22.04"
      flavor             = var.rabbitmq_flavor
      vpc_id             = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = [module.security_groups.security_group_ids_by_key["rabbitmq${local.sfx}"]]
      user_data = templatefile("${path.module}/files/rabbitmq-cloud-init.yaml.tftpl", {
        is_seed                 = false
        seed_ip                 = values(module.rabbitmq_seed.private_ips_by_key)[0]
        erlang_cookie           = random_id.erlang_cookie.b64_url
        ca_ip                   = local.ca_ip
        ca_provisioner_password = local.ca_provisioner_password
        vip_address             = var.vip_address
        step_cli_deb_sha256     = local.step_cli_deb_sha256
      })
    }
  }

  # All 3 RabbitMQ nodes' IPs — nginx is created after both rabbitmq
  # module calls, so both are already known real addresses by this point
  # (same mechanism as mysql_all_ips above).
  rabbitmq_all_ips = concat(
    values(module.rabbitmq_seed.private_ips_by_key),
    values(module.rabbitmq_joiners.private_ips_by_key),
  )
}
