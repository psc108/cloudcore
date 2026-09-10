# HA Frontend Load Balancer (Lab)

Two NGINX nodes running Keepalived in front of a pool of frontend web
instances, sharing a single floating virtual IP (VIP). This is Phase 1.A
of `haFullStack-Phased-Implementation.md` — the Lab/OpenTofu build path
for the load-balancer tier described in `haFullStack-LLD.md` §1.

## What it creates

VPC + subnet + two security groups (`nginx` — client HTTP/SSH/VRRP,
`frontend` — HTTP from the LB nodes only, plus SSH) + a 2-instance
frontend pool (`modules/instance-group`) + two NGINX/Keepalived nodes
(`modules/compute`, one `MASTER` and one `BACKUP`).

NGINX and Keepalived are self-managed on plain instances rather than
using CloudCore's own native load-balancer resource — this pattern is
meant to carry over unchanged to On-Prem and AWS builds of the same LLD
section, where there's no equivalent platform LB to lean on.

## How failover works

Both NGINX nodes run an identical config, differing only in Keepalived's
`state`/`priority`. Keepalived uses multicast VRRP (the default) — this
was empirically validated against this platform's bridged network
(`ccbr0`) before the template was written: the VIP correctly failed over
from the `MASTER` to the `BACKUP` node within ~40 s of stopping
`keepalived` on the master, with no `unicast_peer` configuration needed.
That's specific to CloudCore's own bridge being a genuine L2 segment —
not necessarily true of a cloud VPC's SDN, which is why the on-prem/AWS
phases of this same LLD section may need a different Keepalived mode.

## The private_ip timing problem this template depends on

NGINX's `upstream frontend {}` block needs the frontend instances' real
IPs baked into its config at cloud-init time — but bridged instances only
get a real IP once DHCP completes, some seconds *after* libvirt reports
them `running`. Two platform fixes (made alongside this template) make
that work:

- `api/compute.py`'s `get_instance_ip()` used to fall back to the SLIRP
  placeholder `10.0.2.15` for a bridged instance queried before its DHCP
  lease existed — a *truthy* value that permanently defeated the retry
  used elsewhere. It now returns `""` in that case, matching the "unknown
  yet" convention the rest of the codebase already expects.
- `provider/internal/resources/instance.go`'s `Create()` used to stop
  polling the instant `status == "running"` — often before DHCP had
  completed, baking an empty `private_ip` into Terraform state. It now
  waits for both `status == "running"` **and** a non-empty `private_ip`.

Together these mean `module.frontend.private_ips_by_key` is a real,
populated value by the time `module.nginx`'s `user_data` templates
reference it — no lease-file reads, `local-exec`, or extra API surface
needed. `main.tf`/`locals.tf` rely on this ordering implicitly: NGINX's
`user_data` interpolates the frontend module's output, so OpenTofu builds
the frontend tier first.

## Usage

```bash
tofu init
tofu apply

tofu output vip_address
tofu output lb_url
curl "$(tofu output -raw lb_url)"

# Failover test — from the CloudCore host:
#   ssh into the current MASTER (tofu output nginx_private_ips), then
#   sudo systemctl stop keepalived
# ... and confirm the VIP (and curl above) keep working via the BACKUP.

tofu destroy
```

## Known open items

Carried over from `haFullStack-LLD.md` §1.7 — not blocking for Lab, but
relevant before porting this same phase to Ansible/On-Prem/AWS:

- The Keepalived auth password (`local.vrrp_auth_pass` in `locals.tf`) is
  a lab-only placeholder, not meant to be reused as-is.
- The VRRP security-group rule uses protocol `-1` (all traffic) scoped to
  the Lab bridge subnet, since this platform's security-group model
  doesn't expose IP protocol 112 individually — see `main.tf`.
- `var.vip_address` must be picked manually from outside the bridge's
  DHCP range (`192.168.100.10`-`192.168.100.254`); there's no reservation
  mechanism yet to guarantee it stays free.
