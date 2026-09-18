output "chat_url" {
  description = "Open this in a real browser (not curl — the sandbox page needs a real browser to run its own JS, and llama-server's own /health check the LB uses serves gzip-encoded responses curl doesn't request by default) to reach the Interactive Sandbox: a code editor plus a grounded Run/Ask loop, per llm-chat-interactive-sandbox-Phased-Implementation.md's own Phase 4. Named chat_url for historical reasons — as of Phase 4 this is the sandbox, not a general chat webui; the general chat webui is no longer reachable at all, and neither is a raw /v1/chat/completions (both were closed off deliberately — see that document's own Stage 1)."
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
