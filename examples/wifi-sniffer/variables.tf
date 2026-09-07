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
