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
  default     = "10.30.0.0/16"
}

variable "instance_flavor" {
  description = "Compute flavor for the Kiwix instance. kiwix-serve is light (static binary, disk-I/O bound) — standard.medium is more than enough; only go bigger for a much larger ZIM library than the default."
  type        = string
  default     = "standard.medium"
}

variable "admin_cidr" {
  description = <<-EOT
    CIDR allowed to reach SSH (22) and HTTP (80) on the security group.
    Defaults open (0.0.0.0/0) so this template can be deployed by users
    with no networking background without being blocked on a value they
    don't know how to produce. Safe to leave as-is on CloudCore
    specifically: the security group isn't the real access boundary here
    — every port binds to 127.0.0.1 on the CloudCore host itself and is
    never reachable over a network regardless of this CIDR. Override it
    if you're deploying somewhere that property doesn't hold, or want
    defense-in-depth anyway.
  EOT
  type        = string
  default     = "0.0.0.0/0"

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0))
    error_message = "admin_cidr must be a valid CIDR, e.g. \"203.0.113.4/32\"."
  }
}

variable "kiwix_tools_version" {
  description = "kiwix-tools release version to install (provides kiwix-serve)."
  type        = string
  default     = "3.8.2"
}

variable "kiwix_tools_url" {
  description = "Download URL for the kiwix-tools Linux x86_64 release tarball above."
  type        = string
  default     = "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_linux-x86_64-3.8.2.tar.gz"
}

variable "kiwix_tools_sha256" {
  description = "SHA-256 checksum of kiwix_tools_url's contents. Kiwix's own download server only publishes MD5 (matched against that at build time: 917ea9632a7a7fca946b63b2378579f3) — this SHA-256 was computed directly from that verified download as a stronger independent integrity pin. Update all three kiwix_tools_* variables together when bumping the version."
  type        = string
  default     = "b0ae98dd344aa0469a15ab42feff6d5aafb79541a82fb4e2647c74b073123815"
}

variable "zim_url" {
  description = <<-EOT
    Download URL for the ZIM content archive to serve. Defaults to
    English Wikipedia's "top" (most significant) articles, text-only
    (~2.2 GB) — the full-image version of the same set is 8+ GB, too much
    for a template default. Point this at any other archive from
    https://library.kiwix.org (Wiktionary, Project Gutenberg, Stack
    Exchange, other languages, ...) to serve something else instead —
    update zim_filename and zim_md5 to match.
  EOT
  type        = string
  default     = "https://download.kiwix.org/zim/wikipedia/wikipedia_en_top_nopic_2026-06.zim"
}

variable "zim_filename" {
  description = "Filename zim_url is saved as and served from. Must match the actual file at that URL."
  type        = string
  default     = "wikipedia_en_top_nopic_2026-06.zim"
}

variable "zim_md5" {
  description = "MD5 checksum of zim_url's contents, as published alongside it at <zim_url>.md5 — this is what Kiwix's own distribution publishes for ZIM files (no SHA-256 option), verified before kiwix-serve starts."
  type        = string
  default     = "e3d9f0cf29462733508062fe45b91ff9"
}
