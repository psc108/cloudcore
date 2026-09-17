variable "project" {
  description = "Project name used in resource naming and tags."
  type        = string
  default     = "example"
}

variable "environment" {
  description = "Environment name used in resource naming and tags."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Owner tag value applied to all resources."
  type        = string
  default     = "platform-team"
}

variable "suffix" {
  description = "Optional suffix appended to resource names to keep them unique across runs."
  type        = string
  default     = ""
}

variable "cidr_block" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "instance_flavor" {
  description = "Compute flavor for the sniffer instance. Kismet + the aircrack-ng suite are light; standard.medium is comfortable headroom for the DKMS driver build and packet processing."
  type        = string
  default     = "standard.medium"
}

variable "admin_cidr" {
  description = <<-EOT
    CIDR allowed to reach SSH (22) and the dashboard (lb_port) on the
    security group. Defaults open (0.0.0.0/0) so this template can be
    deployed by users with no networking background without being
    blocked on a value they don't know how to produce. Safe to leave
    as-is on CloudCore specifically — the security group isn't the real
    access boundary here; every port binds to 127.0.0.1 on the CloudCore
    host itself and is never reachable over a network regardless of this
    CIDR. Override it if you're deploying somewhere that property
    doesn't hold, or want defense-in-depth anyway.
  EOT
  type        = string
  default     = "0.0.0.0/0"

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0))
    error_message = "admin_cidr must be a valid CIDR, e.g. \"203.0.113.4/32\"."
  }
}

variable "lb_port" {
  description = "Host port the dashboard is served on. Defaults to 8700, chosen clear of every other port range this platform auto-allocates or the other example templates default to (Ghidra 8600, Kiwix's auto-allocated 8200-8299, SSH 12200-12299, HTTP hostfwd 12800-12899, NFS SSH 12300-12399). Change it if that collides with something else on your host."
  type        = number
  default     = 8700
}

variable "usb_device_id" {
  description = <<-EOT
    The WiFi adapter's host USB device ID ("vendor_id:product_id"), from
    the cloudcore_usb_devices data source — e.g. run
    `tofu console` and evaluate `data.cloudcore_usb_devices.this.items`,
    or `curl http://<cloudcore-host>/v1/usb-devices`, to find it once the
    adapter is plugged in. No default is possible here — there's no
    generic "the WiFi adapter" to fall back to the way Ghidra pins a
    Ghidra release or Kiwix pins a ZIM file; this template needs your
    specific physical hardware's ID.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$", var.usb_device_id))
    error_message = "usb_device_id must look like \"vendor_id:product_id\", e.g. \"0bda:8812\"."
  }
}

variable "rtl8812au_driver_ref" {
  description = "Git tag/branch of the aircrack-ng/rtl8812au driver fork to build (the only fork with working monitor mode + injection for this chipset — the commonly-suggested morrownr fork explicitly does not support monitor mode). Pinned to a known-good release; override to track a newer one."
  type        = string
  default     = "v5.6.4.2"
}

variable "rtl8812au_driver_sha256" {
  description = "SHA-256 checksum of the rtl8812au_driver_ref source tree, tarred by `api/build-package-repo.sh` into its own pinned-artifact cache (haFullStack.md §14) at rtl8812au-<ref>.tar.gz — verified against that tarball before it's unpacked. Update alongside rtl8812au_driver_ref when bumping the version."
  type        = string
  default     = "114f0334f08612652e0d9f5dbf38359d894db26c75f210667cdec1800f805cec"
}

# Leave blank to keep the sniffer instance local (the default -- unchanged
# behavior). To place it on a paired remote host instead, set peer_id
# (see the Dashboard's Peers section, or the cloudcore_peers data
# source, for available hosts) AND peer_vpc_id/peer_subnet_id to THAT
# peer's own vpc_id/subnet_id. Those two aren't optional once peer_id
# is set: a remote peer has its own separate VPC/subnet catalogue, not
# this build's local one (see haFullStack-LLD.md §13).
variable "peer_id" {
  description = "ID of a paired remote peer to place the sniffer instance on instead of the local host. Requires peer_vpc_id/peer_subnet_id/peer_security_group_id to also be set."
  type        = string
  default     = ""
}

variable "peer_vpc_id" {
  description = "The peer's own VPC ID for the sniffer instance — only meaningful when peer_id is set."
  type        = string
  default     = ""
}

variable "peer_subnet_id" {
  description = "The peer's own subnet ID for the sniffer instance — only meaningful when peer_id is set."
  type        = string
  default     = ""
}

# Same reasoning as peer_vpc_id/subnet_id: a peer's own security group
# catalogue is separate from this build's local one, and the API now
# rejects a security_group_ids entry it can't resolve locally at
# instance-create time (previously an unresolved id was silently
# accepted and produced a DROP-only iptables chain, leaving the
# instance completely unreachable with no error anywhere — see
# haFullStack-Findings-Log.md). Required whenever peer_id is set.
variable "peer_security_group_id" {
  description = "The peer's own security group ID for the sniffer instance — required when peer_id is set."
  type        = string
  default     = ""
}
