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

  # Every instance in this stack gets an "ecs" user with passwordless sudo
  # for operational/config-management access, distinct from the default
  # "ubuntu" image user. No ssh_keys given here — cloudcore_instance's
  # users block always adds the CloudCore inter-instance keypair to every
  # extra user automatically (both inbound authorized_keys and outbound
  # ~/.ssh/), which is exactly what lets ecs SSH from any node in this
  # stack to any other node's ecs account without a separate key to manage.
  ecs_user = [{ username = "ecs", sudo = true }]

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

  # Keystone no longer has an http{}-context vhost of its own — :5000 is
  # closed, per direct instruction, leaving only :35357 (this project's
  # real-world admin-port convention: keystone-wsgi-admin, /api/idm),
  # which is a stream{} passthrough (nginx_stream_conf above,
  # keystone_tls_ips) like every other TLS port in this stack. Not :443
  # — that's the frontend tier's own client-facing port on this same
  # VIP. nginx-keystone.conf.tftpl (the file that used to hold this) is
  # deleted, not just unreferenced.

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
  # once per vhost-style config in this setup, and both are http{}-context
  # server{} blocks that coexist fine in one file (each with its own
  # listen directive: 80, 8080). Keystone no longer has one of its own —
  # see the comment above where nginx_keystone_conf used to be.
  nginx_conf = "${local.nginx_frontend_conf}\n${local.nginx_backend_conf}"

  # The application deployed onto these nodes afterward (not by this
  # template) is what would actually use these — provisioned now so that
  # step exists, following haFullStack-LLD.md §5.7's already-specified
  # design ("a backend service would request its own [cert] from the
  # same CA using the same mechanism") rather than inventing anything new.
  backend_user_data = templatefile("${path.module}/files/backend-cloud-init.yaml.tftpl", {
    ca_ip                   = local.ca_ip
    ca_provisioner_password = local.ca_provisioner_password
    step_cli_deb_sha256     = local.step_cli_deb_sha256
    nfs_ip                  = local.nfs_ip
    nfs_share               = local.nfs_share
    nfs_mount_dir           = local.nfs_mount_dir
    promtail_config         = local.promtail_config
  })

  # Pinned, checksum-verified — same pattern as proxysql_deb_sha256 below.
  step_ca_deb_sha256  = "f8e43f0f2ba1e37121b75623993ea0bece5cc3a02b73eefc16e414d41c9fec71"
  step_cli_deb_sha256 = "5845c181251ffe43ca2331bc171e0b92324a71be9cf4ef76cd6fbbba4f2a3cc6"

  # vpc_id/subnet_id/security_group_ids/peer_id swap together to the
  # peer's own catalogue when ca_peer_id is set -- see variables.tf's
  # own comment for why. Empty ca_peer_id (the default) keeps this
  # instance local, unchanged default behavior.
  ca_instance = {
    "ca-a${local.sfx}" = {
      image_id           = "ubuntu-22.04"
      flavor             = var.ca_flavor
      vpc_id             = var.ca_peer_id != "" ? var.ca_peer_vpc_id : module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = var.ca_peer_id != "" ? var.ca_peer_subnet_id : module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = var.ca_peer_id != "" ? [var.ca_peer_security_group_id] : [module.security_groups.security_group_ids_by_key["ca${local.sfx}"]]
      peer_id            = var.ca_peer_id != "" ? var.ca_peer_id : null
      users              = local.ecs_user
      user_data = templatefile("${path.module}/files/ca-cloud-init.yaml.tftpl", {
        step_ca_deb_sha256   = local.step_ca_deb_sha256
        step_cli_deb_sha256  = local.step_cli_deb_sha256
        provisioner_password = random_id.ca_provisioner_password.hex
        promtail_config      = local.promtail_config
      })
    }
  }

  # Every other tier's own cloud-init needs this — the CA's IP to wait on
  # and issue certificates against, and the shared provisioner password.
  ca_ip                   = values(module.ca.private_ips_by_key)[0]
  ca_provisioner_password = random_id.ca_provisioner_password.hex

  # Centralized logging (Loki + Grafana, haFullStack-LLD.md §12) is a
  # host-level, always-on platform capability now
  # (api/setup-logging-service.sh), not a per-example node — every
  # example's own promtail ships to the same fixed address
  # (192.168.100.1:3100), the same pattern already used for the
  # host-level package repo (192.168.100.1:8090, §7). This stack used
  # to run its own dedicated "logging" node purely so every other
  # tier's promtail had somewhere to point — no longer needed, and
  # removing it drops this stack back to 16 nodes.
  #
  # Genuinely static now — no templatefile() interpolation needed at
  # all, since the address is a fixed constant rather than a
  # discovered per-build IP (which is also what retired the circular-
  # dependency workaround this stack briefly needed for the logging
  # node to watch itself: the host-level service doesn't have that
  # problem, since it was never a guest instance in the first place).
  # Rendered once and embedded (via indent(), same reuse pattern as
  # nginx_conf/nginx_stream_conf above) into every tier's own
  # write_files — see files/promtail-config.yml for what it actually
  # ships (journal + cloud-init-output.log + per-service log files,
  # all labeled by this node's own hostname, fixed up at boot the same
  # way frontend's own index.html is).
  promtail_config = file("${path.module}/files/promtail-config.yml")

  # Frontend + backend both mount this share at boot — see
  # setup-nfs-mount.sh in each tier's own cloud-init. api/nfs.py's
  # mount_command()/cloud_init_mount_entry() define the exact
  # "<ip>:/exports/<share>" export path shape reused there.
  nfs_ip        = values(module.nfs.private_ips_by_key)[0]
  nfs_share     = "shared"
  nfs_mount_dir = "/mnt/shared"

  # Every admin/service-account login credential in this stack (MySQL's
  # replication/monitor/app/keystone/ssp_* accounts, Keystone's bootstrap
  # admin and system-domain service accounts, RabbitMQ's admin and
  # application service accounts) shares var.admin_password — one
  # settable value instead of a scattered "changeme-*" placeholder per
  # service. Purely internal, non-login secrets (VRRP, the Erlang
  # cookie, Fernet keys, the CA provisioner password) are deliberately
  # NOT wired to this — see variables.tf's admin_password description.
  mysql_group_name        = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
  mysql_repl_password     = var.admin_password
  mysql_monitor_password  = var.admin_password
  mysql_app_password      = var.admin_password
  mysql_keystone_password = var.admin_password
  mysql_ssp_password      = var.admin_password

  # MySQL's actual root account, per direct instruction — two accounts,
  # both var.admin_password:
  #   - 'root'@'localhost': every node's own OS-package-default root
  #     account, switched from auth_socket (no password, OS-user-match
  #     only) to a real password, for manual/ad-hoc admin work directly
  #     on a MySQL instance. Set identically on every node (bootstrap and
  #     replicas both), same reasoning as the repl account above — a
  #     local-only account each node owns independently, not something
  #     replication should propagate.
  #   - 'root'@'%': a new, genuinely network-reachable superuser account
  #     for the backend application (haFullStack-LLD.md — backend
  #     connects remotely, via the shared VIP -> ProxySQL, same as
  #     appuser/keystone/ssp_*, which already reliably routes to
  #     whichever node GR currently elects primary — a raw bypass
  #     straight to one MySQL node's own IP would need its own primary-
  #     discovery logic the backend doesn't have). Created once on the
  #     bootstrap node alongside appuser/keystone/ssp_*, replicates
  #     normally via Group Replication.
  mysql_root_password = var.admin_password

  # Shared across all 22 "system" domain service/admin accounts created
  # by setup-keystone-roles.sh's system_domain.yml import.
  keystone_system_domain_password = var.admin_password

  # Shared across the 13 application service accounts setup-rabbitmq.sh
  # creates on the seed (create_users.sh/create_vhost.sh, ported in).
  rabbitmq_services_password = var.admin_password

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
      users              = local.ecs_user
      user_data = templatefile("${path.module}/files/mysql-cloud-init.yaml.tftpl", {
        server_id               = 1
        is_bootstrap            = true
        group_name              = local.mysql_group_name
        group_seeds             = ""
        repl_password           = local.mysql_repl_password
        monitor_password        = local.mysql_monitor_password
        app_password            = local.mysql_app_password
        keystone_password       = local.mysql_keystone_password
        ssp_password            = local.mysql_ssp_password
        root_password           = local.mysql_root_password
        ca_ip                   = local.ca_ip
        step_cli_deb_sha256     = local.step_cli_deb_sha256
        ca_provisioner_password = local.ca_provisioner_password
        vip_address             = var.vip_address
        promtail_config         = local.promtail_config
      })
    }
  }

  mysql_replica_roles = {
    b = { server_id = 2 }
    c = { server_id = 3 }
  }

  # Per-node peer placement, node "a" (mysql_bootstrap_instances above)
  # excluded -- it's this tier's anchor and stays local-only, same as
  # load-balanced-web's own "01" anchor. peer_id/vpc_id/subnet_id/
  # security_group_ids swap together to that node's own peer's catalogue
  # when its own <role>_peer_id is set -- see variables.tf's own comment
  # for why. Empty (the default) keeps every node local, unchanged
  # default behavior.
  mysql_replica_peer = {
    b = { peer_id = var.mysql_b_peer_id, vpc_id = var.mysql_b_peer_vpc_id, subnet_id = var.mysql_b_peer_subnet_id, security_group_ids = [var.mysql_b_peer_security_group_id] }
    c = { peer_id = var.mysql_c_peer_id, vpc_id = var.mysql_c_peer_vpc_id, subnet_id = var.mysql_c_peer_subnet_id, security_group_ids = [var.mysql_c_peer_security_group_id] }
  }

  mysql_replica_instances = {
    for role, cfg in local.mysql_replica_roles : "mysql-${role}${local.sfx}" => {
      image_id           = "ubuntu-22.04"
      flavor             = var.mysql_flavor
      vpc_id             = local.mysql_replica_peer[role].peer_id != "" ? local.mysql_replica_peer[role].vpc_id : module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = local.mysql_replica_peer[role].peer_id != "" ? local.mysql_replica_peer[role].subnet_id : module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = local.mysql_replica_peer[role].peer_id != "" ? local.mysql_replica_peer[role].security_group_ids : [module.security_groups.security_group_ids_by_key["mysql${local.sfx}"]]
      peer_id            = local.mysql_replica_peer[role].peer_id != "" ? local.mysql_replica_peer[role].peer_id : null
      users              = local.ecs_user
      user_data = templatefile("${path.module}/files/mysql-cloud-init.yaml.tftpl", {
        server_id               = cfg.server_id
        is_bootstrap            = false
        group_name              = local.mysql_group_name
        group_seeds             = "${values(module.mysql_bootstrap.private_ips_by_key)[0]}:33061"
        repl_password           = local.mysql_repl_password
        monitor_password        = local.mysql_monitor_password
        app_password            = local.mysql_app_password
        keystone_password       = local.mysql_keystone_password
        ssp_password            = local.mysql_ssp_password
        root_password           = local.mysql_root_password
        ca_ip                   = local.ca_ip
        step_cli_deb_sha256     = local.step_cli_deb_sha256
        ca_provisioner_password = local.ca_provisioner_password
        vip_address             = var.vip_address
        promtail_config         = local.promtail_config
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
  # Per-node peer placement, node "a" (MASTER) excluded -- it's this
  # tier's anchor and stays local-only, same as load-balanced-web's own
  # "01" anchor. Only "b" (BACKUP) is independently placeable. peer_id/
  # vpc_id/subnet_id/security_group_ids swap together to that peer's
  # own catalogue when proxysql_b_peer_id is set -- see variables.tf's
  # own comment for why. Empty (the default) keeps both nodes local,
  # unchanged default behavior.
  proxysql_instances = {
    for role, cfg in local.proxysql_roles : "proxysql-${role}${local.sfx}" => {
      image_id  = "ubuntu-22.04"
      flavor    = var.proxysql_flavor
      vpc_id    = role == "b" && var.proxysql_b_peer_id != "" ? var.proxysql_b_peer_vpc_id : module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id = role == "b" && var.proxysql_b_peer_id != "" ? var.proxysql_b_peer_subnet_id : module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = role == "b" && var.proxysql_b_peer_id != "" ? [var.proxysql_b_peer_security_group_id] : [
        module.security_groups.security_group_ids_by_key["proxysql${local.sfx}"]
      ]
      peer_id = role == "b" && var.proxysql_b_peer_id != "" ? var.proxysql_b_peer_id : null
      users   = local.ecs_user
      user_data = templatefile("${path.module}/files/proxysql-cloud-init.yaml.tftpl", {
        proxysql_deb_url        = local.proxysql_deb_url
        proxysql_deb_sha256     = local.proxysql_deb_sha256
        mysql_ips               = local.mysql_all_ips
        monitor_password        = local.mysql_monitor_password
        app_password            = local.mysql_app_password
        keystone_password       = local.mysql_keystone_password
        root_password           = local.mysql_root_password
        ca_ip                   = local.ca_ip
        ca_provisioner_password = local.ca_provisioner_password
        vip_address             = var.vip_address
        promtail_config         = local.promtail_config
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
    promtail_config         = local.promtail_config
    app_password            = local.mysql_app_password
    admin_password          = var.admin_password
    keystone_ips            = values(module.keystone.private_ips_by_key)
    ca_ip                   = local.ca_ip
    ca_provisioner_password = local.ca_provisioner_password
    step_cli_deb_sha256     = local.step_cli_deb_sha256
    nfs_ip                  = local.nfs_ip
    nfs_share               = local.nfs_share
    nfs_mount_dir           = local.nfs_mount_dir
  })

  memcached_user_data = templatefile("${path.module}/files/memcached-cloud-init.yaml.tftpl", {
    promtail_config = local.promtail_config
  })

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
    vip_address                     = var.vip_address
    root_password                   = local.mysql_root_password
    memcached_servers_csv           = local.memcached_servers_csv
    fernet_key0                     = "${random_id.fernet_key0.b64_url}="
    fernet_key1                     = "${random_id.fernet_key1.b64_url}="
    ca_ip                           = local.ca_ip
    ca_provisioner_password         = local.ca_provisioner_password
    step_cli_deb_sha256             = local.step_cli_deb_sha256
    keystone_system_domain_password = local.keystone_system_domain_password
    admin_password                  = var.admin_password
    promtail_config                 = local.promtail_config
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
      users              = local.ecs_user
      user_data = templatefile("${path.module}/files/rabbitmq-cloud-init.yaml.tftpl", {
        is_seed                    = true
        seed_ip                    = ""
        erlang_cookie              = random_id.erlang_cookie.b64_url
        ca_ip                      = local.ca_ip
        ca_provisioner_password    = local.ca_provisioner_password
        vip_address                = var.vip_address
        promtail_config            = local.promtail_config
        step_cli_deb_sha256        = local.step_cli_deb_sha256
        rabbitmq_services_password = local.rabbitmq_services_password
        admin_password             = var.admin_password
      })
    }
  }

  rabbitmq_joiner_roles = {
    b = {}
    c = {}
  }

  # Per-node peer placement, node "a" (rabbitmq_seed_instance above)
  # excluded -- it's this tier's anchor and stays local-only, same as
  # load-balanced-web's own "01" anchor. peer_id/vpc_id/subnet_id/
  # security_group_ids swap together to that node's own peer's
  # catalogue when its own <role>_peer_id is set -- see variables.tf's
  # own comment for why. Empty (the default) keeps every node local,
  # unchanged default behavior.
  rabbitmq_joiner_peer = {
    b = { peer_id = var.rabbitmq_b_peer_id, vpc_id = var.rabbitmq_b_peer_vpc_id, subnet_id = var.rabbitmq_b_peer_subnet_id, security_group_ids = [var.rabbitmq_b_peer_security_group_id] }
    c = { peer_id = var.rabbitmq_c_peer_id, vpc_id = var.rabbitmq_c_peer_vpc_id, subnet_id = var.rabbitmq_c_peer_subnet_id, security_group_ids = [var.rabbitmq_c_peer_security_group_id] }
  }

  rabbitmq_joiner_instances = {
    for role, cfg in local.rabbitmq_joiner_roles : "rabbitmq-${role}${local.sfx}" => {
      image_id           = "ubuntu-22.04"
      flavor             = var.rabbitmq_flavor
      vpc_id             = local.rabbitmq_joiner_peer[role].peer_id != "" ? local.rabbitmq_joiner_peer[role].vpc_id : module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id          = local.rabbitmq_joiner_peer[role].peer_id != "" ? local.rabbitmq_joiner_peer[role].subnet_id : module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = local.rabbitmq_joiner_peer[role].peer_id != "" ? local.rabbitmq_joiner_peer[role].security_group_ids : [module.security_groups.security_group_ids_by_key["rabbitmq${local.sfx}"]]
      peer_id            = local.rabbitmq_joiner_peer[role].peer_id != "" ? local.rabbitmq_joiner_peer[role].peer_id : null
      users              = local.ecs_user
      user_data = templatefile("${path.module}/files/rabbitmq-cloud-init.yaml.tftpl", {
        is_seed                    = false
        seed_ip                    = values(module.rabbitmq_seed.private_ips_by_key)[0]
        erlang_cookie              = random_id.erlang_cookie.b64_url
        ca_ip                      = local.ca_ip
        ca_provisioner_password    = local.ca_provisioner_password
        vip_address                = var.vip_address
        promtail_config            = local.promtail_config
        step_cli_deb_sha256        = local.step_cli_deb_sha256
        rabbitmq_services_password = local.rabbitmq_services_password
        admin_password             = var.admin_password
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

  # Short-hostname DNS, one A record per instance in the stack, so ecs (or
  # anyone) can "ssh <name>" between nodes instead of needing the private
  # IP or the full FQDN — the platform's DHCP domain-search option
  # (instances.cloudcore.internal, set up by api/setup-network.sh) makes
  # the bare name resolve. modules/compute's private_ips_by_key is already
  # keyed by the exact short name (e.g. "mysql-a", "proxysql-b") since
  # that's the caller-supplied key in var.instances; modules/instance-group
  # is keyed by two-digit index only ("01", "02"), so those four tiers
  # need their group name prefixed back on here.
  dns_records = merge(
    { for k, ip in module.ca.private_ips_by_key : k => ip },
    { for k, ip in module.nfs.private_ips_by_key : k => ip },
    { for k, ip in module.mysql_bootstrap.private_ips_by_key : k => ip },
    { for k, ip in module.mysql_replicas.private_ips_by_key : k => ip },
    { for k, ip in module.proxysql.private_ips_by_key : k => ip },
    { for k, ip in module.rabbitmq_seed.private_ips_by_key : k => ip },
    { for k, ip in module.rabbitmq_joiners.private_ips_by_key : k => ip },
    { for k, ip in module.frontend.private_ips_by_key : "frontend${local.sfx}-${k}" => ip },
    { for k, ip in module.memcached.private_ips_by_key : "memcached${local.sfx}-${k}" => ip },
    { for k, ip in module.keystone.private_ips_by_key : "keystone${local.sfx}-${k}" => ip },
    { for k, ip in module.backend.private_ips_by_key : "backend${local.sfx}-${k}" => ip },
  )
}
