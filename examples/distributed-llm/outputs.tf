output "inference_endpoint" {
  description = "OpenAI-compatible HTTP API — POST /v1/chat/completions or /completion. curl it directly to confirm the coordinator is actually serving, or point any OpenAI-client-compatible tool at it."
  value       = "http://127.0.0.1:${var.http_port}"
}

output "coordinator_ssh" {
  description = "SSH command for the coordinator instance."
  value       = module.coordinator.ssh_commands_by_key["01"]
}

output "worker_hosts" {
  description = "Which physical peer each worker actually landed on, keyed by two-digit index."
  value       = module.workers.host_hostnames_by_key
}

output "worker_private_ips" {
  description = "Each worker's own private IP, keyed by two-digit index — the same addresses baked into the coordinator's own --rpc argument."
  value       = module.workers.private_ips_by_key
}
