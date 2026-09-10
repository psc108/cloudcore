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

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-09-10 | Paul Scott | First slice — Load Balancer Tier, client-to-frontend, all three environments. |
| v0.2 | 2026-09-10 | Paul Scott | Phase 1.A (Lab/OpenTofu) built and verified against real infrastructure; §1.3.1/§1.3.2 updated with as-built notes; §1.7 open items resolved for Lab; linked to new `haFullStack-Findings-Log.md`. |
