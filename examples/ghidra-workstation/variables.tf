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
  default     = "10.20.0.0/16"
}

variable "instance_flavor" {
  description = "Compute flavor for the Ghidra workstation. standard.large (4 vCPU / 4 GB) is the largest flavor CloudCore currently offers and is recommended — XFCE + a JVM decompiler is not light."
  type        = string
  default     = "standard.large"
}

variable "admin_cidr" {
  description = <<-EOT
    CIDR allowed to reach SSH (22) and the desktop (lb_port) on the security
    group. Defaults open (0.0.0.0/0) so this template can be deployed by
    users with no networking background (e.g. via a self-service build
    catalog) without being blocked on a value they don't know how to
    produce. This is safe specifically on CloudCore: the security group
    isn't what actually gates access — every port here (SSH, the LB) binds
    to 127.0.0.1 on the CloudCore host itself and is never reachable over
    a network regardless of this CIDR. If you're deploying somewhere that
    property doesn't hold, or want defense-in-depth anyway, override this
    with your own IP (e.g. "203.0.113.4/32").
  EOT
  type        = string
  default     = "0.0.0.0/0"

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0))
    error_message = "admin_cidr must be a valid CIDR, e.g. \"203.0.113.4/32\"."
  }
}

variable "lb_port" {
  description = <<-EOT
    Host port the load balancer listens on (loopback-only, same access
    model as everything else in CloudCore — reachable from 127.0.0.1 on
    the CloudCore host itself). Open http://127.0.0.1:<lb_port>/vnc.html
    directly in a browser once the instance has finished provisioning.
    Chosen outside every other port range this platform auto-allocates
    (SSH 12200-12299, HTTP hostfwd 12800-12899, NFS SSH 12300-12399,
    LB auto-allocation 8200-8299) — change it if it collides with
    something else already running on your host.
  EOT
  type        = number
  default     = 8600
}

variable "vnc_password" {
  description = <<-EOT
    Password for the VNC/noVNC session. Classic VNC auth (used by TigerVNC)
    only honours the first 8 characters — anything beyond that is silently
    ignored, so treat this as an 8-character secret, not a full passphrase.
    No default: you must supply one (e.g. via TF_VAR_vnc_password or a
    .auto.tfvars file that is gitignored).
  EOT
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.vnc_password) >= 6
    error_message = "vnc_password must be at least 6 characters."
  }
}

variable "ghidra_version" {
  description = "Ghidra release version to install (used to build the extracted install path)."
  type        = string
  default     = "12.1.3"
}

variable "ghidra_release_tag" {
  description = "GitHub release tag for the Ghidra version above."
  type        = string
  default     = "Ghidra_12.1.3_build"
}

variable "ghidra_zip_name" {
  description = "Exact release asset filename for the Ghidra version above."
  type        = string
  default     = "ghidra_12.1.3_PUBLIC_20260817.zip"
}

variable "ghidra_sha256" {
  description = "SHA-256 checksum of ghidra_zip_name, published in the GitHub release notes. Verified against the actual asset before it is unzipped — do not change this without changing ghidra_zip_name to match."
  type        = string
  default     = "93a5d11a9ad510622acaaf908c556a7b9b764d338e78a7567f3689bf5081fd54"
}