output "chat_url" {
  description = "Open this in a real browser (not curl — llama-server's built-in Web UI serves gzip-encoded assets, which curl doesn't request by default) to chat with the model directly. The same address also serves the OpenAI-compatible API (POST /v1/chat/completions) for any other client."
  value       = "http://127.0.0.1:${var.http_port}/"
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
