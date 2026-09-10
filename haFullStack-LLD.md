# NGINX High-Availability Load Balancing Architecture — Low-Level Design

**Multi-Service Platform — Frontend, Backend, MySQL, Keystone, RabbitMQ**

v0.1 (in progress — built section by section) | Paul Scott

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
| 3 | Identity Tier — Keystone | Draft, under review |

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

**A claim in `haFullStack.md` §7 to verify, not assume:** it says
memcached "is what allows either Keystone instance to validate a token
issued by the other." That's very likely imprecise. Fernet tokens are
self-describing bearer tokens — any Keystone node holding the *same
Fernet key material* can decrypt and validate a token issued by any
other node holding it, with zero memcached involvement. memcached's
actual role is caching validation results (performance) and propagating
the revocation event list quickly — not the mechanism that makes
cross-node validation possible at all. The real enabler is **shared
Fernet keys**, which §7 doesn't mention needing to distribute at all.
This gets its own failure-mode test (§3.3.1a test 2) rather than being
assumed either way — the same discipline that caught F-021.

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
  workaround for missing DNS** — worth being precise about, since Lab
  guest-network DNS genuinely doesn't reach vhost-routing usability
  (CloudCore's own DNS server is confirmed working — F-010–F-014 — but
  host-loopback-only, unreachable from bridge-network guests; that's a
  real, separate limitation, just not the reason for this particular
  decision). More detail on the real application's addressing scheme is
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

| # | Test | What it proves | Expected result |
|---|---|---|---|
| 1 | Stop one Keystone node | Active-active genuinely means zero failover delay, not just "a fast failover" | Zero impact — the surviving node keeps answering immediately, no election/promotion step exists to wait on (unlike §2's MySQL primary failover, which has a real, measured RTO) |
| 2 | Get a token from node A, stop node A, validate that token against node B | Whether cross-node validation is really enabled by shared Fernet keys (as this LLD argues) or genuinely depends on memcached (as `haFullStack.md` §7 claims) | Token validates successfully via node B — if this fails, the claim in §3.1 was wrong instead and memcached (or something else) actually matters here; either outcome is real information, not assumed |
| 3 | Stop one memcached node | memcached is a performance/revocation cache, not required for basic Fernet validation (per §3.1's claim, being tested here too) | Token issuance and validation keep working — slower, or with more redundant crypto work, but not broken |
| 4 | Stop both memcached nodes | Same as test 3, pushed further — is memcached ever a hard dependency for basic auth, or only for revocation-list propagation | Basic token issuance/validation still works; a revoked-token check may not propagate as fast without memcached available, but that's a different claim than "auth is down" |
| 5 | Stop both Keystone nodes | Genuine identity-tier outage — the one failure mode with no redundancy left to test | `keystone-status.html` correctly shows `CRITICAL`, distinct from `DEGRADED` |

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
- **Memcached's actual role — resolve via test 2/3/4, don't assume
  either the original doc or this LLD's counter-claim.** `haFullStack.md`
  §7 will need correcting one way or the other once this is tested for
  real, the same way §5.3 was corrected by F-021.
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
