terraform {
  required_version = ">= 1.8.0"
  required_providers {
    cloudcore = {
      source  = "registry.opentofu.org/cloudcore/cloudcore"
      version = ">= 0.1.0"
    }
  }
}

provider "cloudcore" {}