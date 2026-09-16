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
  # is the only thing this cloud-init does. Rendered via
  # templatefile(), not an inline heredoc: an earlier version used
  # `<<-EOT ... ${indent(N, local.promtail_config)} ... EOT` directly,
  # which corrupted the embedded YAML's indentation — HCL heredocs'
  # own `<<-` dedent (based on the closing marker's indentation)
  # strips a uniform amount of leading whitespace from every line
  # *after* indent() has already given line 1 zero and every other
  # line N spaces, so the two don't compose correctly. Confirmed live
  # (F-080, haFullStack-Findings-Log.md): promtail crash-looped
  # (`yaml: line 4: did not find expected key`) on a real instance —
  # `tofu validate` never catches this, since it only checks HCL
  # syntax, not the interpolated YAML it produces. templatefile()
  # against a real file doesn't have this interaction at all — same
  # reliable pattern already used everywhere else in this repo.
  promtail_user_data = templatefile("${path.module}/files/promtail-cloud-init.yaml.tftpl", {
    promtail_config = local.promtail_config
  })
}
