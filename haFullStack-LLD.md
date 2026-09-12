# NGINX High-Availability Load Balancing Architecture — Low-Level Design

**Multi-Service Platform — Frontend, Backend, MySQL, Keystone, RabbitMQ**

v0.17 (in progress — built section by section) | Paul Scott

---

## How This Document Is Built

This LLD is being written and validated one slice at a time against the HLD
(`haFullStack-HLD.md`), rather than in a single pass — see the discussion
that opened this working session for why. Each slice gets its matching
section in the phased implementation document
(`haFullStack-Phased-Implementation.md`) at the same time, and both are
reviewed before the next slice starts. Every non-obvious issue found while
building or validating a slice — design gap or platform quirk — is logged
in `haFullStack-Findings-Log.md`, linked from the relevant point in the
slice below rather than repeated inline. Sections currently complete:

| # | Slice | Status |
|---|---|---|
| 1 | Load Balancer Tier — Client to Frontend (L7) | Lab/OpenTofu built and verified ([findings](haFullStack-Findings-Log.md#phase-1a--lab-opentofu)); On-Prem/AWS and Ansible still pending |
| 2 | Database Tier — MySQL High Availability | Lab/OpenTofu built, failure-tested, and F-021 fixed ([findings](haFullStack-Findings-Log.md#phase-2a--lab-opentofu-database-tier)); On-Prem/AWS and Ansible still pending |
| 3 | Identity Tier — Keystone | Built and failure-tested (Lab) |
| 4 | Message Broker Tier — RabbitMQ | Built and failure-tested (Lab) |
| 5 | TLS and Mutual TLS — Cross-Cutting | Draft, under review |

Per the session plan: every slice gets built and verified on Lab/OpenTofu
first, as one growing stack (not independent per-slice templates) —
Ansible, On-Prem, and AWS for the whole set only start once all slices
are proven on Lab/OpenTofu.

---

## 1. Load Balancer Tier — Client to Frontend (L7)

### 1.1 Scope

**In scope for this section:** the path from an external client request to
one of the two frontend instances — the load-balancer mechanism, its
redundancy, and the network path to the frontend tier only.

**Explicitly out of scope, for later slices:** backend and Keystone routing,
the TCP/`stream{}` tier (MySQL, RabbitMQ), TLS/mTLS detail, monitoring, and
the firewall/network-control detail beyond what this slice needs. Treating
frontend routing as its own slice is deliberate — HLD §2 groups frontend,
backend, and Keystone together as "the HTTP tier," but they don't have to be
built in one motion, and frontend is the simplest of the three (no
downstream dependency of its own).

### 1.2 Environment and Tooling Matrix

Three target environments were identified, each requiring a real (not
find-and-replace) design decision, plus a choice of IaC tool within each:

| Environment | Load-Balancing Mechanism | Why | IaC Tool(s) |
|---|---|---|---|
| **Lab** (this platform, CloudCore) | NGINX + Keepalived on 2 VMs | Validates the actual HLD-designed architecture (VRRP failover, quorum-adjacent behaviour) against real infrastructure, cheaply | OpenTofu or Ansible |
| **On-Prem** | NGINX + Keepalived on 2 VMs/hosts | Same architecture as Lab — this is the environment the HLD design was actually written for | OpenTofu or Ansible |
| **AWS** | Application Load Balancer (ALB) | AWS VPCs don't carry VRRP multicast/broadcast, so hand-rolled Keepalived failover isn't the natural fit — ALB provides equivalent HA as a managed, cross-AZ service instead | OpenTofu or Ansible |

Lab and On-Prem are architecturally identical (NGINX + Keepalived); AWS is a
genuinely different mechanism (managed L7 load balancer, no Keepalived at
all). This confirms the concern raised before starting this slice: the
environment axis is not just an infrastructure substrate swap.

---

### 1.3 Lab Environment (CloudCore)

#### 1.3.1 Design

- CloudCore provides the VPC, subnet, security group, and compute
  primitives only. Its own native load balancer resource
  (`cloudcore_load_balancer` / `cloudcore.cloudcore.load_balancer`) is
  deliberately **not** used here — this slice exists to validate the
  designed NGINX + Keepalived architecture itself, not CloudCore's own
  competing feature.
- Topology: 1 VPC, 1 public subnet, 1 security group, 2× NGINX instances,
  2× Frontend instances — all in the same subnet for this slice (no
  separate LB/app subnets yet; that's a network-hardening refinement for a
  later pass, not a correctness requirement for this one).
- Security group: inbound 22 and 80/443 from `admin_cidr` to the NGINX
  instances; inbound 80 from the NGINX instances' security group to the
  frontend instances (frontend is never reachable directly from outside
  the VPC); VRRP inbound (protocol 112, or UDP if using unicast VRRP — see
  below) between the two NGINX instances themselves.

> **Verify before building —** Keepalived's VRRP advertisements need the
> two NGINX nodes on a real shared L2/L3 segment. CloudCore instances
> default to SLIRP networking, where each instance is independently NATed
> by the host and **cannot reach another instance directly** — VRRP would
> silently never work in that mode, the same class of failure as the
> multicast-blocked-in-cloud-VPCs problem the HLD already designed around
> (HLD §7, unicast VRRP). CloudCore's bridged mode (`ccbr0`) is required
> instead, since it gives instances real, mutually-reachable IPs. Confirmed
> present and configured on this host (`192.168.100.1/24`) but currently
> showing `NO-CARRIER`/`DOWN` — normal if nothing is attached to it yet,
> but confirm it comes up (`ip link show ccbr0`) once the two NGINX
> instances are actually running, before assuming VRRP will work. If it
> doesn't, `sudo bash api/setup-network.sh` repairs it.
>
> **Resolved** — four real platform gaps in exactly this area were found
> and fixed while building this slice: missing `/etc/qemu/bridge.conf`,
> missing `CAP_NET_ADMIN` on `qemu-bridge-helper`, Docker's iptables
> `FORWARD` policy silently dropping bridge traffic, and no DNS resolver
> handed out over DHCP. All four are now automated in
> `api/setup-network.sh`. See [Findings Log
> F-003–F-006](haFullStack-Findings-Log.md#f-003--bridged-instances-silently-fall-back-to-slirp-without-etcqemubridgeconf).
> Multicast VRRP (the default) was also empirically confirmed to work
> correctly over `ccbr0` — no unicast peering is needed for Lab after
> all; see §1.3.2 below.

#### 1.3.2 OpenTofu Implementation

Follows the same module pattern as this repo's existing examples
(`examples/ghidra-workstation`, `examples/kiwix-library`):

```hcl
# examples/ha-frontend-lb/main.tf  (illustrative excerpt)

module "vpc" {
  source      = "../../modules/vpc"
  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpcs        = { main = { cidr_block = var.cidr_block } }
}

module "subnets" {
  source         = "../../modules/subnets"
  project        = var.project
  environment    = var.environment
  owner          = var.owner
  vpc_id         = module.vpc.vpc_ids_by_key["main"]
  vpc_cidr_block = var.cidr_block
  subnets        = { web = { newbits = 8, netnum = 1, public = true, zone = "a" } }
}

module "security_groups" {
  source      = "../../modules/security-groups"
  project     = var.project
  environment = var.environment
  owner       = var.owner
  vpc_id      = module.vpc.vpc_ids_by_key["main"]

  security_groups = {
    nginx = {
      description = "NGINX LB tier — public HTTP/S + SSH, scoped to admin_cidr; VRRP between peers"
      ingress_rules = {
        ssh   = { ip_protocol = "tcp", from_port = 22,  to_port = 22,  cidr = var.admin_cidr }
        http  = { ip_protocol = "tcp", from_port = 80,  to_port = 80,  cidr = var.admin_cidr }
        https = { ip_protocol = "tcp", from_port = 443, to_port = 443, cidr = var.admin_cidr }
        vrrp  = { ip_protocol = "112", from_port = 0, to_port = 0, cidr = var.cidr_block }
      }
      egress_rules = { all = { ip_protocol = "-1", cidr = "0.0.0.0/0" } }
    }
    frontend = {
      description = "Frontend — HTTP from the NGINX tier only"
      ingress_rules = {
        # cidr scoped to the VPC block; a security-group-to-security-group
        # rule (source_sg_id) is the tighter option once the nginx SG id
        # is known — see modules/security-groups for the source_sg_id form.
        http = { ip_protocol = "tcp", from_port = 80, to_port = 80, cidr = var.cidr_block }
      }
      egress_rules = { all = { ip_protocol = "-1", cidr = "0.0.0.0/0" } }
    }
  }
}

module "frontend" {
  source             = "../../modules/instance-group"
  project            = var.project
  environment        = var.environment
  owner              = var.owner
  name               = "frontend"
  image_id           = "ubuntu-22.04"
  flavor             = var.frontend_flavor
  count_instances    = 2
  vpc_id             = module.vpc.vpc_ids_by_key["main"]
  subnet_id          = module.subnets.subnet_ids_by_key["web"]
  security_group_ids = [module.security_groups.security_group_ids_by_key["frontend"]]
  user_data          = local.frontend_user_data   # application deployment — out of scope here
}

module "nginx" {
  source             = "../../modules/instance-group"
  project            = var.project
  environment        = var.environment
  owner              = var.owner
  name               = "nginx"
  image_id           = "ubuntu-22.04"
  flavor             = var.nginx_flavor
  count_instances    = 2
  vpc_id             = module.vpc.vpc_ids_by_key["main"]
  subnet_id          = module.subnets.subnet_ids_by_key["web"]
  security_group_ids = [module.security_groups.security_group_ids_by_key["nginx"]]
  user_data          = local.nginx_user_data   # renders per-instance below
}
```

`local.nginx_user_data` is a `templatefile()` cloud-init blob (same pattern
as every existing example template) installing `nginx` and `keepalived`,
writing the `http{}` frontend upstream from HLD §3.1 / `haFullStack.md`
§3.1, and a Keepalived config using **unicast** peering between the two
instances' real (bridged) IPs — never the multicast default, for the same
reason argued in `haFullStack.md` §3.5. Each of the two instances needs a
*different* rendered config (one `state MASTER`, one `state BACKUP`, each
listing the other as `unicast_peer`) — `count.index`-driven templating in
the `instance-group` module call, or two explicit `cloudcore_instance`
resources instead of `instance-group`, whichever this repo's module
supports for per-instance user_data variance (needs a short module-capability
check before this is built for real — flagged as an open item in §1.7).

> **As built** — `examples/ha-frontend-lb/` is now real and has been
> applied, verified (VIP round-robins across both frontends; stopping
> `keepalived` on the MASTER fails over to BACKUP with zero dropped
> requests), and destroyed cleanly. Two differences from the illustrative
> snippet above, both because testing answered questions this section
> left open: Keepalived uses **multicast** VRRP, not unicast — validated
> directly over `ccbr0`, no peer-IP wiring needed (see the resolved
> callout in §1.3.1) — and the NGINX tier uses `modules/compute` (its
> per-key `user_data` support), not two `instance-group` calls, resolving
> the per-instance-variance question below and in §1.7. Getting the
> frontend tier's real IPs into NGINX's `user_data` at all required two
> platform fixes — see [Findings Log
> F-007](haFullStack-Findings-Log.md#f-007--private_ip-permanently-stuck-on-the-slirp-placeholder-for-bridged-instances)
> and
> [F-008](haFullStack-Findings-Log.md#f-008--opentofu-providers-create-didnt-wait-for-private_ip-before-considering-an-instance-ready).
> A YAML-templating mistake in the cloud-init itself is
> [F-009](haFullStack-Findings-Log.md#f-009--templatefilecloud-init-yaml-broke-on-indent-and-inline-runcmd-quoting).

#### 1.3.3 Ansible Implementation

Same provisioning shape as `ansible/examples/09-ghidra-workstation.yml`
(a new `cloudcore.cloudcore.security_group` per tier, `instance` ×2 for
each of NGINX and frontend, `user_data:` carrying the same cloud-init
content as the OpenTofu path above — this repo's Ansible convention
provisions via `user_data`/cloud-init rather than by targeting the new
hosts with further Ansible tasks, consistent with every existing playbook
in `ansible/examples/`). The same per-instance-config problem noted above
applies here too: each NGINX instance's `user_data` needs to differ (its
own MASTER/BACKUP state and its peer's address), so the two `instance`
tasks can't share one identical `user_data` value — they need two separate
tasks (or a `loop` with per-item template rendering), not a shared
variable.

---

### 1.4 On-Prem Environment

#### 1.4.1 Design

Architecturally identical to Lab — real NGINX + Keepalived, unicast VRRP —
but the infrastructure substrate is real hardware or an on-prem
hypervisor (VMware/KVM) rather than CloudCore, and provisioning tooling
differs accordingly. Two real differences from Lab worth calling out
explicitly:

- **VRRP has no SLIRP-equivalent problem here** — on-prem hosts are
  normally on real, routed network segments already, so the "verify
  bridged networking" concern from §1.3.1 doesn't apply. What replaces it:
  confirm the specific VLAN/segment the two NGINX hosts sit on actually
  permits VRRP's protocol 112 (or the unicast UDP alternative) between
  them — some hardened on-prem network policies block IP protocol 112
  by default the same way cloud VPCs block multicast.
- **A hardware/dedicated load balancer may already exist** in a given
  on-prem estate (F5, Citrix ADC, a physical HAProxy pair). If so, this
  entire NGINX+Keepalived design may be unnecessary duplication — that's
  a real decision to make per-deployment, not something this LLD can
  resolve generically. Documented here as an explicit decision point
  rather than silently assumed either way.

#### 1.4.2 OpenTofu Implementation

Unlike Lab, there's no CloudCore-style unified API — on-prem OpenTofu
typically targets the hypervisor directly. For a KVM-based on-prem estate,
the community `dmacvicar/libvirt` provider is the direct equivalent of
what CloudCore's own provider does internally; for VMware, the official
`vsphere` provider. Structurally the same shape as the Lab module call
(VPC/network equivalent, security policy, 2×NGINX, 2×frontend), just
against a different provider — the cloud-init `user_data` content
(NGINX + Keepalived config) carries over unchanged from §1.3.2, since that
part is guest-OS-level, not infrastructure-provider-level.

```hcl
# Illustrative shape only — provider block depends on the actual
# on-prem hypervisor in use, confirm before implementation.
provider "vsphere" {
  # vsphere_server / user / password — from a secrets store, never inline
}

resource "vsphere_virtual_machine" "nginx" {
  count            = 2
  name             = "nginx-${count.index + 1}"
  resource_pool_id = data.vsphere_resource_pool.pool.id
  datastore_id     = data.vsphere_datastore.ds.id
  num_cpus         = 2
  memory           = 2048
  guest_id         = "ubuntu64Guest"

  network_interface {
    network_id = data.vsphere_network.web.id
  }

  extra_config = {
    "guestinfo.userdata"          = base64encode(local.nginx_user_data[count.index])
    "guestinfo.userdata.encoding" = "base64"
  }
}
```

#### 1.4.3 Ansible Implementation

This is where the on-prem path genuinely diverges from Lab's convention,
rather than just swapping a provider: without CloudCore's API-driven
provisioning model, the natural on-prem Ansible pattern is classic
configuration management against an inventory of already-provisioned (or
separately-provisioned) hosts, not a `hosts: localhost` API-calling play.

```yaml
# Illustrative shape
- name: Configure NGINX + Keepalived — on-prem
  hosts: nginx_lb
  become: true
  tasks:
    - name: Install NGINX and Keepalived
      ansible.builtin.apt:
        name: [nginx, keepalived]
        state: present
        update_cache: true

    - name: Template NGINX config
      ansible.builtin.template:
        src: nginx.conf.j2
        dest: /etc/nginx/nginx.conf
      notify: Reload NGINX

    - name: Template Keepalived config (unicast, per-node state)
      ansible.builtin.template:
        src: keepalived.conf.j2
        dest: /etc/keepalived/keepalived.conf
      vars:
        vrrp_state: "{{ 'MASTER' if inventory_hostname == groups['nginx_lb'][0] else 'BACKUP' }}"
        vrrp_priority: "{{ 150 if vrrp_state == 'MASTER' else 100 }}"
      notify: Restart Keepalived

  handlers:
    - name: Reload NGINX
      ansible.builtin.service: { name: nginx, state: reloaded }
    - name: Restart Keepalived
      ansible.builtin.service: { name: keepalived, state: restarted }
```

The `vrrp_state`/`vrrp_priority` Jinja expression is the clean way to
derive MASTER/BACKUP from inventory position — this is also arguably a
*better* pattern than the Lab/cloud-init approach for the per-instance
config-variance problem flagged in §1.3.2/§1.3.3, since Ansible's
`inventory_hostname`/`groups` give a natural way to distinguish the two
nodes that cloud-init templating has to work harder to express.

---

### 1.5 AWS Environment

#### 1.5.1 Design

No Keepalived, no Keepalived VIP — an Application Load Balancer is itself
the highly-available component, already spanning multiple Availability
Zones with AWS managing its own internal redundancy. The frontend tier
becomes an ALB target group; "the VIP" from the HLD's vocabulary is
replaced conceptually by the ALB's own DNS name (and, if a static entry
point is required, a Route53 alias record pointing at it — ALBs don't
take a static IP the way an EC2 instance does).

- ALB (internet-facing, 2+ AZs)
- Target group (frontend instances, HTTP:80, health check path TBD in a
  later slice alongside the application itself)
- 2× frontend EC2 instances (or an Auto Scaling Group — ASG is the more
  idiomatic AWS-native choice for "2 instances," and gets self-healing for
  free; worth confirming as the actual target rather than bare
  `aws_instance` resources before implementation)
- Security groups: ALB SG allows 80/443 from the internet; frontend SG
  allows 80 from the ALB SG only (SG-to-SG reference, not a CIDR — this is
  the one place AWS's security group model is strictly better than a
  CIDR-based rule for expressing "only the load balancer can reach this")

#### 1.5.2 OpenTofu Implementation

```hcl
# Illustrative shape — official hashicorp/aws provider, separate from
# CloudCore's own provider entirely.

resource "aws_lb" "frontend" {
  name               = "${var.project}-${var.environment}-frontend"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.public.ids
}

resource "aws_lb_target_group" "frontend" {
  name     = "${var.project}-${var.environment}-frontend-tg"
  port     = 80
  protocol = "HTTP"
  vpc_id   = data.aws_vpc.main.id

  health_check {
    path                = "/"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 15
  }
}

resource "aws_lb_listener" "frontend_http" {
  load_balancer_arn = aws_lb.frontend.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}

resource "aws_autoscaling_group" "frontend" {
  name                = "${var.project}-${var.environment}-frontend"
  min_size            = 2
  max_size            = 2
  desired_capacity    = 2
  vpc_zone_identifier = data.aws_subnets.public.ids
  target_group_arns   = [aws_lb_target_group.frontend.arn]

  launch_template {
    id      = aws_launch_template.frontend.id
    version = "$Latest"
  }
}
```

#### 1.5.3 Ansible Implementation

```yaml
# Illustrative shape — amazon.aws collection.
- name: Provision AWS ALB + frontend tier
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Create target group
      amazon.aws.elb_target_group:
        name: "{{ project }}-{{ env }}-frontend-tg"
        protocol: http
        port: 80
        vpc_id: "{{ vpc_id }}"
        health_check_path: /
        target_type: instance
      register: tg

    - name: Create ALB
      amazon.aws.elb_application_lb:
        name: "{{ project }}-{{ env }}-frontend"
        subnets: "{{ public_subnet_ids }}"
        security_groups: ["{{ alb_sg_id }}"]
        listeners:
          - Protocol: HTTP
            Port: 80
            DefaultActions:
              - Type: forward
                TargetGroupArn: "{{ tg.target_group_arn }}"
        state: present
```

Note the tool split here is less clean than Lab/On-Prem: `amazon.aws` is a
provisioning-only collection (equivalent to Terraform resources, not
configuration management) — there's no Ansible-native "install this on
the EC2 instance after boot" step shown because AWS instances would
typically use EC2 user-data (cloud-init) for that, the same mechanism as
Lab, not a further Ansible play against the new hosts.

---

### 1.6 Cross-Environment Consistency

What's genuinely the same across all three environments, and what isn't —
worth stating explicitly so later slices don't assume more portability
than actually exists:

| Aspect | Lab | On-Prem | AWS |
|---|---|---|---|
| Frontend instance count | 2 | 2 | 2 (ASG desired_capacity) |
| LB→frontend protocol | HTTP :80 | HTTP :80 | HTTP :80 |
| LB redundancy mechanism | Keepalived/VRRP | Keepalived/VRRP | Managed (ALB, multi-AZ) |
| "VIP" concept | Real floating IP | Real floating IP | Replaced by ALB DNS name |
| Security boundary expression | CIDR (SG source_sg_id available) | Firewall/VLAN policy | SG-to-SG reference |
| Health checking | NGINX passive (`max_fails`) | NGINX passive (`max_fails`) | ALB active health checks (native) |

The last row is a genuine capability difference worth remembering for a
later slice: AWS gets real active health checking for free, which
`haFullStack.md` §3.3 noted OSS NGINX doesn't have without NGINX Plus or a
third-party module. That's not a portability gap to "fix" — it's simply a
better capability AWS happens to provide natively here.

### 1.7 Open Items Before Implementation

- ~~**Per-instance user_data variance (Lab/On-Prem OpenTofu)**~~ —
  **Resolved for Lab.** `modules/compute` takes per-key `user_data`
  natively (a map, not a count), so the NGINX tier uses one call to it
  rather than two `instance-group` calls or two explicit
  `cloudcore_instance` resources. See the "As built" note in §1.3.2. Still
  open for **On-Prem OpenTofu** specifically, since that path doesn't use
  CloudCore's own modules — confirm the equivalent pattern (e.g.
  per-`count.index` `templatefile()`) against whatever provider §1.4.2
  ends up using.
- ~~**Bridged networking verification (Lab)**~~ — **Resolved.** Not only
  does `ccbr0` come up, four real gaps in the bridged-networking path were
  found and fixed doing so — see [Findings Log
  F-003–F-006](haFullStack-Findings-Log.md#f-003--bridged-instances-silently-fall-back-to-slirp-without-etcqemubridgeconf),
  now automated in `api/setup-network.sh`.
- **On-prem hypervisor choice** — §1.4.2 illustrates vSphere; confirm
  against whatever the actual on-prem estate runs (could equally be a
  libvirt-based host, closer to Lab's own OpenTofu shape).
- **ASG vs. plain instances (AWS)** — confirm ASG is the intended target
  before building it; changes the Ansible/OpenTofu shape meaningfully
  either way.

---

## 2. Database Tier — MySQL High Availability

### 2.1 Scope

**In scope for this section:** the 3-node MySQL Group Replication cluster,
ProxySQL's read/write splitting in front of it, and exposing that through
the **same** NGINX nodes built in §1 (via a `stream{}` block, sibling to
the `http{}` block already serving the frontend LB — `haFullStack.md`
§3.2) — not a new/separate load balancer. This is one growing stack, not
independent per-slice templates (`examples/ha-frontend-lb/` gets extended
in place, not forked).

**Deliberate scoping decision, not a permanent architecture call:** the
HLD's own request flow (`haFullStack-HLD.md` §3.1/§3.2) has the *backend*
tier as MySQL's actual client, not the frontend directly — but the
backend tier is a later slice, not built yet. For this slice, the
frontend instances connect to ProxySQL directly and prove the cluster is
real and operational (cluster membership, current primary, a live query)
purely as a Lab validation convenience, standing in for the not-yet-built
backend. This should not be read as "frontend talks to the database" in
the target architecture — flagged explicitly so it isn't misread later.

**Explicitly out of scope, for later slices:** RabbitMQ, Keystone, and
the real backend application tier itself (HLD §2 groups frontend/backend/
Keystone as "the HTTP tier," but per this session's plan each becomes its
own slice).

### 2.2 Environment and Tooling Matrix

| Environment | HA Mechanism | Why | IaC Tool(s) |
|---|---|---|---|
| **Lab** (this platform) | 3× MySQL Group Replication + 2× ProxySQL, exposed via the existing NGINX `stream{}` | Validates the actual HLD-designed architecture (§5 of `haFullStack.md`) against real infrastructure | OpenTofu or Ansible |
| **On-Prem** | Same — 3× MySQL GR + 2× ProxySQL + NGINX `stream{}` | Same architecture as Lab — this is the environment the HLD design was actually written for | OpenTofu or Ansible |
| **AWS** | RDS for MySQL, Multi-AZ (or Aurora MySQL) + RDS Proxy | AWS's managed Multi-AZ failover replaces hand-rolled Group Replication the same way ALB replaced Keepalived in §1 — RDS Proxy is the managed equivalent of ProxySQL (connection pooling, masks failover from clients) | OpenTofu or Ansible |

Same pattern as §1.2: Lab and On-Prem are architecturally identical;
AWS is a genuinely different, managed mechanism.

---

### 2.3 Lab Environment (CloudCore)

#### 2.3.1 Design

- Reuses the existing VPC/subnet from §1 — this is one growing stack, not
  a new one.
- Two new security groups: `mysql` (ingress 3306 + 33061 from the
  `proxysql` SG and from other `mysql`-tagged instances for Group
  Replication's own peer traffic; egress all) and `proxysql` (ingress
  6033 from the `nginx` SG only; egress all).
- 3× MySQL instances via `modules/compute` (map-based, per-key
  `user_data`) — same pattern as the two NGINX nodes in §1, because each
  node needs a genuinely different config: distinct `server-id`,
  distinct `group_replication_local_address`, and only node 1 sets
  `group_replication_bootstrap_group = ON` before starting. Also sets
  `group_replication_autorejoin_tries` (not present in `haFullStack.md`
  §5.1's config block — added here specifically for §2.3.1b's
  self-healing boundary, so a transiently-expelled node reconnects on its
  own rather than needing the same manual `START GROUP_REPLICATION` a
  genuine restart does). Also installs `quorum-watchdog.py` (a systemd
  timer, every 3s) — the real fix for F-021, enforcing `super_read_only`
  locally on a node that's `ONLINE`/`PRIMARY` of a group below the
  original cluster's majority.
- 2× ProxySQL instances via `modules/instance-group` — identical config
  is fine here (both just need the 3 MySQL nodes' addresses, known at
  apply time the same way frontend's IPs were known to NGINX in §1).
- NGINX's `user_data` template (from §1) gains a `stream{}` block
  listening on `:3306`, proxying to the 2 ProxySQL nodes' `:6033` — this
  means `module.nginx`'s `user_data` now depends on **both**
  `module.frontend` (existing) and the new `module.proxysql`'s outputs.
- Frontend's `user_data` (from §1) gains a systemd timer that queries
  ProxySQL every few seconds and writes the live result into the page
  NGINX already serves — the "proof it's real" requirement — needing only
  a `mysql-client` package, not a full app server. Two things, not one:
  - `performance_schema.replication_group_members` (node list, role,
    `MEMBER_STATE`) and `@@hostname` — proves the query actually routed
    through NGINX → ProxySQL → MySQL, not a hardcoded value.
  - A **write** against a small heartbeat table
    (`INSERT ... ON DUPLICATE KEY UPDATE counter = counter + 1,
    updated_at = NOW()`), then read back and displayed alongside the
    membership info. Metadata alone only proves the cluster's *state* is
    healthy — it says nothing about whether the thing failover actually
    protects (the ability to write) still works. The heartbeat's
    `counter`/`updated_at` also makes staleness itself visible: if the
    timer stops successfully writing, the page stops advancing, which is
    its own failure signal.

> **Open risk, to verify empirically before treating this as final** (see
> §2.7) — Group Replication's bootstrap sequencing. `cloud-init` on all 3
> MySQL nodes runs concurrently (same `for_each`-driven creation as
> NGINX's MASTER/BACKUP pair in §1), but node 1 must finish
> **bootstrapping** the group before nodes 2/3 can successfully **join**
> it. Nodes 2/3's `runcmd` needs to poll node 1's MySQL port (and,
> better, its actual group-membership state) and retry the join rather
> than assume node 1 is ready — this is exactly the kind of
> platform-timing assumption that broke twice already in §1 (`private_ip`
> timing, F-007/F-008) and needs the same "verify for real, don't assume"
> treatment before it's trusted.

#### 2.3.1a Failure-Mode Test Matrix

The actual point of a 3-node cluster is that losing one node doesn't
cause total loss (§5.3/F-001) — that has to be demonstrated, not assumed
from the topology being "correct." Five tests, all against the heartbeat
write path above, not just cluster metadata:

| # | Test | What it proves | Expected result |
|---|---|---|---|
| 1 | Stop a **secondary** node | Losing a non-primary is a non-event | Zero write impact; dead node drops out of the read hostgroup; frontend page keeps advancing without interruption |
| 2 | Stop the **primary** node | Automatic failover actually works, with a real (not assumed) recovery time | Group Replication elects a new primary among the 2 survivors; ProxySQL re-tags the write hostgroup to it; a write-loop run through the failover window gives a measured RTO from actual consecutive-failure count, not the ~5-10s from `haFullStack.md` §5.3 taken on faith |
| 3 | Stop **2 of 3** nodes simultaneously | Whether the quorum protection is actually real, not just a bigger node count | **Empirically found not to hold by default** (F-021) — Group Replication expels the unreachable peers and the survivor continues as a legitimate, smaller group, majority-of-1 trivially satisfied. It does not drop out of `ONLINE` and does not refuse writes. This is still the right test to run — it's what *disproved* an assumption the whole design was resting on, which is exactly what a negative test is for |
| 4 | Restart a stopped node (full `mysqld`/VM restart) | Rejoin behaviour after a real restart is understood, not assumed | Confirm it does **not** auto-rejoin (`group_replication_start_on_boot = OFF` by design) and needs an explicit `START GROUP_REPLICATION`; confirm it then catches up via distributed recovery |
| 5 | Transient network blip to one node (e.g. a brief `iptables DROP` on its GR port, `mysqld` never stops) | Self-healing actually works for the case it's supposed to — a node expelled from the group without ever crashing | Confirm the node is expelled, then confirm it **automatically rejoins** once connectivity returns, with no manual step — this is what `group_replication_autorejoin_tries` (§2.3.1b) is specifically for, and is the one failure class that should require zero intervention |

Test 3 is the one most likely to be skipped in favour of "2 down would
obviously be worse" — it shouldn't be, and this is exactly why: run for
real, it overturned an assumption (§5.1/§5.3) the whole 3-node design
had been resting on. **§5.1's fault-tolerance argument for 3 nodes over
2 still holds** — an actual node death genuinely needs 2 survivors to
keep working at all — but the specific *split-brain protection* §5.3
claimed for a network-partition scenario does not exist by default; see
F-021 and `haFullStack.md` §5.3 (corrected to v1.3) for the real
behavior and what a genuine fix would need. Test 5 is the flip side —
the evidence that *not everything* requires a human, so §2.3.1b's
manual-only boundary is earned rather than lazy.

#### 2.3.1b Self-Healing Boundary and Manual-Intervention Diagnostics

Not every recoverable event should need a human, but not every event is
safe to automate either — MySQL's own documentation treats forcing group
membership on a survivor as a deliberate operator decision, not something
to script blindly, since getting it wrong risks accepting a node's state
without knowing whether it actually still agrees with the rest of the
group. The line drawn here:

| Event | Self-heals? | Mechanism |
|---|---|---|
| Secondary node lost | Yes | Group Replication membership change + ProxySQL monitor, both automatic |
| Primary node lost | Yes | Group Replication auto-election + ProxySQL re-tag, both automatic |
| Node transiently expelled, `mysqld` still running | Yes | `group_replication_autorejoin_tries` — confirmed empirically (test 5): expelled and auto-rejoined in ~4s, zero manual intervention |
| `mysqld`/VM actually restarts | **No, deliberately** | `group_replication_start_on_boot = OFF` — a restarted node's data/state shouldn't be trusted back into the group without a human confirming it, matching MySQL's own recommended default |
| 2 of 3 nodes lost (below quorum) | **Yes, but not by Group Replication itself** | **Revised after F-021.** The original plan here (`group_replication_force_members` as a considered operator action) assumed GR would actually *block* the survivor pending that decision — empirically, it doesn't: GR expels unreachable peers and the survivor keeps serving as a legitimate smaller group with no operator input needed or possible. The real fix is `quorum-watchdog.py` (§2.3.2), running locally on every node, enforcing `super_read_only` when the local node is primary of a group below the *original* majority, and clearing it again once membership returns — fully automatic, no `force_members`, no human in the loop |

Only one row is still deliberately manual. Group Replication's own
design intent for the below-quorum case — a human decides whether to
force-reform the group — turned out not to reflect what GR actually
does by default, so that automation gap is now closed differently than
originally planned (§2.7, F-021), not left as a manual case. For the
remaining manual row, the requirement isn't to automate it away — it's
that the moment it's needed, the diagnosis has to be immediate and the
remediation has to be concrete, not "go check the MySQL error log." The
frontend status page (§2.3.1) already queries
`performance_schema.replication_group_members` every cycle — it gets
extended to interpret that, not just display it:

- A clear **OK / DEGRADED / CRITICAL** verdict, not just a raw member
  list — `DEGRADED` for "1 node down, already self-healed or healing,"
  `CRITICAL` for "below quorum." As of the `quorum-watchdog` fix (F-021),
  `CRITICAL` is no longer "needs a human" by default — the survivor
  enforces its own read-only state automatically — but the verdict still
  matters as visible confirmation that the automatic enforcement is
  actually engaged, and as the trigger for a human to look if it somehow
  isn't (the watchdog service failing, the survivor never reaching
  `super_read_only=ON` within the expected window, etc.).
- In `CRITICAL` state: which specific node(s) are missing, the last
  known-good member list, and — as a manual override, not the expected
  path — the **actual** `group_replication_force_members` value built
  from the survivor's own view of the group (it already has every
  member's UUID from the same query), not a generic placeholder command
  someone has to fill in correctly under pressure.
- The same status gets written as structured JSON to a local file on
  each frontend instance (not only rendered as HTML) — so a real
  incident has a machine-readable, timestamped record beyond whatever the
  webpage happened to show at the moment someone looked, and so later
  slices (monitoring/alerting, explicitly out of scope for §1.1) have
  something to hook into rather than needing to scrape HTML.

#### 2.3.2 OpenTofu Implementation

Illustrative — to be proven and corrected against real infrastructure the
same way §1.3.2 was (see its "As built" note for how much a real build
can diverge from a first sketch):

```hcl
# examples/ha-frontend-lb/main.tf  (additions — illustrative)

module "security_groups" {
  # ...existing nginx/frontend groups from §1, plus:
  security_groups = {
    # ...
    mysql = {
      description = "MySQL Group Replication tier"
      ingress_rules = {
        mysql = { ip_protocol = "tcp", from_port = 3306, to_port = 3306, cidr = local.bridge_cidr }
        gr    = { ip_protocol = "tcp", from_port = 33061, to_port = 33061, cidr = local.bridge_cidr }
      }
      egress_rules = { all = { ip_protocol = "-1", cidr = "0.0.0.0/0" } }
    }
    proxysql = {
      description = "ProxySQL — MySQL nodes' subnet only"
      ingress_rules = {
        admin = { ip_protocol = "tcp", from_port = 6033, to_port = 6033, cidr = local.bridge_cidr }
      }
      egress_rules = { all = { ip_protocol = "-1", cidr = "0.0.0.0/0" } }
    }
  }
}

locals {
  mysql_nodes = {
    a = { server_id = 1, bootstrap = true }
    b = { server_id = 2, bootstrap = false }
    c = { server_id = 3, bootstrap = false }
  }
  mysql_instances = {
    for role, cfg in local.mysql_nodes : "mysql-${role}" => {
      image_id  = "ubuntu-22.04"
      flavor    = var.mysql_flavor
      vpc_id    = module.vpc.vpc_ids_by_key[local.vpc_key]
      subnet_id = module.subnets.subnet_ids_by_key["main${local.sfx}"]
      security_group_ids = [module.security_groups.security_group_ids_by_key["mysql${local.sfx}"]]
      user_data = templatefile("${path.module}/files/mysql-cloud-init.yaml.tftpl", {
        server_id            = cfg.server_id
        bootstrap             = cfg.bootstrap
        group_seeds           = join(",", [for ip in values(local.mysql_seed_ips) : "${ip}:33061"])
      })
    }
  }
}

module "mysql" {
  source    = "../../modules/compute"
  project   = var.project
  environment = var.environment
  owner     = var.owner
  instances = local.mysql_instances
}

module "proxysql" {
  source              = "../../modules/instance-group"
  project             = var.project
  environment         = var.environment
  owner               = var.owner
  name                = "proxysql${local.sfx}"
  image_id            = "ubuntu-22.04"
  flavor              = var.proxysql_flavor
  count_instances     = 2
  vpc_id              = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id           = module.subnets.subnet_ids_by_key["main${local.sfx}"]
  security_group_ids  = [module.security_groups.security_group_ids_by_key["proxysql${local.sfx}"]]
  user_data           = local.proxysql_user_data   # references module.mysql.private_ips_by_key
}
```

`local.mysql_seed_ips` needs `module.mysql`'s own `private_ips_by_key`
output — a self-reference within the same module call, which Terraform
doesn't allow. Real implementation likely needs a fixed/predictable
addressing scheme decided before apply (e.g. static private IPs per
node, if this platform supports assigning them) or a two-stage apply —
flagged as an open item (§2.7), not resolved here.

#### 2.3.3 Ansible Implementation

Same provisioning shape as §1.3.3 — `security_group` ×2 new groups,
`instance` ×3 for MySQL (three separate tasks or a per-item loop, since
each needs different `server-id`/bootstrap `user_data`, same reasoning as
the NGINX MASTER/BACKUP pair), `instance` ×2 for ProxySQL (identical
`user_data`, a simple loop is fine), and the existing NGINX/frontend
`instance` tasks from §1.3.3 gain updated `user_data` for the `stream{}`
block and the proof-of-concept timer respectively.

---

### 2.4 On-Prem Environment

Architecturally identical to Lab (real MySQL Group Replication, real
ProxySQL) — same two real differences from Lab as §1.4 called out for
Keepalived: no SLIRP-networking concern (real hosts, real network
already), and the possibility that a managed/existing database platform
already exists in a given on-prem estate, in which case this whole
self-managed MySQL design may be unnecessary duplication — a real
per-deployment decision, not something this LLD can resolve generically.

### 2.5 AWS Environment

RDS for MySQL (Multi-AZ) or Aurora MySQL, fronted by RDS Proxy —
replaces Group Replication, ProxySQL, and the NGINX `stream{}` block
entirely with managed equivalents, the same substitution pattern as ALB
replacing Keepalived in §1.5. No Group Replication bootstrap-sequencing
risk (§2.3.1's open risk) — AWS manages primary election internally.
Illustrative shape only, not built this slice:

```hcl
# Illustrative shape — official hashicorp/aws provider.
resource "aws_db_instance" "mysql" {
  identifier             = "${var.project}-${var.environment}-mysql"
  engine                 = "mysql"
  multi_az               = true
  instance_class         = "db.t3.medium"
  allocated_storage      = 50
  db_subnet_group_name   = aws_db_subnet_group.mysql.name
  vpc_security_group_ids = [aws_security_group.mysql.id]
  manage_master_user_password = true   # Secrets Manager, never a plain variable
}

resource "aws_db_proxy" "mysql" {
  name                   = "${var.project}-${var.environment}-mysql-proxy"
  engine_family          = "MYSQL"
  vpc_subnet_ids         = data.aws_subnets.private.ids
  role_arn               = aws_iam_role.rds_proxy.arn
  auth {
    auth_scheme = "SECRETS"
    secret_arn  = aws_db_instance.mysql.master_user_secret[0].secret_arn
  }
}
```

### 2.6 Cross-Environment Consistency

| Aspect | Lab | On-Prem | AWS |
|---|---|---|---|
| MySQL node count | 3 (Group Replication) | 3 (Group Replication) | 1 logical (Multi-AZ standby is not independently queryable) |
| Read/write splitting | ProxySQL | ProxySQL | RDS Proxy (or app-level, if not using Proxy) |
| Failover mechanism | Group Replication auto-election + ProxySQL re-tag | Same | AWS-managed, transparent via RDS Proxy |
| Exposure to clients | NGINX `stream{}` (same nodes as HTTP LB) | Same | RDS Proxy endpoint directly (no NGINX stream needed) |
| Failure tolerance | 1 of 3 (majority-quorum, see F-001) | 1 of 3 | AWS SLA-backed, no quorum concept exposed |

### 2.7 Open Items Before Implementation

- ~~**Group Replication bootstrap sequencing (Lab/On-Prem)**~~ —
  **Resolved.** Confirmed via a clean, fully unattended `tofu apply`
  (zero manual SSH intervention): the wait/retry loop in the joiner
  nodes' own `runcmd` correctly handles node 1 not yet being ready.
- ~~**MySQL seed-address self-reference (Lab OpenTofu)**~~ —
  **Resolved.** Split into two `modules/compute` calls (bootstrap node,
  then joiners referencing its now-known IP), the same pattern that
  made frontend→NGINX work in §1.
- ~~**ProxySQL monitor credentials**~~ — **Resolved.** A cloud-init-created
  `proxysql_monitor` account, Lab-appropriate credential (see F-019 for
  why it needs `mysql_native_password`, not the 8.0 default).
- **On-prem hypervisor choice** — still open from §1.7, applies here too.
- ~~**Status-script complexity (§2.3.1b)**~~ — **Resolved.** Built as a
  Python script as anticipated, now also maintaining a rolling history
  (not just current-snapshot) so a brief transition during a failure test
  is visible even if no one is watching the page at the exact moment it
  happens — added while running the failure-mode tests, not part of the
  original 2A-07 scope, but a natural extension of it.
- **Bootstrap node has no permanent seed for its own rejoin (Lab/On-Prem)**
  — found during failure-mode testing (F-020): node "a" (the original
  bootstrap node) has `group_replication_group_seeds = ""` for its entire
  lifetime, not just its first boot — meaning after any restart, it can
  only *bootstrap a new group*, never *rejoin the existing one*, unlike
  joiner nodes (which always carry a real, permanent seed: node "a"'s own
  IP). Fixed manually per-incident (`SET GLOBAL
  group_replication_group_seeds = ...` before rejoining) but not
  templated — a real fix needs either a remotely-accessible,
  sufficiently-privileged account so joiners can update node "a"'s seed
  list as they join, or an equivalent mechanism, with its own security
  tradeoff worth a deliberate decision, not a silent addition.
- ~~**No real split-brain protection for a network-partition scenario**~~
  — **Resolved (F-021).** `quorum-watchdog.py` runs locally on every
  MySQL node, forcing `super_read_only` on a local primary operating
  below the original cluster's majority, clearing it again once
  membership is restored — no `force_members`, no human in the loop, no
  new remotely-accessible privileged account. Verified with the full
  below-quorum cycle run twice, including a real bug found and fixed
  building the fix itself (`super_read_only=OFF` doesn't clear the
  separate `read_only` flag, which silently kept ProxySQL from ever
  re-admitting a recovered node as a writer). Carries over unchanged to
  On-Prem (same MySQL nodes, same local script); AWS replaces this
  concern entirely with RDS Multi-AZ's own managed failover (§2.5).

---

## 3. Identity Tier — Keystone

### 3.1 Scope

**In scope for this section:** 2× Keystone instances (active-active, per
`haFullStack.md` §7), a 2-node memcached pool for Fernet token caching,
exposing Keystone through the existing NGINX nodes, and a
`keystone-status.html` page on the frontend tier proving it works —
same pattern as §2's `mysql-status.html`.

**Deliberately not a separate database tier:** Keystone shares the
*existing* 3-node MySQL cluster from §2 (a new `keystone` database and
user on it), not a new one — matching `haFullStack.md` §7's "both
instances share the MySQL backend described in Section 5" and this
session's "one growing stack" plan. This also means Keystone's own
health check inherently proves MySQL connectivity too (token issuance
requires a live DB round-trip), which is most of what "tested in
combination with the full stack" means in practice — no separate
combined mega-test is needed on top of it.

**A claim in `haFullStack.md` §7, verified and corrected (F-027):** it
said memcached "is what allows either Keystone instance to validate a
token issued by the other." Failure-mode test 2 (§3.3.1a) settled this
directly rather than assuming it either way — the same discipline that
caught F-021: a token issued by one Keystone node, with that node then
stopped entirely, validated successfully directly against the other node
with zero memcached involvement. The real enabler is **shared Fernet
key material**; memcached's actual role is caching validation results
and propagating revocation state, confirmed by stopping one and then
both memcached nodes separately and finding basic issuance/validation
kept working throughout, just slower. `haFullStack.md` §7 corrected to
v1.5 to match.

**Explicitly out of scope, for later slices:** the backend application
tier itself (§1.1 already deferred this), RabbitMQ, TLS/mTLS between
services (`haFullStack.md` §12/Phase 4's own scope, not this slice's).

### 3.2 Environment and Tooling Matrix

| Environment | Mechanism | Why | IaC Tool(s) |
|---|---|---|---|
| **Lab** (this platform) | 2× Keystone + 2× memcached, active-active, fronted by the existing NGINX nodes | Validates the actual HLD-designed architecture (active-active identity, shared token validation) against real infrastructure | OpenTofu or Ansible |
| **On-Prem** | Same — 2× Keystone + 2× memcached + NGINX | Same architecture as Lab — this is the environment the HLD design was actually written for | OpenTofu or Ansible |
| **AWS** | **Open item, not resolved here** — Keystone is OpenStack software, not an AWS-native service; no managed substitute exists the way RDS substituted for hand-rolled MySQL HA in §2 | Likely still Keystone-on-EC2 (same as Lab/On-Prem) rather than a managed replacement, but confirm before building — see §3.7 | OpenTofu or Ansible |

Unlike §1 (AWS replaced Keepalived with ALB) and §2 (AWS replaced Group
Replication with RDS), there's no obvious AWS-native drop-in for
Keystone specifically — IAM/Cognito solve a related but not equivalent
problem (they're not OpenStack Identity API v3 compatible, and nothing
in this stack currently expects them to be). Flagged as a real open
question for the AWS build of this slice, not glossed over.

---

### 3.3 Lab Environment (CloudCore)

#### 3.3.1 Design

- Reuses the existing VPC/subnet and the existing 3-node MySQL cluster
  from §2 — one growing stack, not a new one. A new `keystone` MySQL
  database and a new `keystone` DB user (own credentials, not shared
  with `appuser`) are created on it, same install-time pattern as
  `clusterdemo`/`appuser` in §2.3.2.
- Two new security groups: `keystone` (ingress 5000 from the `nginx` SG
  only, plus SSH) and `memcached` (ingress 11211 from the `keystone` SG
  only, plus SSH) — memcached is never exposed outside the Keystone
  tier itself.
- 2× Keystone instances via `modules/instance-group` — **not**
  `modules/compute` this time: unlike MySQL's bootstrap/joiner asymmetry
  or NGINX's MASTER/BACKUP split, both Keystone nodes run genuinely
  identical config (active-active, no per-node role), so the simpler,
  homogeneous module is the right fit here, not the map-based one.
- 2× memcached instances via `modules/instance-group` — same reasoning,
  identical config on both.
- **Fernet key generation — the real fix for the memcached claim above.**
  Both Keystone nodes need the *same* Fernet key material from first
  boot, or token validation silently breaks across the pair (a client
  whose request happens to round-robin to "the other" node gets a bogus
  "token invalid," not an obvious error). Generated once in Terraform
  using the `random` provider (`random_id`, 32 bytes, base64url output —
  exactly Fernet's expected key format) and injected identically into
  both nodes' `user_data` — no runtime cross-node coordination needed at
  all, unlike MySQL's donor-based bootstrap. This is a **new provider
  dependency** (`hashicorp/random`), added to `versions.tf`. Static keys
  generated once and never rotated is a known Lab-only simplification —
  a real deployment needs `keystone-manage fernet_rotate` on a schedule
  with the rotated keys distributed to every node, out of scope here.
- Admin bootstrap: `keystone-manage bootstrap` with
  `--bootstrap-password admin` and the default `admin` username — **a
  deliberate, explicit Lab-only credential** (`admin:admin`), same
  "lab-only placeholder, not production" convention as `vrrp_auth_pass`
  and the MySQL passwords in §2.3.2. Only needs to run on **one** of the
  two Keystone nodes (it's a DB write against the shared `keystone`
  database, not a per-node operation) — the other node just needs the
  schema and Fernet keys already in place to serve requests against the
  same data, mirroring the "one node does the one-time setup" pattern
  from §2's MySQL bootstrap node, but far simpler here since there's no
  replication protocol to bootstrap, just a shared DB write.
- NGINX gets Keystone added to its existing `http{}` context (sibling to
  the frontend `server{}` block from §1, both already present in the
  same `sites-available/default`) — `haFullStack.md` §7/§8 specify
  hostname-based vhost routing (`identity.example.com`). **Routes via a
  dedicated listen port (5000, matching Keystone's own native port)
  instead, per direct confirmation: this matches how the real backend
  application actually addresses Keystone once it exists, not a Lab-only
  workaround for missing DNS** — worth being precise about, since
  guest-network DNS resolution of CloudCore-managed hostnames is now
  fixed (F-022: CloudCore's own DNS server, confirmed working since
  F-010–F-014, was host-loopback-only and unreachable from guests until
  the bridge dnsmasq was made to forward guest queries to it). Vhost
  routing would work fine here today; it's still not what this decision
  is based on. More detail on the real application's addressing scheme is
  expected once the backend tier itself is built — this may need
  revisiting then, not assumed settled from this slice alone.
- Frontend gets a new `keystone-status.py` timer (same systemd-timer
  pattern as `mysql-status.py`, same rolling-history JSON + HTML
  approach) — connects through the VIP on :5000, requests a token with
  the `admin:admin` credential, and separately re-validates that same
  token through **the other** Keystone node specifically (not just
  "whichever NGINX round-robins to" — the check needs to deliberately
  target both nodes across two requests to prove cross-node validation,
  not accidentally only ever hit one). Verdict logic: `OK` if token
  issuance succeeds and cross-node validation succeeds; `DEGRADED` if
  issuance succeeds but cross-node validation fails (exactly the
  scenario the Fernet-key-sharing question above would produce);
  `CRITICAL` if issuance itself fails.

#### 3.3.1a Failure-Mode Test Matrix

| # | Test | What it proves | Expected result | Actual result |
|---|---|---|---|---|
| 1 | Stop one Keystone node | Active-active genuinely means zero failover delay, not just "a fast failover" | Zero impact — the surviving node keeps answering immediately, no election/promotion step exists to wait on (unlike §2's MySQL primary failover, which has a real, measured RTO) | **Differs.** VIP-routed issuance fails over quickly (brief `CRITICAL` blip at the moment, matching NGINX's default failure-detection window), but the status page then reads `DEGRADED`, not `OK`, for the entire outage — by design: `keystone-status.py` deliberately validates directly against every node's own IP, so it correctly reports one node unreachable rather than only checking through the VIP. A restarted node also takes ~30s longer than SSH/ping suggest before it actually answers, due to `mod_wsgi`'s own worker startup time, not a platform issue (F-028) |
| 2 | Get a token from node A, stop node A, validate that token against node B | Whether cross-node validation is really enabled by shared Fernet keys (as this LLD argues) or genuinely depends on memcached (as `haFullStack.md` §7 claims) | Token validates successfully via node B — if this fails, the claim in §3.1 was wrong instead and memcached (or something else) actually matters here; either outcome is real information, not assumed | **Confirmed as expected.** `HTTP 200` validating node A's token against node B with node A fully stopped, zero memcached involvement (F-027) |
| 3 | Stop one memcached node | memcached is a performance/revocation cache, not required for basic Fernet validation (per §3.1's claim, being tested here too) | Token issuance and validation keep working — slower, or with more redundant crypto work, but not broken | **Confirmed as expected.** `HTTP 201`, ~3s instead of sub-second |
| 4 | Stop both memcached nodes | Same as test 3, pushed further — is memcached ever a hard dependency for basic auth, or only for revocation-list propagation | Basic token issuance/validation still works; a revoked-token check may not propagate as fast without memcached available, but that's a different claim than "auth is down" | **Confirmed as expected.** Still succeeded (4.7s) with both nodes down; first attempt right after the second node went down hit a 15s client timeout with no response, consistent with a one-time retry/backoff penalty rather than a hard block |
| 5 | Stop both Keystone nodes | Genuine identity-tier outage — the one failure mode with no redundancy left to test | `keystone-status.html` correctly shows `CRITICAL`, distinct from `DEGRADED` | **Confirmed as expected.** Recovered cleanly to `OK` once restarted, subject to the same `mod_wsgi` worker-startup delay as test 1 (F-028) |

Test 2 is this slice's equivalent of §2's test 3 (2A-13) — the one most
likely to be skipped as "obviously fine," and the one actually worth
running, because it's the test that resolves the memcached question
rather than assuming either the design doc or this LLD's counter-claim.

#### 3.3.2 OpenTofu Implementation

Illustrative — to be proven and corrected against real infrastructure,
same process as §1/§2:

```hcl
# examples/ha-frontend-lb/main.tf  (additions — illustrative)

module "security_groups" {
  # ...existing nginx/frontend/mysql/proxysql groups, plus:
  security_groups = {
    # ...
    keystone = {
      description = "Keystone identity tier — API from nginx SG only, plus SSH"
      ingress_rules = {
        api = { ip_protocol = "tcp", from_port = 5000, to_port = 5000, cidr = local.bridge_cidr }
        ssh = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
      }
      egress_rules = { all = { ip_protocol = "-1", cidr = "0.0.0.0/0" } }
    }
    memcached = {
      description = "memcached — Keystone SG only, plus SSH"
      ingress_rules = {
        memcache = { ip_protocol = "tcp", from_port = 11211, to_port = 11211, cidr = local.bridge_cidr }
        ssh      = { ip_protocol = "tcp", from_port = 22, to_port = 22, cidr = var.admin_cidr }
      }
      egress_rules = { all = { ip_protocol = "-1", cidr = "0.0.0.0/0" } }
    }
  }
}

resource "random_id" "fernet_key0" { byte_length = 32 }
resource "random_id" "fernet_key1" { byte_length = 32 }

module "memcached" {
  source              = "../../modules/instance-group"
  project             = var.project
  environment         = var.environment
  owner               = var.owner
  name                = "memcached${local.sfx}"
  image_id            = "ubuntu-22.04"
  flavor              = var.memcached_flavor
  count_instances     = 2
  vpc_id              = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id           = module.subnets.subnet_ids_by_key["main${local.sfx}"]
  security_group_ids  = [module.security_groups.security_group_ids_by_key["memcached${local.sfx}"]]
  user_data           = local.memcached_user_data
}

module "keystone" {
  source              = "../../modules/instance-group"
  project             = var.project
  environment         = var.environment
  owner               = var.owner
  name                = "keystone${local.sfx}"
  image_id            = "ubuntu-22.04"
  flavor              = var.keystone_flavor
  count_instances     = 2
  vpc_id              = module.vpc.vpc_ids_by_key[local.vpc_key]
  subnet_id           = module.subnets.subnet_ids_by_key["main${local.sfx}"]
  security_group_ids  = [module.security_groups.security_group_ids_by_key["keystone${local.sfx}"]]
  user_data           = local.keystone_user_data   # references mysql_all_ips + memcached IPs + fernet keys
}
```

`local.keystone_user_data` needs `module.memcached`'s IPs and the MySQL
cluster's IPs (both already-known outputs by this point in the apply
graph, same mechanism as ProxySQL referencing MySQL in §2) plus the two
`random_id` resources' `.b64_url` outputs — no self-reference problem
here at all, since both Keystone nodes get genuinely identical
`user_data` (no per-node bootstrap/joiner split), so this doesn't need
§2.3.2's two-module-call pattern.

#### 3.3.3 Ansible Implementation

Same provisioning shape as §1.3.3/§2.3.3 — new `security_group` ×2,
`instance` ×2 for memcached and ×2 for Keystone (all four with identical
`user_data` within their pair, a simple loop suffices, no per-instance
branching needed). Fernet key generation has no direct Ansible-native
equivalent to Terraform's `random_id` resource — `openssl rand
-base64 32` run once via a local task, with the result passed as a
`user_data` template variable to both `instance` tasks, is the closest
equivalent.

---

### 3.4 On-Prem Environment

Architecturally identical to Lab — same 2× Keystone + 2× memcached,
active-active, shared MySQL backend — with real vhost-based routing
(`identity.example.com`) instead of Lab's dedicated-port substitution,
since real DNS is available. Same open question as §1.4/§2.4: an
existing on-prem identity provider (LDAP, an existing Keystone
deployment, SSO) may make this whole tier unnecessary duplication for a
given estate — a real per-deployment decision, not resolved generically
here.

### 3.5 AWS Environment

**Genuinely open, not just illustrative** — unlike §1/§2, there isn't an
obvious managed-service substitution to sketch here. Keystone-on-EC2
(identical to Lab/On-Prem, just a different provider) is the default
assumption until confirmed otherwise; IAM/Cognito are NOT drop-in
replacements (different API, different token model, nothing in this
stack currently expects OpenStack Identity API v3 compatibility from
them) and shouldn't be assumed as a substitution without a real decision
to redesign around them.

### 3.6 Cross-Environment Consistency

| Aspect | Lab | On-Prem | AWS |
|---|---|---|---|
| Keystone node count | 2 (active-active) | 2 (active-active) | 2 (active-active) — until confirmed otherwise |
| memcached node count | 2 | 2 | 2 |
| Fernet key distribution | Terraform `random_id`, injected at apply time | Same, or Ansible `openssl rand` equivalent | Same |
| Client routing | Dedicated port (matches the real backend application's addressing, per direct confirmation — not a Lab-only DNS workaround) | Vhost (`identity.example.com`) as originally documented — may need revisiting once the backend tier confirms its actual addressing scheme | Vhost, or ALB path/host rule if reusing §1's ALB — same caveat |
| Failure tolerance | Either node lost — zero impact, no election | Same | Same |

### 3.7 Open Items Before Implementation

- **AWS Keystone architecture** — genuinely undecided (§3.2/§3.5), not
  just unconfirmed detail. Needs a real decision (Keystone-on-EC2 vs. a
  deliberate redesign around IAM/Cognito) before that build starts.
- ~~Memcached's actual role~~ — **resolved (F-027).** Tests 2/3/4 confirmed
  shared Fernet keys, not memcached, enable cross-node validation.
  `haFullStack.md` §7 corrected to v1.5.
- ~~Restart-reachability window~~ — **resolved (F-028).** Not a CloudCore
  platform gap — `mod_wsgi`'s own worker-process startup takes ~30s after
  Apache itself is already listening, confirmed by precise timing against
  a real Keystone install and by a clean isolated service showing zero
  such gap. Worth remembering for any future restart-based test of an
  Apache/mod_wsgi service specifically, not a platform characteristic to
  account for generically.
- **Fernet key rotation** — this slice generates static keys once and
  never rotates them; a real deployment needs `keystone-manage
  fernet_rotate` on a schedule with distribution to every node. Out of
  scope for Lab validation, but should be flagged before this pattern is
  treated as production-ready anywhere.
- **On-prem/AWS existing identity provider** — as with the DB tier,
  confirm whether a given estate already has one before assuming this
  design is needed wholesale.
- **Keystone's real client-addressing scheme (port vs. vhost) —
  confirmed for Lab, not yet for On-Prem/AWS.** Direct confirmation: the
  real backend application addresses Keystone by port, which is why Lab
  uses a dedicated port rather than `haFullStack.md` §7/§8's originally
  documented vhost routing. Whether On-Prem/AWS should follow the same
  scheme or the originally-documented vhost approach isn't settled —
  more detail is expected once the backend tier itself is built. Don't
  assume either way for those builds without revisiting this.

---

## 4. Message Broker Tier — RabbitMQ

### 4.1 Scope

**In scope for this section:** 3× RabbitMQ instances forming a real
cluster with quorum queues (per `haFullStack.md` §6, F-002's 3-node
correction), exposed through the existing NGINX nodes' `stream{}` context
(AMQP, same mechanism as §2's MySQL proxying), and a `rabbitmq-status.html`
page on the frontend tier — same pattern as §2/§3's status pages.

**A claim in `haFullStack.md` §10, verified and partly corrected
(F-031):** it said a 2-of-3 node loss leaves "affected queues have no
leader and stop accepting operations," requiring manual `rabbitmqctl
force_boot` recovery. Quorum queues are Raft-based, and Raft's
majority-quorum requirement is the same theoretical mechanism Group
Replication's Paxos implementation was assumed to enforce — which F-021
found didn't hold in practice for MySQL. RabbitMQ's turned out different
again: failure-mode test 2 confirmed the *protection itself* genuinely
works as documented (a publish was cleanly rejected below quorum, unlike
MySQL) — but the *recovery* half of the claim was wrong. Simply
restarting the missing nodes recovered the tier automatically, with
`force_boot` never run or needed; that command is for a different,
permanent-partition scenario. `haFullStack.md` §10 corrected to v1.7.

**Deliberately not a new VPC/subnet:** reuses the existing one, same
growing-stack pattern as §2/§3.

**A structural difference from §3 (Keystone), matching §2 (MySQL)
instead:** RabbitMQ clustering is **not** genuinely symmetric the way
Keystone's active-active pair was. A new node's Erlang runtime starts as
its own single-node cluster by default; joining an existing cluster is an
explicit, one-directional operation (`rabbitmqctl join_cluster
rabbit@<seed>`) run *on the joining node*, pointed at an already-running
seed. This is the same bootstrap/seed-vs-joiner asymmetry §2.3.2 solved
for MySQL via two separate `modules/compute` calls — the same pattern
applies here (one seed node's `user_data`, two joiners' `user_data`
referencing the seed's known IP), not `modules/instance-group`.

**A second shared-secret problem, matching Keystone's Fernet keys:**
every node in an Erlang cluster must share an identical `.erlang.cookie`
file — RabbitMQ clustering silently refuses to connect nodes with
mismatched cookies. Same fix as §3.3.1's Fernet keys: generate once via
Terraform's `random` provider, inject identically into all three nodes'
`user_data`, no runtime coordination needed.

**Node addressing — IP-based, not hostname-based, as the default choice
here:** RabbitMQ's own node identity is `rabbit@<hostname>` by default,
and clustering requires every node to resolve every other node's
hostname. CloudCore's guest-visible DNS is now fixed (F-022) and could
support this directly via `RABBITMQ_NODENAME=rabbit@<fqdn>` against the
`instances.cloudcore.internal` zone — but that fix is recent and this
would be its first real load-bearing use for inter-node clustering
specifically (as opposed to F-022's own verification, which only tested
simple guest-to-guest resolution). Defaulting to
`RABBITMQ_NODENAME=rabbit@<ip>` instead — RabbitMQ explicitly supports
IP-based node names for exactly this kind of environment — matches the
already-proven, lower-risk pattern used for MySQL's `report_host` (F-017)
and avoids making this slice's success depend on a fix that hasn't been
exercised this way before. Worth trying the DNS-based form in a later
slice once there's a second data point beyond this session's own
introduction of it.

**Explicitly out of scope, for later slices:** the backend application
tier's own AMQP publish/consume logic (§1.1 already deferred the backend
tier generally), TLS between broker and clients (`haFullStack.md` §4/§12,
Phase 4's own scope, not this slice's).

### 4.2 Environment and Tooling Matrix

| Environment | Mechanism | Why | IaC Tool(s) |
|---|---|---|---|
| **Lab** (this platform) | 3× RabbitMQ, quorum queues, fronted by the existing NGINX `stream{}` | Validates the actual HLD-designed architecture (3-node Raft quorum) against real infrastructure | OpenTofu or Ansible |
| **On-Prem** | Same — 3× RabbitMQ + NGINX `stream{}` | Same architecture as Lab — this is the environment the HLD design was actually written for | OpenTofu or Ansible |
| **AWS** | **Amazon MQ for RabbitMQ** (managed) — genuinely comparable to how §1 replaced Keepalived with ALB and §2 replaced Group Replication with RDS, unlike §3's Keystone (no equivalent managed substitute exists there) | AWS ships a real, wire-compatible managed RabbitMQ offering (multi-AZ, quorum queues supported) — worth using rather than hand-rolling Erlang clustering on EC2 unless a specific reason rules it out | OpenTofu or Ansible against `aws_mq_broker` |

### 4.3 Lab Environment (CloudCore)

#### 4.3.1 Design

- Reuses the existing VPC/subnet — one growing stack, not a new one.
- New security group `rabbitmq`: ingress `5672` (AMQP) and `15672`
  (management UI) from the `nginx` SG's subnet only (matching §2/§3's
  pattern — nothing in this tier is exposed directly to clients), plus
  `4369` (EPMD, Erlang port-mapper daemon) and `25672` (Erlang
  distribution/inter-node traffic) restricted to the bridge subnet for
  cluster members to reach each other, plus SSH.
- Erlang cookie generated once via `random_id` (Terraform), injected
  identically into all three nodes' `user_data` as
  `/var/lib/rabbitmq/.erlang.cookie` (owned `rabbitmq:rabbitmq`,
  `0400` — RabbitMQ refuses to start if this file's permissions are too
  open, unlike Keystone's Fernet keys which only needed `0600`).
- 3× RabbitMQ instances via two `modules/compute` calls (seed, then
  joiners referencing the seed's known IP) — same structural reason as
  §2.3.2's MySQL split, see §4.1.
- Cluster formation on each joiner: `rabbitmqctl stop_app`,
  `rabbitmqctl join_cluster rabbit@<seed-ip>`, `rabbitmqctl start_app` —
  with the same explicit wait/retry loop pattern as MySQL's joiners
  (§2.3.2), waiting for the seed's own `rabbitmqctl status` to succeed
  before attempting to join, since cloud-init on all three nodes runs
  concurrently.
- Quorum queue default policy set on the seed node only, once clustered:
  `rabbitmqctl set_policy quorum-default "^" '{"queue-type":"quorum"}' --apply-to queues`
  (`haFullStack.md` §6.3) — a cluster-wide policy, not a per-node
  operation, same reasoning as Keystone's `keystone-manage bootstrap`
  needing to run only once.
- Management UI enabled via `rabbitmq-plugins enable rabbitmq_management`
  on every node (a local plugin-activation command, not a cluster-wide
  operation — needs to run on each node individually) — used by the
  status script for a simple HTTP-based cluster/queue health check
  instead of parsing `rabbitmqctl` CLI output over SSH, matching
  Keystone's status script preferring a real HTTP client over CLI
  parsing.
- Admin user: a Lab-only `admin:admin` credential created via
  `rabbitmqctl add_user`/`set_user_tags ... administrator` on the seed
  node only (same one-time, shared-DB-equivalent reasoning as Keystone's
  bootstrap) — same "lab-only placeholder, not production" convention as
  every other credential in this stack.
- NGINX gets a second `upstream`/`server` pair added to its **existing**
  `stream{}` block (`nginx-stream.conf.tftpl`, extended — not a new file,
  since `stream{}` can only appear once as a top-level context in
  `nginx.conf`, unlike `http{}` server blocks which §3.3.1 could
  concatenate across separate files) — `server { listen 5672; proxy_pass
  rabbitmq_amqp; }`, mirroring the existing MySQL `:3306` block exactly.
- Frontend gets a new `rabbitmq-status.py` timer (same rolling-history
  pattern as `mysql-status.py`/`keystone-status.py`) — connects to the
  management HTTP API (port 15672, `admin:admin`) via the VIP for a
  cluster-overview check (`GET /api/nodes`, `GET /api/queues`), and
  separately publishes and consumes a real test message through a quorum
  queue on every check, proving durability through the real path rather
  than just reporting cluster membership. Verdict logic: `OK` if all 3
  nodes report running and the publish/consume round-trip succeeds;
  `DEGRADED` if fewer than 3 nodes are running but the round-trip still
  succeeds; `CRITICAL` if the round-trip itself fails.

#### 4.3.1a Failure-Mode Test Matrix

| # | Test | What it proves | Expected result | Actual result |
|---|---|---|---|---|
| 1 | Stop one RabbitMQ node | 3-node quorum survives losing 1, matching MySQL's own tolerance (§2's F-001 argument) | `DEGRADED`-equivalent (2/3 nodes), publish/consume keeps working — same "correctly reports reduced redundancy rather than papering over it" design as Keystone's test 1 (§3.3.1a), not literally "zero impact" | **Confirmed as expected.** Self-corrected to `DEGRADED` (2/3, dead node named, round-trip still succeeding) after a ~1min settling window (`stream{}`'s own `max_fails`/`fail_timeout`, not an application-level delay like F-028) |
| 2 | Stop 2 of 3 RabbitMQ nodes | Whether Raft-based quorum queues actually refuse operations below majority (as `haFullStack.md` §10 claims) or reconfigure and continue (as Group Replication actually did, F-021) — resolves this rather than assuming either way | Not assumed — this is the test that answers it, the same standing 2A-13 had for the DB tier and 3A-11 had for Identity | **More nuanced than a simple confirm/deny.** Quorum protection itself is real (publish cleanly rejected, `400`, in well under a second) — genuinely different from MySQL's F-021. But `haFullStack.md` §10's specific recovery claim was wrong: simply restarting the two stopped nodes recovered the tier automatically in 0.04s, no `rabbitmqctl force_boot` needed at all (F-031) |
| 3 | Publish/consume through test 1's single-node failure | Zero message loss for confirmed publishes on quorum queues (`haFullStack.md` §6.5's claim) | A message published before the stop, on a quorum queue, is still consumable after — no data loss for a tolerated failure | **Confirmed as expected.** Uniquely-marked message published before the stop, consumed intact after — identical payload |
| 4 | Restart a stopped node | Whether a previously-clustered node auto-rejoins on its own (cluster membership is disk-persisted in RabbitMQ, unlike MySQL's `group_replication_start_on_boot=OFF` requiring an explicit restart command) or needs the same manual `join_cluster` step as first-time joining | Not assumed — RabbitMQ's docs suggest auto-rejoin, but this project's own experience (F-020) is that a node's *specific role* at cluster-formation time can create asymmetric gaps invisible from the general docs alone | **Confirmed as expected.** A restarted joiner auto-rejoined on its own — back in `Disk Nodes` and `Running Nodes` with zero manual `join_cluster` step, unlike MySQL's bootstrap-node gap (F-020) |
| 5 | Stop all 3 RabbitMQ nodes | Genuine broker-tier outage — the one failure mode with no redundancy left to test | `rabbitmq-status.html` correctly shows `CRITICAL` | **Confirmed as expected.** Recovered cleanly to `OK` once all three were restarted, zero manual intervention |

Test 2 carries the same weight 2A-13 and 3A-11 did for their tiers —
`haFullStack.md` §10's specific manual-recovery claim (`rabbitmqctl
force_boot`) either gets confirmed as the real, necessary procedure or
corrected based on what actually happens.

#### 4.3.2 OpenTofu Implementation

Illustrative — to be proven and corrected against real infrastructure,
same process as §1/§2/§3:

```hcl
# examples/ha-frontend-lb/main.tf  (additions — illustrative)

resource "random_id" "erlang_cookie" { byte_length = 20 }

module "security_groups" {
  # ...existing groups, plus:
  security_groups = {
    # ...
    rabbitmq = {
      description = "RabbitMQ tier — AMQP/management from nginx SG only, Erlang clustering within the bridge subnet, plus SSH"
      ingress_rules = {
        amqp    = { ip_protocol = "tcp", from_port = 5672,  to_port = 5672,  cidr = local.bridge_cidr }
        mgmt    = { ip_protocol = "tcp", from_port = 15672, to_port = 15672, cidr = local.bridge_cidr }
        epmd    = { ip_protocol = "tcp", from_port = 4369,  to_port = 4369,  cidr = local.bridge_cidr }
        erldist = { ip_protocol = "tcp", from_port = 25672, to_port = 25672, cidr = local.bridge_cidr }
        ssh     = { ip_protocol = "tcp", from_port = 22,    to_port = 22,    cidr = var.admin_cidr }
      }
      egress_rules = { all = { ip_protocol = "-1", cidr = "0.0.0.0/0" } }
    }
  }
}

module "rabbitmq_seed" {
  source    = "../../modules/compute"
  project   = var.project
  environment = var.environment
  owner     = var.owner
  instances = local.rabbitmq_seed_instance
}

module "rabbitmq_joiners" {
  source    = "../../modules/compute"
  project   = var.project
  environment = var.environment
  owner     = var.owner
  instances = local.rabbitmq_joiner_instances
}
```

`local.rabbitmq_joiner_instances`' `user_data` references
`module.rabbitmq_seed.private_ips_by_key`'s now-known IP, same
self-reference-avoidance mechanism as §2.3.2's MySQL split. NGINX's
`user_data` now also depends on `module.rabbitmq_seed`/`_joiners` for the
`stream{}` block's second upstream, alongside `module.proxysql` and
`module.keystone` from the earlier slices.

#### 4.3.3 Ansible Implementation

Same provisioning shape as §1.3.3/§2.3.3/§3.3.3 — `security_group` ×1,
`instance` ×1 (seed) then ×2 (joiners, looped, referencing the seed's
registered IP fact) via the existing `instance` module. Erlang cookie
generation has no direct Ansible-native equivalent to Terraform's
`random_id`, same substitution as §3.3.3 used for Fernet keys —
`openssl rand -base64 30` run once via a local task, passed as a
`user_data` template variable to all three `instance` tasks.

---

### 4.4 On-Prem Environment

Architecturally identical to Lab — same 3-node RabbitMQ cluster, quorum
queues, fronted by the same NGINX `stream{}` tier. Same open question as
§1.4/§2.4/§3.4: an existing on-prem message broker (an existing RabbitMQ
cluster, Kafka, ActiveMQ, a managed internal service) may make this whole
tier unnecessary duplication for a given estate — a real per-deployment
decision, not resolved generically here.

### 4.5 AWS Environment

**A real managed-service substitution, unlike §3's Keystone** — Amazon MQ
for RabbitMQ is wire-compatible with standard RabbitMQ clients and
supports quorum queues, multi-AZ deployment, and the same AMQP 0-9-1
protocol this design already assumes. Default assumption for AWS unless
a specific reason (a required RabbitMQ plugin Amazon MQ doesn't support,
a cost constraint, an existing hand-rolled deployment) rules it out —
confirm the specific broker-engine version and plugin support needed
before committing, not assumed settled here.

### 4.6 Cross-Environment Consistency

| Aspect | Lab | On-Prem | AWS |
|---|---|---|---|
| RabbitMQ node count | 3 (quorum-tolerant) | 3 | Amazon MQ multi-AZ (managed, node count abstracted) |
| Erlang cookie distribution | Terraform `random_id`, injected at apply time | Same, or Ansible `openssl rand` equivalent | N/A — managed service |
| Node addressing | IP-based (`rabbit@<ip>`), not hostname/DNS-based — see §4.1 | Same, or real DNS if available | N/A — managed service |
| Client routing | Dedicated port via NGINX `stream{}` (5672), matching §2's MySQL pattern | Same | Amazon MQ's own endpoint |
| Failure tolerance | Loses 1 of 3 — tolerated (pending test 2's actual result); loses 2 of 3 — not assumed, being tested | Same | Managed — AWS's own SLA, not this design's concern |

### 4.7 Open Items Before Implementation

- ~~The 2-node-loss recovery claim~~ — **resolved (F-031).** Protection
  is real (publish rejected below quorum, unlike MySQL); recovery is
  automatic once the missing nodes restart, `force_boot` never needed.
  `haFullStack.md` §10 corrected to v1.7.
- ~~Node-restart auto-rejoin behavior~~ — **resolved.** Confirmed
  auto-rejoin with zero manual intervention, unlike MySQL's bootstrap
  node (F-020).
- **IP-based vs. DNS-based node naming** — deliberately deferred to a
  later slice per §4.1; revisit once guest DNS (F-022) has a second real
  load-bearing use case beyond this session's own introduction of it.
- **AWS Amazon MQ specifics** — plugin support, exact version compatibility,
  and cost need a real decision before that build starts, not assumed
  from this LLD's default recommendation alone.
- **On-prem existing message broker** — as with the DB and Identity
  tiers, confirm whether a given estate already has one before assuming
  this design is needed wholesale.

---

## 5. TLS and Mutual TLS — Cross-Cutting

### 5.1 Scope

**In scope for this section:** a private CA (`step-ca`), TLS termination
on NGINX for client-facing traffic, and TLS + mutual TLS on **every
east-west path that already exists** across the four tiers already
built: NGINX↔MySQL/ProxySQL, NGINX↔Keystone, NGINX↔RabbitMQ.

**Genuinely broader than `haFullStack.md` §4.3 currently documents, by
design:** §4.3 is written backend-centric (backend→Keystone,
backend→ProxySQL, backend→RabbitMQ) — but the backend tier doesn't exist
yet (deferred in every slice's own scope note so far, §1.1/§2/§3/§4).
Rather than wait for a tier that isn't built, this slice covers the
*mesh of trust* the user actually asked for — every service that already
exists trusting every other service it talks to — and specifies
backend's own mTLS requirements precisely enough to build against once
that tier arrives, without needing this section revisited then.

**A claim in `haFullStack.md` §4.1 to correct before building, not
carry forward:** it specified 90-day certificate lifetimes. Confirmed
directly against a real `step-ca` install: its default provisioner caps
certificate duration at **24 hours** when `authority.claims` is left
unconfigured — that's `step-ca`'s own unconfigured default, not a fixed
ceiling; `maxTLSCertDuration`/`defaultTLSCertDuration` in `ca.json` are
plain config values, confirmed directly by setting them to `8760h` and
observing a freshly-issued cert's own `Valid... to:` field jump from
~24h out to exactly 365 days out. `step-cli` ships a built-in
`step ca renew --daemon` mode that continuously auto-renews regardless
of the configured duration (it renews at a fixed *fraction* of whatever
validity window the CA hands out — 2/3 by default — not a fixed
absolute interval), so lengthening the duration doesn't reintroduce the
"manual, easily-forgotten renewal" problem `haFullStack.md` §4.1
originally warned against.

**Second correction, once the Lab's own CA needed to reflect real
On-Prem/AWS lifetimes, not just `step-ca`'s convenient default:** built
and initially ran with the 24h default unmodified — reasonable for a
quick smoke test, but a mismatch against the 1-year service-cert
lifetimes a real enterprise/ACM-backed PKI would actually issue, and
this project's own stated goal is a Lab that reflects those later
environments as closely as practicable. `authority.claims` is now set
explicitly (`ca-cloud-init.yaml.tftpl`'s `setup-ca.sh`, edited via a
small `python3` JSON patch before `step-ca` first starts, so no restart
is needed for it to take effect) to `maxTLSCertDuration`/
`defaultTLSCertDuration: 8760h` (365 days). Confirmed directly: `step
ca renew` (as opposed to `step ca certificate`, a fresh issuance) does
**not** pick up a changed CA default on its own — it re-requests the
*same duration the original cert had*, not the CA's current default,
unless `--expires-in` is passed explicitly. Every already-running
node's cert had to be force-reissued (`step ca certificate ... --force`,
not renewed) to actually pick up the new 365-day window — a genuinely
easy trap: assuming a CA policy change propagates to existing certs on
their next scheduled renewal is wrong for `step-ca`'s renew semantics.

**A topology change made alongside this slice, not part of TLS itself:**
§1's standalone NGINX/Keepalived tier is now co-located on the ProxySQL
nodes rather than its own dedicated pair — two fewer nodes, and only the
node actively holding the VIP ever serves real traffic anyway, so
pairing the LB layer with a tier that's otherwise idle between requests
(ProxySQL) costs nothing functionally. `main.tf`/`locals.tf` carry the
full rationale; the one genuine wrinkle is that this section's own
`stream{}` config for MySQL now has to avoid referencing its own node's
module output (a circular dependency, since that config is baked into
the very node it would be pointing at) — resolved by pointing the
MySQL upstream at `127.0.0.1` instead of a 2-node list, which turns out
to be more correct than the pre-merge design, not just a workaround:
Keepalived only ever routes real client traffic to whichever node
currently holds the VIP, so the passive BACKUP node's NGINX never needs
to fail over to a peer in the first place. The frontend tier was
deliberately **not** folded into this same merge, despite being an
equally idle pool of nodes — it's the mesh's own independent observer
(`tls-status.py` and the other status scripts specifically exist to
report on NGINX/ProxySQL/Keystone/RabbitMQ's health from outside them),
and co-locating an observer with the thing it observes means a real
failure there takes out visibility into itself at the same time.

**A deliberate Lab simplification, flagged as an open item, not silently
assumed:** the CA runs as a **single node**, not HA — matching this
project's existing "flag it explicitly" convention for Lab-only
simplifications (static Fernet/Erlang-cookie secrets with no rotation,
`admin:admin` credentials). Worth being precise about the actual blast
radius: a CA outage blocks new certificate *issuance and renewal* —
confirmed directly, this gets its own failure-mode test (§5.3.1a test 4)
— but does **not** break TLS connections already using already-issued,
still-valid certificates, since certificate validation is purely
cryptographic (chain-of-trust against the root, which every node already
holds a copy of) and needs no live connection back to the CA at
handshake time.

**Explicitly out of scope, deferred to later work:** RabbitMQ's own
inter-node Erlang distribution TLS (`inet_tls_dist`) — a materially
different, lower-level mechanism than the AMQP-protocol TLS this slice
covers, protecting RabbitMQ's own clustering traffic rather than client
connections to it; MySQL Group Replication's own recovery-channel TLS
(`group_replication_recovery_use_ssl`) — same reasoning, inter-MySQL-node
traffic rather than client connections; full backend↔X mTLS build-out,
which needs the backend tier to exist first (§1.1) — this section
specifies its requirements precisely so building it later doesn't need
this slice revisited.

### 5.2 Environment and Tooling Matrix

| Environment | Mechanism | Why | IaC Tool(s) |
|---|---|---|---|
| **Lab** (this platform) | Self-hosted `step-ca`, one node | Validates the actual mesh-of-trust architecture against real infrastructure; `step-ca`/`step-cli` aren't in Ubuntu's default repos, installed via pinned GitHub-release `.deb`s, same pattern as ProxySQL | OpenTofu or Ansible |
| **On-Prem** | Same `step-ca`, **or** an existing enterprise PKI/CA if the estate already has one | Same open question as every other tier — confirm before assuming this design is needed wholesale (§5.7) | OpenTofu or Ansible |
| **AWS** | **Split, not a single substitute** — ACM for the client-facing VIP certificate (ALB/NLB integration is native and free); ACM Private CA for internal mTLS issuance is the closer analog to `step-ca`, but is a distinct, metered AWS service, not a drop-in — confirm before assuming it's the right call over self-hosting `step-ca` on EC2 too | OpenTofu against `aws_acm_certificate` (public) and, if adopted, `aws_acmpca_certificate_authority` (private) |

### 5.3 Lab Environment (CloudCore)

#### 5.3.1 Design

- Reuses the existing VPC/subnet — one growing stack, not a new one.
- New security group `ca`: ingress `8443` (the CA's own HTTPS API, both
  for issuance/renewal calls and for `roots.pem` bootstrap fetches) from
  the bridge subnet (every tier needs to reach it), plus SSH.
- One new instance (`ca-a`) via `modules/compute` — genuinely a single
  node this time, not a bootstrap/joiner or active-active pair, per
  §5.1's flagged simplification.
- `step-ca`/`step-cli` installed from pinned, checksum-verified GitHub
  release `.deb`s (`smallstep/certificates` and `smallstep/cli`), same
  pattern as ProxySQL's own install script.
- `step ca init` run non-interactively at boot (`--dns`, `--address
  :8443`, `--provisioner admin`, `--password-file`, `--deployment-type
  standalone`) — confirmed directly to produce a working root+intermediate
  chain and a running CA server in well under a minute.
- **A second shared-secret problem, same pattern as Keystone's Fernet
  keys and RabbitMQ's Erlang cookie:** every node that needs to request
  or renew a certificate needs the CA's **provisioner password** to
  authenticate to it. Generated once via Terraform's `random` provider,
  injected identically into the CA node's own `user_data` (to init the
  provisioner) and every certificate-requesting node's `user_data` (to
  use it) — no runtime coordination needed, same mechanism as before.
- Every other node's cloud-init: wait/retry loop for the CA to become
  reachable (same explicit-wait pattern as every prior tier's bootstrap
  dependency, not relying on Terraform apply order) — polling its
  plain-HTTP cert-serving endpoint (`:8080`, see above), not `step ca
  health` — then `step ca certificate <SAN> <cert-path> <key-path>
  --ca-url https://<ca-ip>:8443 --root <fetched-root> --provisioner
  admin --provisioner-password-file <path>`. **Built with IP-only SANs
  (own private IP + the VIP), not the node's CloudCore-DNS hostname** —
  a deliberate simplification against this section's original draft,
  not an oversight: every inter-tier connection in this stack is already
  IP-based (Terraform bakes `private_ips_by_key` into upstream configs
  directly), so a DNS-hostname SAN would add a second identity every
  cert carries without anything in the stack actually connecting by
  that name. `VERIFY_IDENTITY`-level client verification needs the SAN
  to match however the client *actually* connects — which is always by
  IP here — so adding the DNS name would be dead weight, not defense in
  depth. Worth revisiting if a future slice starts connecting by
  hostname instead.
- `step ca renew --daemon` runs as a systemd service on every
  certificate-holding node, handling the renewal cycle automatically —
  no custom renewal script needed, this is a real, built-in `step-cli`
  mode. Renews at 2/3 of whatever validity the CA hands out (365 days,
  see above), not a fixed absolute interval. **Confirmed directly a CA
  policy change (the 24h→365d duration change above) does not propagate
  to already-issued certificates on their next renewal** — `step ca
  renew` re-requests the *same duration the original cert had*, not the
  CA's current default; only a fresh `step ca certificate` issuance
  picks up a changed default (F-038, `haFullStack-Findings-Log.md`).
- **NGINX**: TLS termination for client-facing traffic (443, `http{}`
  context) using a CA-issued cert for the VIP's own identity, replacing
  §1's plain-HTTP `:80` listener (kept alongside, not removed, for the
  Lab's own `mysql-status.html`/`keystone-status.html`/
  `rabbitmq-status.html` pages, which don't need TLS for their own
  purpose). **Correction from this section's original draft: `stream{}`
  needs no `proxy_ssl_*` directives at all.** MySQL/ProxySQL negotiate
  TLS *in-band*, an upgrade within the existing TCP stream, so `stream{}`
  stays a transparent byte-forwarder regardless; RabbitMQ's TLS is a
  dedicated port from the first byte, but that's still just a raw
  passthrough to a port, no NGINX-side TLS awareness needed either.
  Keystone's new TLS port (5443) is routed through `stream{}` the same
  way specifically so NGINX doesn't have to act as its own TLS client at
  all — the only genuine TLS *termination* NGINX does anywhere in this
  mesh is its own client-facing `:443`. (NGINX is now co-located on the
  ProxySQL nodes rather than its own tier — see the note at the top of
  this section and `main.tf`/`locals.tf`'s own comments for why, and how
  the resulting self-reference for the MySQL upstream — this config
  being baked into the very node it points at — is resolved via
  `127.0.0.1` rather than a 2-node list.)
- **MySQL**: `REQUIRE X509` on every client-facing account (`appuser`,
  `keystone`, `proxysql_monitor`), server cert from the CA — confirmed
  directly end-to-end: a TLS connection without a client cert is
  rejected (`Access denied`), a TLS connection with a CA-issued client
  cert succeeds. **Deliberately not** `require_secure_transport = ON`
  server-wide, despite that being this section's original draft — found
  directly that the global flag also blocks Group Replication's own
  internal recovery channel (`repl`, deliberately left plaintext, see
  below), taking down clustering entirely as a side effect (F-034/F-035,
  `haFullStack-Findings-Log.md`). Per-account `REQUIRE X509` enforces
  the same client-facing requirement without that collateral damage.
- **ProxySQL**: TLS on its own client-facing listener — no config
  variable for this exists, confirmed directly (F-032); it expects a
  replacement cert dropped at fixed paths
  (`/var/lib/proxysql/proxysql-{ca,cert,key}.pem`) instead — and TLS to
  its MySQL backends (`mysql-ssl_p2s_*` globals, enabled per-row via
  `mysql_servers.use_ssl`, not a single global boolean). Both confirmed
  working end-to-end against the real deployed stack.
- **Keystone**: Apache `mod_ssl` termination on a **new** port, 5443,
  alongside the existing plain `:5000` vhost (kept for the Lab's own
  convenience, not removed) — `SSLEngine on`,
  `SSLCertificateFile`/`SSLCertificateKeyFile` from the CA-issued cert,
  `SSLVerifyClient require`/`SSLCACertificateFile` requiring a client
  certificate on the mTLS path. `SSLCACertificateFile` needs the full
  chain (root + intermediate), not just the root — applied proactively
  once RabbitMQ hit the identical requirement (F-033) rather than
  waiting to reproduce the same symptom here too. Confirmed working via
  the frontend's own `tls-status.py` continuously reporting a
  successful mTLS handshake against this listener through the VIP.
- **RabbitMQ**: a TLS listener (`5671`, alongside the existing plain
  `5672`) via `ssl_options` in `rabbitmq.conf`
  (`verify = verify_peer`, `fail_if_no_peer_cert = true`,
  `cacertfile`/`certfile`/`keyfile` from the CA). `cacertfile` must be
  the full chain, not just the root — Erlang's SSL stack doesn't
  chain-build from what the client presents the way OpenSSL does;
  confirmed directly (F-033).
- Frontend's status scripts keep using plain HTTP to Keystone/RabbitMQ
  for their own application-level checks (mysql-status.py,
  keystone-status.py, rabbitmq-status.py — deliberately not switched to
  mTLS in this pass, a mechanical follow-up if ever needed) — but a
  **new fourth script, `tls-status.py`**, was added specifically for
  this slice: it holds its own CA-issued client identity and performs a
  real TLS/mTLS handshake against every TLS-enabled listener (NGINX
  `:443`, RabbitMQ `:5671`, Keystone `:5443`, all via raw `ssl.SSLContext`
  socket handshakes; MySQL/ProxySQL `:3306` via the `mysql` CLI's
  `--ssl-mode=VERIFY_IDENTITY`, since that tier's TLS is negotiated
  in-band and a bare socket handshake doesn't speak it) — proving the
  mesh of trust is real, not just configured, the same "answer it, don't
  assume it" standard every other tier's status page already holds
  itself to.

#### 5.3.1a Verification Matrix

Different shape from every prior tier's test — this isn't about node
failure/quorum, it's about certificate issuance, enforcement, and
lifecycle:

| # | Test | What it proves | Expected result | Actual result |
|---|---|---|---|---|
| 1 | Attempt a plain (non-TLS) connection to each service | TLS is actually enforced, not just available | Cleanly rejected, not a silent plaintext fallback | **Confirmed for MySQL** (`appuser`'s `REQUIRE X509` rejects a plain TCP connection, `Access denied`) **and RabbitMQ/Keystone** (their TLS ports, 5671/5443, only speak TLS at all — a plain connection is a protocol mismatch, not a graceful reject, which is the equivalent failure mode for a dedicated TLS port). NGINX's own `:443` is server-only TLS termination, not mTLS, so this test doesn't apply to it in the client-cert sense |
| 2 | Attempt a TLS connection with no client certificate | mTLS (not just server-side TLS) is actually enforced | Cleanly rejected — `REQUIRE X509`-equivalent error | **Confirmed continuously**, not just once — `tls-status.py` performs exactly this test (TLS handshake presenting its own CA-issued client cert) against NGINX/RabbitMQ/Keystone every 2 seconds; a version of this script run *without* loading a client cert was used during template development to confirm the servers reject a certless handshake, matching `verify_peer`/`fail_if_no_peer_cert`/`SSLVerifyClient require`'s documented behavior |
| 3 | Attempt a TLS connection with a valid, CA-issued client certificate | The full mesh of trust actually works end-to-end | Succeeds, with the expected cipher/protocol reported | **Confirmed as expected**, and continuously — the live `tls-status.html` dashboard page shows `OK` with the negotiated cipher/protocol for all four checks (NGINX, RabbitMQ, Keystone, MySQL/ProxySQL) as an ongoing status, not a one-off manual test |
| 4 | Stop the CA node, attempt a fresh certificate request; separately, confirm an already-open TLS connection using an already-issued cert keeps working | §5.1's claimed blast radius — CA is a SPOF for issuance/renewal only, not for using already-issued certs — resolved rather than assumed | New issuance fails; existing, already-issued certs keep authenticating successfully until they naturally expire | **Not yet run** — open item, tracked below |
| 5 | Force a short-lived test certificate close to expiry, confirm `step ca renew --daemon` actually replaces it before the service starts rejecting connections | The auto-renewal claim (§5.1) works in practice, not just in theory — independent of whatever the configured validity window is (365 days in this Lab) | Certificate file's own expiry timestamp advances well before the old one would have lapsed, with no connection-rejecting gap | **Not yet run as originally scoped** (a short-lived-cert-close-to-expiry scenario) — but a closely related fact *was* confirmed directly along the way: `step ca renew` preserves a cert's original requested duration rather than adopting a changed CA default, so a CA-side policy change alone does **not** cause the next scheduled renewal to reflect it (F-038) — every currently-issued cert in this stack had to be force-reissued, not renewed, to pick up the new 365-day window. Test 5 as originally scoped (does the daemon renew before expiry, gap-free) remains an open item |

### 5.3.2 OpenTofu Implementation

Illustrative — to be proven and corrected against real infrastructure,
same process as every prior slice:

```hcl
# examples/ha-frontend-lb/main.tf  (additions — illustrative)

resource "random_id" "ca_provisioner_password" { byte_length = 24 }

module "security_groups" {
  # ...existing groups, plus:
  security_groups = {
    # ...
    ca = {
      description = "step-ca — issuance/renewal API from the bridge subnet, plus SSH"
      ingress_rules = {
        api = { ip_protocol = "tcp", from_port = 8443, to_port = 8443, cidr = local.bridge_cidr }
        ssh = { ip_protocol = "tcp", from_port = 22,   to_port = 22,   cidr = var.admin_cidr }
      }
      egress_rules = { all = { ip_protocol = "-1", cidr = "0.0.0.0/0" } }
    }
  }
}

module "ca" {
  source    = "../../modules/compute"
  project   = var.project
  environment = var.environment
  owner     = var.owner
  instances = local.ca_instance
}
```

Every other tier's `user_data` gains the CA's known IP
(`module.ca.private_ips_by_key`) and the shared provisioner password —
the same "one new dependency threaded through every existing template"
shape as the Erlang cookie was for RabbitMQ, just wider (every tier this
time, not just one).

### 5.3.3 Ansible Implementation

Same provisioning shape as every prior slice — `security_group` ×1,
`instance` ×1 for the CA via the existing `instance` module. The CA
provisioner password has no direct Ansible-native equivalent to
Terraform's `random_id`, same substitution used for Keystone's Fernet
keys and RabbitMQ's Erlang cookie — `openssl rand -base64 24` run once
via a local task, passed to every relevant `instance` task.

---

### 5.4 On-Prem Environment

Architecturally identical to Lab — same single `step-ca` node (or,
per §5.2, an existing enterprise CA if the estate has one — a real
decision, not assumed either way here) — with real DNS available for
SANs rather than Lab's `.internal` guest-DNS substitute.

### 5.5 AWS Environment

**Split across two services, not one substitute** — ACM for the
client-facing VIP/ALB certificate (native, free, auto-renewing — a
strictly better fit than self-hosting `step-ca` for this one piece);
ACM Private CA for internal mTLS issuance if adopted (the closer analog
to `step-ca`, but a distinct, metered, per-certificate-billed service —
confirm the cost model before assuming it over self-hosting `step-ca`
on EC2 too, which remains a legitimate option here unlike RabbitMQ's
AWS story where a managed drop-in clearly wins).

### 5.6 Cross-Environment Consistency

| Aspect | Lab | On-Prem | AWS |
|---|---|---|---|
| CA | Self-hosted `step-ca`, single node | Same, or existing enterprise CA | ACM (public) + ACM Private CA (internal, if adopted) or self-hosted `step-ca` |
| Certificate lifetime | 365 days, auto-renewed (`step ca renew --daemon`) — deliberately set to mirror the other two columns rather than left at `step-ca`'s unconfigured 24h default | Same, or the enterprise CA's own policy (commonly ~1 year for service certs) | ACM-managed (public, auto-rotated ~13 months); policy-dependent (private) |
| Certificate SAN identity | CloudCore DNS hostname + private IP, both | Real DNS hostname | AWS-internal DNS / IP, service-dependent |
| Client routing | Direct TLS/mTLS to each tier's own listener | Same | Same, or ALB-terminated for the public edge |

### 5.7 Open Items Before Implementation

- **CA high availability** — deliberately single-node for Lab (§5.1);
  a real deployment needs to decide between a clustered `step-ca` (needs
  a shared DB backend, real added complexity) or accepting the
  issuance/renewal SPOF with a generous certificate lifetime as
  mitigation. Not resolved generically here.
- **RabbitMQ inter-node and MySQL GR recovery-channel TLS** —
  deliberately deferred (§5.1); both are real, separate mechanisms worth
  a future pass, not bundled into this already-broad slice.
- **Backend↔X mTLS** — fully specified by this section's design (every
  tier already requires client certs; a backend service would request
  its own from the same CA using the same mechanism) but can't be built
  or tested for real until the backend tier itself exists (§1.1).
- **On-prem/AWS existing PKI** — as with every other tier, confirm
  whether a given estate already has a trusted internal CA before
  assuming this design is needed wholesale.
- **Verification matrix tests 4 and 5** (§5.3.1a) — not yet run: CA-node
  outage blast radius (issuance/renewal fails, already-issued certs keep
  working) and a genuine short-lived-cert-close-to-expiry auto-renewal
  gap check. A closely related fact *was* confirmed along the way
  (F-038 — `step ca renew` doesn't adopt a changed CA default, only a
  fresh issuance does), but neither test as originally scoped has been
  run for real yet.

---

## 6. Local Package Repository — Cross-Cutting (superseded by §7 for this stack)

**`ha-frontend-lb` no longer builds the NFS-based design this section
describes** — it was retrofitted onto §7's host-level `cloudcore-repo`
service instead. This section remains accurate as a design reference
(the same shape a genuinely air-gapped On-Prem estate would still need,
§6.4) but is no longer what the Lab config actually does.

### 6.1 Scope

**In scope:** a real, indexed local apt repository plus a pinned-
artifact cache, both served over NFS, so a full stack rebuild installs
every package from local infrastructure instead of the real Ubuntu
mirror. Motivated directly by F-037 (`haFullStack-Findings-Log.md`):
rebuilding this stack's ~15-17 nodes concurrently repeatedly exhausted
the Lab's own path to `archive.ubuntu.com` (no IPv6 route, plus
ordinary mirror-side congestion under that much simultaneous load),
causing multi-tens-of-minutes stalls and cloud-init failures that
looked like real bugs until traced back to network congestion.

**Built and verified for real, not drafted first this time** — unlike
every earlier slice, this one was small and mechanical enough (no new
application-level failure modes to reason about ahead of time, just
infrastructure plumbing) that building directly and correcting the
design against what was actually found was faster than drafting an
illustrative design first. Every claim below reflects the real,
finished build, including six real findings (F-039–F-045) hit and
fixed getting there.

**Deliberately a "download once" cache, not a continuously-reconciled
mirror:** refreshed only when the base Ubuntu release increments (a new
repo snapshot from scratch — a `.deb` built for 22.04 doesn't belong in
a 24.04 repo, this isn't a "patch the existing one" operation) or when a
security patch is specifically needed for one of the packages this
stack actually installs. Matches how a genuinely bandwidth-constrained
or air-gapped on-prem environment would operate — a deliberate patch
cadence, not perpetual sync — which is also why a caching *proxy*
(`apt-cacher-ng` or similar) was considered and rejected in favor of a
real pre-built repo: a caching proxy still needs to reach the real
mirror on every cache miss, which doesn't hold up as a stand-in for an
environment that might have no outbound path to the internet at all.

### 6.2 Environment and Tooling Matrix

| Environment | Package source | Artifact source |
|---|---|---|
| Lab (CloudCore) | `cloudcore_nfs_server`-hosted local apt repo | Same NFS server, `artifacts` share |
| On-Prem | Same pattern — an internal apt mirror/repo is a standard, common piece of on-prem infrastructure already | Internal artifact store (Nexus/Artifactory-class, or the same NFS-style approach) |
| AWS | S3-backed apt mirror (e.g. via `aptly`) or a VPC-local yum/apt mirror instance; CodeArtifact for anything it covers | S3 |

### 6.3 Lab Design

- **NFS server** (`modules/nfs-server`, `cloudcore_nfs_server`) with two
  exports: `apt-repo` (the indexed package repo) and `artifacts` (the
  pinned `step-ca`/`step-cli`/`proxysql` `.deb`s). `clients` set
  explicitly to `local.bridge_cidr`, not left at the module's own
  `"vpc"` default — confirmed directly that default resolves to the
  CloudCore VPC's own declared CIDR block, not the Lab bridge's real
  DHCP subnet every bridged instance actually gets its address from
  (F-041), the same mismatch this stack's security-group rules already
  route around everywhere else. Fixed at the platform level since this
  was written — `api/nfs.py`'s `"vpc"` resolution now uses the real
  bridge CIDR automatically when bridge networking is in use — so this
  explicit override is no longer strictly required for new templates,
  though it's left in place here as harmless and self-documenting.
- **One-shot repo-builder instance** (`modules/compute`), gated behind
  `var.build_repo_now` (default `true`) rather than a permanent part of
  the stack — matches §6.1's "build once" policy at the Terraform level:
  set `false` on a later apply and OpenTofu destroys just the builder,
  leaving the NFS server and its already-populated shares untouched.
  `cloudcore_nfs_server` itself is a fixed appliance with no `user_data`
  hook of its own (confirmed directly against `api/nfs.py` — it always
  provisions `nfs-kernel-server` + LVM, nothing else), so the actual
  repo-building work has to happen on a separate node that mounts the
  same exports and populates them, not on the NFS server itself.
- **Repo build**: `apt-get install --download-only --reinstall -y
  <full package closure>` (every top-level package every tier's own
  cloud-init installs — apt resolves the full transitive dependency
  graph the same way it would against the real mirror), then
  `dpkg-scanpackages . /dev/null > Packages` + `gzip` — a flat-repo
  layout (no `dists/`/`pool/` hierarchy), the simplest valid structure
  for a small custom repo. A `MANIFEST.txt` records the build date,
  Ubuntu release, and exact package list for traceability against the
  "only refresh when needed" policy. A `.build-complete` sentinel file
  lets every consuming node's own wait-loop know the repo is actually
  populated, not just that the NFS mount succeeded.
- **Artifact fetch**: the pinned GitHub-release `.deb`s (previously
  curled independently by every certificate-requesting/ProxySQL node —
  12+ redundant fetches of the same files) are now fetched exactly once
  by the repo-builder, with the same checksum verification every
  individual node used to do itself, still applied locally on each
  consuming node against the NFS-served copy (defense in depth — the
  NFS path is trusted, but the check is nearly free and catches a
  corrupted copy either way).
- **Every consuming tier's own `bootcmd`** (not `write_files`/`runcmd` —
  cloud-init's earliest hook, the only one guaranteed to run before the
  `packages:` module below it): installs `nfs-common`, mounts both NFS
  exports, waits for the `.build-complete` sentinel, then rewrites
  `/etc/apt/sources.list` to `deb [trusted=yes] file:///mnt/apt-repo/
  ./` — a flat-repo `file://` source, no web server needed. Two
  findings specific to this step:
  - `apt_preserve_sources_list: true` is required — cloud-init's own
    `apt_configure` module otherwise silently regenerates
    `/etc/apt/sources.list` from its own default-mirror template right
    after `bootcmd` runs, discarding the rewrite with no error
    anywhere (F-042).
  - The bootstrap `apt-get update`/`install nfs-common` step itself has
    to be scoped to a minimal, temporary `jammy main`-only source list
    (`-o Dir::Etc::sourcelist=...`), not the full default — a plain
    `apt-get update` at that point in boot still refreshes every
    component's index (~40MB combined) against the real mirror, which
    alone was enough to reproduce F-037's congestion across 15
    concurrently-booting nodes even though it's "just installing one
    small bootstrap package" (F-043).
- **A necessary, small, acknowledged exception to "no mirror traffic
  during a rebuild":** `nfs-common` itself has to come from the real
  Ubuntu mirror — nothing can mount the local repo before an NFS client
  exists to mount it with. Scoped to the minimum possible footprint
  (F-043 above), not eliminated entirely.

### 6.3.1a Verification

Confirmed directly, not assumed:
- A real `apt-get install --simulate` against the finished repo resolved
  every package this stack needs — including Keystone's full OpenStack
  dependency chain (SQLAlchemy, the `oslo.*` family, etc.) — entirely
  from `file:///mnt/apt-repo`, zero packages missing.
- A node rebuilt with the full fix (F-042 + F-043 both applied) reached
  the same point in its own boot log — past the *entire* package-install
  phase, into TLS setup — in under 6 minutes of guest uptime, versus
  22+ minutes stuck on the bootstrap step alone before the fix (measured
  on the same tier, Keystone, before and after).
- The finished, fully-rebuilt stack showed all four dashboard checks
  (`mysql-status`, `keystone-status`, `rabbitmq-status`, `tls-status`)
  reporting `OK`, confirmed twice in a row — the same real-infrastructure
  bar every other slice in this document holds itself to.

### 6.4 Open Items

- ~~**F-040's missing share-update path**~~ — **resolved.** A new
  `PATCH /v1/nfs-servers/<id>/shares/<name>` backend endpoint plus
  matching provider `Update()` support (detecting field-level changes to
  an existing share, not just add/remove-by-name) now handle this
  in-place — no `-replace` needed any more. Verified live against a real
  NFS server with an existing share.
- **F-045's recurring `nginx`+`keepalived` dpkg quirk** — reliably fixed
  with a one-line `apt-get install -f`, but not root-caused; worth a
  closer look (e.g. splitting the package list, or an automatic
  self-healing step in `runcmd`) if it keeps recurring on future
  rebuilds.
- **On-Prem/AWS repo builds** — this section's Lab design assumes a
  fresh Ubuntu-22.04-specific repo snapshot; the On-Prem/AWS equivalents
  need their own build process appropriate to whatever mirror tooling
  (`aptly`, `apt-mirror`, or an existing internal mirror) is actually
  available in those environments — not assumed to be a drop-in port of
  the Lab's own `dpkg-scanpackages` approach.

---

## 7. Host-Level Package Repository — Platform Capability (not project-scoped)

### 7.1 Scope

Distinct from §6: this is CloudCore host infrastructure, not part of
`ha-frontend-lb` or any other project's Terraform. §6 exists because it
was built first; this section exists because building it surfaced a
better generalization — one repo, always available, shared by every
project and every example template — rather than every project needing
its own NFS server and one-shot builder instance. Not a replacement for
§6 in this document yet: `ha-frontend-lb` still builds and uses its own
NFS repo as designed there, and retrofitting it onto this service
instead is a real, available, not-yet-done next step.

### 7.2 Components

- **`api/serve-package-repo.py`** — a `SimpleHTTPRequestHandler`
  subclass bound to `192.168.100.1:8090` (the bridge's own gateway
  address, set up by `setup-network.sh` — every bridged instance's
  default gateway, reachable regardless of which VPC/subnet a consuming
  guest belongs to), serving `api/package-repo/<codename>/{apt-repo,
  artifacts}/`. One directory per Ubuntu codename so multiple guest OS
  versions can coexist.
- **`cloudcore-repo.service`** (installed by `api/setup-package-repo.sh`,
  a one-time `sudo` step) — a systemd unit wrapping the above,
  `Restart=always` and `WantedBy=multi-user.target`, specifically so it
  survives a host reboot with no manual restart, unlike a manually
  relaunched background process such as `dnsmasq`'s. Not a CloudCore
  resource — no VPC, no instance, nothing in the API or database — so no
  project's `tofu destroy` can reach it.
- **`api/build-package-repo.sh`** — populates the repo, run by hand on
  the same "OS bump or security-patch" cadence as §6.3, never
  automatically. Drives CloudCore's own REST API directly with `curl`
  (not Terraform — a one-shot, non-declarative operation outside any
  project's lifecycle) to stand up a throwaway builder instance matching
  the target codename (`jammy` → `ubuntu-22.04`), naming its resources
  `cloudcore-repo-builder-<unix-timestamp>-*`. On the builder: trusts two
  third-party apt repos unconditionally (Adoptium, for
  `ghidra-workstation`'s `temurin-21-jdk`; kismetwireless.net, for
  `wifi-sniffer`'s `kismet` metapackage and its ~20
  `kismet-capture-*` sub-packages — neither exists in Ubuntu's own
  archive) — harmless on a throwaway instance even for a build that
  doesn't strictly need them — then `apt-get install --download-only
  --reinstall` across the union of every example template's package
  list (plus `linux-headers-$(uname -r)`/`linux-modules-extra-$(uname
  -r)`, valid as long as a guest boots the same image around the same
  time this ran), `dpkg-scanpackages` to index, `scp` the result back,
  tear the builder down. Pinned release artifacts (`step-ca`,
  `step-cli`, `proxysql`, the Ghidra release zip, the `kiwix-tools`
  tarball, the Wikipedia ZIM `kiwix-library` needs every build, and a
  pre-cloned tarball of `wifi-sniffer`'s RTL8812AU driver source — DKMS
  still compiles it per-kernel on the guest, only the GitHub clone
  itself is pre-cached) are fetched directly on the host, no builder VM
  needed, checksums recorded in `MANIFEST.txt` (each guest's own
  cloud-init still verifies its checksum independently — this cache is a
  bandwidth shortcut, not a trust boundary).

A guest consumes it with a plain `sources.list.d` drop-in — no NFS
mount, no `bootcmd`, no `apt_preserve_sources_list`, none of §6's
cloud-init workarounds, since nothing is rewriting `/etc/apt/sources.list`
itself:

```
deb [trusted=yes] http://192.168.100.1:8090/jammy/apt-repo ./
```

### 7.3 Protections

- **`api/teardown-network.sh`** refuses to delete `ccbr0` while
  `cloudcore-repo.service` is active — deleting the bridge doesn't stop
  the service, it silently cuts every guest off from it — unless run
  with `--force`.
- **`api/package-repo/`** is host-local, gitignored build output,
  expensive to regenerate (15-20+ minutes, several GB of real
  downloads). `.gitignore` carves out one tracked exception,
  `api/package-repo/README.md`, so a `git clean -xfd` leaves a marker
  behind explaining what used to be there and how to rebuild it, instead
  of the directory just silently going empty.

### 7.4 Verified

- End-to-end: a real, unrelated running guest VM successfully installed
  a package from the repo over plain HTTP.
- A live rebuild covering every template's superset: 652 packages
  indexed (up from 256 in the `ha-frontend-lb`-only build), both
  third-party repos resolved and included, all pinned artifacts fetched
  and checksummed, throwaway builder torn down cleanly with the
  `cleanup()` trap leaving no orphaned VPC/SG/subnet.
- A genuinely stalled (not merely slow) apt-mirror connection hit mid
  `apt-get update` during that live rebuild — zero bytes read over 20+
  seconds on an `ESTAB` TCP connection — recovered by killing the stuck
  apt method worker process on the builder, which unstuck apt's own
  retry; the build then completed normally on the next pass (F-047).
- `teardown-network.sh`'s new guard confirmed refusing to proceed
  without `--force` while the service was active, bridge left untouched.
- The `cloudcore-package-repo` → `cloudcore-repo` service rename migrated
  live (disable/remove old unit, run `setup-package-repo.sh` again) with
  zero data loss — same bind address, same `api/package-repo/` contents,
  confirmed serving all 652 packages immediately after.
- **`ha-frontend-lb` retrofitted onto this service** (2026-09-12) —
  `module.nfs`/`module.repo_builder` and every tier's NFS-mount `bootcmd`
  block removed, replaced with a single `cat > /etc/apt/sources.list`
  write (no wait loop needed — unlike the per-project NFS repo, this
  service has no build-completion race to wait on, it's already running
  before any guest even starts booting). `apt_preserve_sources_list:
  true` kept, not dropped — confirmed directly it's still needed (cloud-
  init's own apt module still overwrites a plain `sources.list.d`-style
  rewrite exactly as it did for the NFS design, F-042). A real `tofu
  apply` from empty state: 28 resources instead of the old 31 (15 nodes,
  not 17), all four dashboard checks `OK`, confirmed via cloud-init logs
  that the CA node had zero `archive.ubuntu.com` references and the
  MySQL bootstrap node had 44 references to the host-level repo and
  zero to the real mirror, clean `tofu destroy` afterward. One
  already-documented, unrelated issue recurred along the way (F-045's
  `nginx`/`keepalived` dpkg race) and got its usual one-line fix.

### 7.5 Open Items

- **Kernel-header drift** — `linux-headers-$(uname -r)`/
  `linux-modules-extra-$(uname -r)` are cached under whatever kernel the
  builder happened to boot with; a guest that picks up a kernel bump via
  `unattended-upgrades` before the repo is next rebuilt falls back to a
  live `apt-get` for just those two packages.
- **On-Prem/AWS equivalents** — this section is Lab/bridge-network
  specific (the `192.168.100.1` gateway address doesn't exist outside
  it); On-Prem/AWS need their own equivalent, appropriate to whatever
  internal mirror or artifact-store tooling is actually available there
  — same caveat §6.4 already carries for its own repo design.

---

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-09-10 | Paul Scott | First slice — Load Balancer Tier, client-to-frontend, all three environments. |
| v0.2 | 2026-09-10 | Paul Scott | Phase 1.A (Lab/OpenTofu) built and verified against real infrastructure; §1.3.1/§1.3.2 updated with as-built notes; §1.7 open items resolved for Lab; linked to new `haFullStack-Findings-Log.md`. |
| v0.3 | 2026-09-10 | Paul Scott | Second slice — §2, Database Tier (MySQL High Availability): 3-node Group Replication + ProxySQL, exposed via the existing NGINX `stream{}`, extending `examples/ha-frontend-lb/` in place as one growing stack. Added §2.3.1a's failure-mode test matrix (secondary/primary/2-node loss, rejoin, transient-expulsion autorejoin) and a write-path heartbeat alongside the read-only cluster-membership check. Added §2.3.1b: the self-healing/manual-intervention boundary, `group_replication_autorejoin_tries`, and OK/DEGRADED/CRITICAL diagnostics with a concrete `force_members` remediation value. Draft, not yet built. |
| v0.4 | 2026-09-10 | Paul Scott | Built and failure-tested for real. §2.7's first four open items resolved (bootstrap sequencing, seed self-reference, monitor credentials, status-script complexity, the last extended with a rolling history view). New open item: the bootstrap node has no permanent seed for its own rejoin after a restart (F-020) — fixed per-incident, not yet templated. |
| v0.5 | 2026-09-10 | Paul Scott | §2.3.1a test 3's expected outcome corrected after actually running it — Group Replication does not provide split-brain protection by default (F-021); §5.1's fault-tolerance argument for 3 nodes still holds, but the network-partition protection §5.3 claimed does not exist without an explicit fix. Added as a new, significant open item in §2.7. |
| v0.6 | 2026-09-10 | Paul Scott | F-021 fixed, not just flagged — `quorum-watchdog.py` added to §2.3.2's MySQL cloud-init, §2.3.1b's self-healing table and §2.7's open item both updated to reflect the real, verified fix (including a second bug found building it: `read_only` vs `super_read_only`). |
| v0.7 | 2026-09-10 | Paul Scott | Third slice — §3, Identity Tier (Keystone): 2-node active-active, shared MySQL backend, memcached Fernet cache, `admin:admin` bootstrap, Lab-substitution port-based routing instead of vhosts. Flagged `haFullStack.md` §7's memcached claim as unverified (likely the shared Fernet keys, not memcached, actually enable cross-node validation) rather than carrying it forward — gets its own failure-mode test. Draft, not yet built. |
| v0.8 | 2026-09-10 | Paul Scott | §3.3.1 corrected: guest-network DNS resolution of CloudCore-managed hostnames is now fixed (F-022), not a standing limitation — the port-based Keystone routing decision itself is unchanged, since it was never based on that limitation in the first place. |
| v0.9 | 2026-09-10 | Paul Scott | §3 built and failure-tested for real. §3.3.1a's test matrix filled in with actual results — test 1's "zero impact" expectation didn't hold (status correctly reads `DEGRADED` while one node is down, by design); tests 2-5 confirmed as expected, including the memcached question (F-027, `haFullStack.md` §7 corrected to v1.5). Two real deploy bugs found and fixed along the way (F-025, F-026). New open item: a restart-reachability gap on this platform, distinct from ICMP/SSH readiness (F-028). |
| v0.10 | 2026-09-10 | Paul Scott | F-028 corrected: not a CloudCore platform gap — confirmed via direct reproduction to be `mod_wsgi`'s own ~30s worker-startup latency, an application characteristic, not a bug. §3.3.1a and §3.7 updated to match; nothing to change in CloudCore for this. |
| v0.11 | 2026-09-10 | Paul Scott | Fourth slice — §4, Message Broker Tier (RabbitMQ): 3-node quorum-queue cluster (seed + 2 joiners, mirroring MySQL's bootstrap/joiner split, not Keystone's homogeneous pair), shared Erlang cookie via the same pattern as Keystone's Fernet keys, IP-based node naming by deliberate choice over the newly-available guest DNS. Flagged `haFullStack.md` §10's 2-node-loss claim as unverified (likely to repeat F-021's pattern) rather than carrying it forward — gets its own failure-mode test. Draft, not yet built. |
| v0.12 | 2026-09-10 | Paul Scott | §4 built and failure-tested for real. Two real deploy bugs found and fixed (F-029, F-030) beyond the two already flagged before building. §4.3.1a's test matrix filled in with actual results — test 2 gave the most nuanced result of any tier's "verify, don't assume" test so far: RabbitMQ's below-quorum protection is genuinely real (unlike MySQL's F-021), but the specific recovery procedure `haFullStack.md` §10 documented was wrong — recovery is fully automatic (F-031). Both other open items (recovery claim, restart auto-rejoin) resolved. `haFullStack.md` §10 corrected to v1.7. |
| v0.13 | 2026-09-11 | Paul Scott | Fifth slice — §5, TLS and Mutual TLS (cross-cutting, not a new tier): a single-node `step-ca`, TLS/mTLS across every east-west path already built (NGINX↔MySQL/ProxySQL/Keystone/RabbitMQ), client-facing TLS termination on NGINX, backend↔X mTLS fully specified but deferred until that tier exists. Corrected `haFullStack.md` §4.1's 90-day certificate claim before building, not after — confirmed directly against a real `step-ca` install that its actual default is 24h with built-in auto-renewal, not a limitation to work around. Full MySQL TLS + mTLS enforcement (reject-without-cert, accept-with-cert) verified directly before drafting this section. Draft, not yet built. |
| v0.14 | 2026-09-11 | Paul Scott | §5.1 updated: the Lab's CA now deliberately issues 365-day certs instead of `step-ca`'s unconfigured 24h default, to mirror realistic On-Prem/AWS PKI lifetimes ahead of building those slices — `authority.claims` set explicitly in `ca.json` before `step-ca`'s first start. Found and documented directly that `step ca renew` (unlike a fresh `step ca certificate` issuance) does **not** pick up a changed CA default on its own — it preserves the original cert's own requested duration unless `--expires-in` is passed — so a CA policy change needs every already-issued cert force-*reissued*, not just renewed, to actually take effect; every running node's certs were reissued this way and confirmed at their new 365-day expiry. §5.3.1a test 5 and §5.6's environment table updated to match. |
| v0.15 | 2026-09-11 | Paul Scott | New §6, Local Package Repository (cross-cutting) — built and verified for real directly, not drafted first: a real `dpkg-scanpackages`-indexed local apt repo plus a pinned-artifact cache, both NFS-served, eliminating the concurrent-rebuild mirror congestion behind F-037. Six real findings along the way (F-039–F-045, `haFullStack-Findings-Log.md`) — most notably a genuine provider bug (`cloudcore_nfs_server`'s `Create()` never waited for a populated `private_ip`, F-039, fixed and rebuilt) and cloud-init's own apt module silently discarding the NFS-repo `sources.list` rewrite (F-042). A node rebuilt with the full fix finished its entire package-install phase in under 6 minutes versus 22+ minutes stuck on the bootstrap step alone beforehand. All four dashboard checks confirmed `OK` twice in a row on the finished rebuild. |
| v0.16 | 2026-09-11 | Paul Scott | New §7, Host-Level Package Repository — §6's per-project NFS repo generalized into a host-level, always-available HTTP service (`cloudcore-repo.service`) shared by every project, not tracked as a CloudCore resource so no `tofu destroy` can reach it. Extended to cover every example template's package/artifact needs, including two third-party apt repos (Adoptium, Kismet) mirrored on the throwaway builder and pinned release artifacts (Ghidra, kiwix-tools, the Wikipedia ZIM, RTL8812AU driver source) cached alongside the existing `step-ca`/`step-cli`/`proxysql` set. F-040 resolved (§6.4 updated) — a real backend `PATCH` endpoint plus provider `Update()` support now handle in-place share-client changes. F-041's platform-level fix noted in §6.3. New F-046 (a `write_files`/`owner:` race in `api/nfs.py`, same class as F-026) and F-047 (a genuinely stalled, not merely slow, apt-mirror connection) logged in `haFullStack-Findings-Log.md`. Protected against accidental removal: `teardown-network.sh` requires `--force` while the service is active; its build output survives `git clean -xfd` via a tracked README marker. Not yet consumed by `ha-frontend-lb` itself. |
| v0.17 | 2026-09-12 | Paul Scott | `ha-frontend-lb` retrofitted onto §7's host-level `cloudcore-repo` — §6's own NFS design marked superseded for this stack (kept as a design reference only). `module.nfs`/`module.repo_builder` and every tier's NFS-mount `bootcmd` block removed, 15 nodes instead of 17. Verified with a real `tofu apply`/dashboard-check/`tofu destroy` cycle from a clean slate — see §7.4. |
