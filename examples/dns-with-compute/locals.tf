locals {
  sfx     = var.suffix != "" ? "-${var.suffix}" : ""
  vpc_key = "main${local.sfx}"

  # Same fixed host-level Loki address every other example points at
  # (haFullStack-LLD.md §12.3) — see examples/ha-frontend-lb/files/
  # promtail-config.yml for the full content/reasoning; copied here
  # verbatim rather than referenced cross-module, matching this
  # project's own each-example-is-self-contained convention.
  promtail_config = file("${path.module}/files/promtail-config.yml")

  # This example has no application of its own to install — promtail
  # is the only thing this cloud-init does, so it's a plain heredoc
  # rather than a separate files/cloud-init.yaml.tftpl.
  promtail_user_data = <<-EOT
    #cloud-config
    # apt_preserve_sources_list + bootcmd point this guest at the
    # host-level package repo (api/build-package-repo.sh,
    # haFullStack.md §14) exclusively, the same pattern every other
    # example uses — promtail is only ever cached there, not in the
    # real Ubuntu archive.
    apt_preserve_sources_list: true
    bootcmd:
      - |
        cat > /etc/apt/sources.list <<'EOF'
        deb [trusted=yes] http://192.168.100.1:8090/jammy/apt-repo ./
        EOF

    package_update: true
    packages:
      - promtail

    write_files:
      # Centralized logging (haFullStack-LLD.md §12) — ships this
      # node's own journal + cloud-init-output.log to the host-level
      # Loki service. __HOSTNAME__ fixed up in runcmd below, same
      # sed-substitution idiom this platform already uses elsewhere.
      - path: /etc/promtail/config.yml
        content: |
          ${indent(10, local.promtail_config)}

    runcmd:
      # F-073/F-072 promtail fix (F-076, haFullStack-Findings-Log.md —
      # promtail's own package postinst auto-starts it immediately
      # once installed, before this runcmd block gets a chance to fix
      # up __HOSTNAME__ or grant the "adm" group needed to read
      # root:adm 0640 /var/log/cloud-init-output.log).
      - systemctl stop promtail || true
      - sed "s/__HOSTNAME__/$(hostname)/" -i /etc/promtail/config.yml
      - usermod -aG adm promtail
      - systemctl enable --now promtail
  EOT
}
