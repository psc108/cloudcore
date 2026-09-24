#!/usr/bin/env bash
# Populates the host-level package repo (api/package-repo/<codename>/) —
# run by hand, on a deliberate cadence: when the target Ubuntu release
# increments, or when a specific package needs a security patch. Not a
# continuously-reconciled mirror (haFullStack-LLD.md §6.1) — nothing
# calls this automatically.
#
# The apt-repo build needs a real matching-release environment (the
# CloudCore host itself may be on a different Ubuntu release than the
# guest images it serves), so it launches a throwaway CloudCore instance
# of the target release, builds the repo there, pulls the result back
# over SSH, and tears the instance down again — the repo *serving* is
# host-level and always-on (serve-package-repo.py), but *building* it
# still needs a same-release guest for correctness, same as every other
# CloudCore VM this platform creates.
#
# Covers every current example template's package/artifact needs, not
# just ha-frontend-lb (haFullStack-Findings-Log.md F-039-041 cleanup led
# to this repo going host-level/always-on; this pass extends its
# coverage to ghidra-workstation, kiwix-library, wifi-sniffer, full-stack
# and load-balanced-web too, since none of those have any bandwidth-
# saving mechanism of their own).
#
# Usage: api/build-package-repo.sh [codename] [package...]
#   codename   Ubuntu release codename — must match a CloudCore image_id
#              of "ubuntu-<version>" (default: jammy -> ubuntu-22.04)
#   package... top-level packages to include (their own dependencies are
#              resolved automatically) — default: the union of every
#              current example template's package list
set -euo pipefail

CODENAME="${1:-jammy}"
shift || true
PACKAGES=("$@")
if [ ${#PACKAGES[@]} -eq 0 ]; then
  PACKAGES=(
    # ha-frontend-lb
    curl ca-certificates dpkg-dev mysql-server mysql-client nginx keepalived \
    keystone python3-pymysql python3-memcache python3 rabbitmq-server \
    python3-requests python3-yaml python3-openstackclient nfs-common \
    grafana loki promtail \
    # full-stack, load-balanced-web (nginx already listed above)
    # ghidra-workstation
    xfce4 xfce4-terminal tigervnc-standalone-server tigervnc-common novnc websockify unzip gnupg \
    # kiwix-library (curl/ca-certificates already listed above)
    # wifi-sniffer
    build-essential dkms bc libelf-dev git aircrack-ng hcxtools hcxdumptool tcpdump tshark \
    # llm-chat Stage 5 — sandbox_terminal.py's own WS<->SSH bridge needs
    # paramiko (real Ubuntu 22.04 archive package, confirmed working
    # live). websockets is deliberately NOT installed from apt here —
    # jammy's own python3-websockets (9.1-1) is confirmed BROKEN on
    # jammy's own current Python 3.10.12: it calls asyncio.Lock(loop=...),
    # a parameter Python 3.10 removed outright, so every single WS
    # connection crashes with TypeError before this was caught live.
    # A newer version is vendored instead via ARTIFACT_URLS below,
    # matching the pinned-artifact convention already used throughout
    # this file — see firecracker_archive_name's own comment for the
    # same reasoning applied to a different broken-apt-package problem.
    # dnsmasq is the coordinator's own DHCP+DNS server for the Firecracker
    # sandbox subnet, same tool setup-network.sh already uses for ccbr0.
    python3-paramiko dnsmasq
  )
fi

# Packages that only exist in a third-party apt repo, not Ubuntu's own
# archive — the builder adds all three unconditionally before the
# install step below (harmless on a throwaway instance even for a build
# that doesn't strictly need them) rather than threading a per-template
# opt-in through this script. nodejs: Ubuntu 22.04's own archive ships a
# years-old 12.x, nowhere near ha-frontend-lb's frontend tier's stated
# "18.x minimum, prefer latest" — NodeSource's repo is the standard
# current-version source, same pattern as Adoptium/Kismet below.
# grafana/loki/promtail: Grafana Labs' own apt repo hosts all three —
# none exist in Ubuntu's own archive.
THIRDPARTY_PACKAGES=(temurin-21-jdk kismet nodejs grafana loki promtail)

case "$CODENAME" in
  jammy) IMAGE_ID="ubuntu-22.04" ;;
  noble) IMAGE_ID="ubuntu-24.04" ;;
  *) echo "Unknown codename '$CODENAME' — add it to the case statement in this script first" >&2; exit 1 ;;
esac

# Defaults match every other doc/script in this repo (README, the in-app
# Help articles, setup-package-repo.sh's own printed instructions) —
# override either by exporting the env var first if your CloudCore
# instance uses a different API URL or token.
CLOUDCORE_API_URL="${CLOUDCORE_API_URL:-http://127.0.0.1:8080}"
CLOUDCORE_API_TOKEN="${CLOUDCORE_API_TOKEN:-dev-token}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY="$SCRIPT_DIR/keys/cloudcore_ed25519"
REPO_DIR="$SCRIPT_DIR/package-repo/$CODENAME"
API="$CLOUDCORE_API_URL"
AUTH=(-H "Authorization: Bearer $CLOUDCORE_API_TOKEN")
# UserKnownHostsFile=/dev/null, not just StrictHostKeyChecking=no --
# see api/build-firecracker-rootfs.sh's own matching comment: this
# lab's small throwaway-instance IP pool recycles addresses, so a
# later builder run can land on an IP a previous run already recorded
# a *different* host key for, which ssh refuses outright regardless of
# StrictHostKeyChecking=no (that only skips the prompt for a genuinely
# new host). Same class of throwaway host, same fix.
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -i "$KEY")
SUFFIX="cloudcore-repo-builder-$(date +%s)"

mkdir -p "$REPO_DIR/apt-repo" "$REPO_DIR/artifacts"

cleanup() {
  echo "Cleaning up throwaway build infrastructure..."
  # Deletion order matters — delete_vpc 409s if the VPC still has any
  # active instance/security-group/subnet attached (confirmed directly:
  # a first version of this script left orphaned VPCs behind because it
  # only deleted the instance and VPC, skipping the security group and
  # subnet in between).
  [ -n "${INSTANCE_ID:-}" ] && curl -s -X DELETE "$API/v1/instances/$INSTANCE_ID" "${AUTH[@]}" >/dev/null || true
  sleep 5
  [ -n "${SG_ID:-}" ] && curl -s -X DELETE "$API/v1/security-groups/$SG_ID" "${AUTH[@]}" >/dev/null || true
  [ -n "${SUBNET_ID:-}" ] && curl -s -X DELETE "$API/v1/subnets/$SUBNET_ID" "${AUTH[@]}" >/dev/null || true
  [ -n "${VPC_ID:-}" ] && curl -s -X DELETE "$API/v1/vpcs/$VPC_ID" "${AUTH[@]}" >/dev/null || true
}
trap cleanup EXIT

echo "=== Fetching pinned artifacts directly (no VM needed, OS-version-agnostic) ==="
# Exact values transcribed from each template's own variables.tf — keep
# these in sync by hand when a template bumps its pinned version; each
# guest's own cloud-init still verifies its checksum independently at
# boot, this cache is purely a bandwidth shortcut, not a trust boundary.
declare -A ARTIFACT_URLS=(
  [step-ca.deb]="https://github.com/smallstep/certificates/releases/download/v0.30.2/step-ca_0.30.2-1_amd64.deb"
  [step-cli.deb]="https://github.com/smallstep/cli/releases/download/v0.30.6/step-cli_0.30.6-1_amd64.deb"
  [proxysql.deb]="https://github.com/sysown/proxysql/releases/download/v3.0.11/proxysql_3.0.11-ubuntu22_amd64.deb"
  # ghidra-workstation
  [ghidra.zip]="https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.3_build/ghidra_12.1.3_PUBLIC_20260817.zip"
  # ha-frontend-lb — frontend/backend: jasypt (CLI dist with its own
  # bin/*.sh scripts, not just a plain jar — Maven Central only has the
  # bare jasypt.jar, this is the project's own GitHub release asset) and
  # Bouncy Castle's core JCE provider jar, for use as an extra crypto
  # provider alongside it. Both verified as real, current, working URLs
  # directly (jasypt's project archived Nov 2025 — 1.9.3 is its last and
  # latest release; Bouncy Castle 1.80 confirmed latest on Maven Central).
  [jasypt-dist.zip]="https://github.com/jasypt/jasypt/releases/download/jasypt-1.9.3/jasypt-1.9.3-dist.zip"
  [bcprov.jar]="https://repo1.maven.org/maven2/org/bouncycastle/bcprov-jdk18on/1.80/bcprov-jdk18on-1.80.jar"
  # kiwix-library
  [kiwix-tools.tar.gz]="https://download.kiwix.org/release/kiwix-tools/kiwix-tools_linux-x86_64-3.8.2.tar.gz"
  # kiwix-library — the 2.2GB "top articles, no pictures" ZIM (variables.tf
  # notes the full-image variant is 8+GB and deliberately not used here).
  # This one dwarfs every other artifact in this list; skip it with
  # SKIP_ZIM=1 if disk space is a concern — everything else still builds.
  [wikipedia_en_top_nopic_2026-06.zim]="https://download.kiwix.org/zim/wikipedia/wikipedia_en_top_nopic_2026-06.zim"
  # llm-chat's own retrieval-grounding backend (haFullStack-Findings-Log.md
  # F-132) -- three real reference corpora kiwix-serve loads together,
  # queried by verify_proxy.py's own _kiwix_search() before answering a
  # Linux Help question. Full Wikipedia (nopic, ~49GB -- deliberately
  # NOT the smaller "mini" flavour, which drops to abridged/summary
  # article text; the whole point is depth, and _details:yes vs
  # _details:no is a real, meaningful difference here), ManKier's own
  # Linux man-page mirror (~190MB), and ArchWiki (~36MB). All three
  # covered by the same SKIP_ZIM guard below as the top-articles ZIM
  # above -- this list adds ~49GB total.
  [wikipedia_en_all_nopic_2026-06.zim]="https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_nopic_2026-06.zim"
  [www.mankier.com_en_all_2026-07.zim]="https://download.kiwix.org/zim/zimit/www.mankier.com_en_all_2026-07.zim"
  [archlinux_en_all_maxi_2026-07.zim]="https://download.kiwix.org/zim/other/archlinux_en_all_maxi_2026-07.zim"
  # llm-chat's own coding Ask panel, direct follow-up to F-132/F-136/F-137
  # ("are there any other downloadable resources for grounding... in
  # the python field"): official Python documentation plus a curated
  # DevDocs bundle (DevDocs mirrors each project's own official docs
  # verbatim, not third-party writeups) covering the libraries most
  # likely to come up in a Python coding sandbox. Deliberately NOT the
  # much larger corey Schafer tutorial-video ZIMs also in the Kiwix
  # catalog (thin text content relative to their size, a poor fit for
  # full-text search grounding regardless of the presenter's own
  # reputation), and NOT the whole 231-entry DevDocs catalog (the
  # sandbox only ever executes Python, so non-Python language docs
  # wouldn't be used) -- covered by the same SKIP_ZIM guard below as
  # every other ZIM in this list, adds ~4.2GB total, ~4GB of that being
  # the official docs.python.org mirror alone.
  [docs.python.org_en_all_2026-08a.zim]="https://download.kiwix.org/zim/zimit/docs.python.org_en_all_2026-08a.zim"
  [peps.python_en_all_2026-08.zim]="https://download.kiwix.org/zim/zimit/peps.python_en_all_2026-08.zim"
  [devdocs_en_python_2026-08.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_python_2026-08.zim"
  [devdocs_en_numpy_2026-07.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_numpy_2026-07.zim"
  [devdocs_en_pandas_2026-07.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_pandas_2026-07.zim"
  [devdocs_en_django_2026-07.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_django_2026-07.zim"
  [devdocs_en_flask_2026-07.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_flask_2026-07.zim"
  [devdocs_en_fastapi_2026-04.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_fastapi_2026-04.zim"
  [devdocs_en_matplotlib_2026-07.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_matplotlib_2026-07.zim"
  [devdocs_en_scikit-learn_2026-04.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_scikit-learn_2026-04.zim"
  [devdocs_en_requests_2026-07.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_requests_2026-07.zim"
  [devdocs_en_jinja_2026-07.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_jinja_2026-07.zim"
  [devdocs_en_click_2026-04.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_click_2026-04.zim"
  [devdocs_en_pygame_2026-07.zim]="https://download.kiwix.org/zim/devdocs/devdocs_en_pygame_2026-07.zim"
  # distributed-llm — the llama.cpp CPU build (contains both llama-server,
  # the coordinator's own OpenAI-compatible HTTP server, and
  # ggml-rpc-server, the worker binary — one archive covers both roles).
  # No checksums file is published for this asset; llama_sha256 was
  # computed directly against the real downloaded archive instead.
  [llama-b11025-bin-ubuntu-x64.tar.gz]="https://github.com/ggml-org/llama.cpp/releases/download/b11025/llama-b11025-bin-ubuntu-x64.tar.gz"
  # distributed-llm — the GGUF model itself, ~4.37GB (Q4_K_M). Dwarfs
  # every other artifact here except the ZIM above; skip it with
  # SKIP_LLM_MODEL=1 if disk space is a concern — everything else still
  # builds. model_sha256 is Hugging Face's own X-Linked-ETag for this
  # exact LFS object (its authoritative server-side content hash), not
  # self-computed — confirmed directly via a HEAD request before use.
  [Mistral-7B-Instruct-v0.3-Q4_K_M.gguf]="https://huggingface.co/bartowski/Mistral-7B-Instruct-v0.3-GGUF/resolve/main/Mistral-7B-Instruct-v0.3-Q4_K_M.gguf"
  # llm-chat/distributed-llm — the higher-precision variant of the same
  # already-trusted bartowski quantization (same repo, same uploader —
  # deliberately not a different model/architecture, so no new chat
  # template or tokenizer behaviour to verify), for the new
  # standard.xlarge flavor per direct request ("allow the use of a
  # large model with a larger vm"). ~7.17GB (Q8_0); model_sha256 is
  # again Hugging Face's own X-Linked-ETag, confirmed via a real HEAD
  # request before use, same as the Q4_K_M entry above.
  [Mistral-7B-Instruct-v0.3-Q8_0.gguf]="https://huggingface.co/bartowski/Mistral-7B-Instruct-v0.3-GGUF/resolve/main/Mistral-7B-Instruct-v0.3-Q8_0.gguf"
  # llm-chat's own default model as of this pin, replacing Mistral-7B —
  # per direct request ("mistral itself isn't really the best... code
  # is all I really care about"): Qwen2.5-Coder-7B-Instruct is
  # code-specialized (benchmarks well ahead of general 7B models like
  # Mistral on HumanEval/MBPP) and, confirmed live via its own real
  # chat_template, actually supports a system role (ChatML,
  # `{%- if messages[0]['role'] == 'system' %}`) — Mistral-7B-Instruct-
  # v0.3's own template has no system-role handling at all (confirmed
  # live: GET /props' chat_template_caps reports
  # "supports_system_role": false), meaning llm-chat's own
  # webui_system_message anti-hallucination prompt was likely never
  # being honoured regardless of the temperature/localStorage fix.
  # Same bartowski quantizer already trusted for the Mistral pins.
  # ~4.36GB (Q4_K_M); model_sha256 is again HF's own X-Linked-ETag,
  # confirmed via a real HEAD request before use. distributed-llm keeps
  # defaulting to Mistral — its job is Sentinel log summarization, a
  # different task this switch wasn't asked to touch.
  [Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf]="https://huggingface.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"
  # llm-chat's standard.xlarge upgrade path — same swap as above, one
  # tier up. ~7.54GB (Q8_0).
  [Qwen2.5-Coder-7B-Instruct-Q8_0.gguf]="https://huggingface.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/Qwen2.5-Coder-7B-Instruct-Q8_0.gguf"
  # llm-chat's new default model — real testing found the 7B Qwen-Coder
  # still hallucinated on non-trivial code tasks (wrong percentile math,
  # a rolling-window dedup that was a complete no-op, invented function
  # names) even with the anti-hallucination system prompt actually
  # reaching it. Per direct request ("is there anything less likely to
  # hallucinate"): self-consistency/faithfulness to one's own generated
  # code scales with parameter count more reliably than prompting does,
  # so stepping up within the same trusted family/quantizer rather than
  # trying a different one. ~8.37GB (Q4_K_M), 48 transformer layers
  # (vs the 7B's 28) — api/gguf_meta.py now reads this directly from
  # each GGUF's own header rather than needing another hand-maintained
  # layer-count comment every time the model changes.
  [Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf]="https://huggingface.co/bartowski/Qwen2.5-Coder-14B-Instruct-GGUF/resolve/main/Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf"
  # llm-chat Stage 5 — Firecracker + jailer, one archive covers both (real
  # static musl binaries, GitHub release, ships its own SHA256SUMS/per-
  # asset .sha256.txt — verified directly against the real downloaded
  # archive before this pin was set, not transcribed from the release
  # notes). firecracker_sha256 in variables.tf is this whole archive's
  # own hash; the individual firecracker/jailer binary hashes inside it
  # are checked again by coordinator-cloud-init.yaml.tftpl after
  # extraction, against SHA256SUMS shipped inside the archive itself.
  [firecracker-v1.17.0-x86_64.tgz]="https://github.com/firecracker-microvm/firecracker/releases/download/v1.17.0/firecracker-v1.17.0-x86_64.tgz"
  # llm-chat Stage 5 — a pinned kernel build from Firecracker's own public
  # CI artifact bucket (documented, official source for exactly this —
  # see firecracker-microvm/firecracker's own docs/getting-started.md),
  # not built from source here. Deliberately NOT Firecracker's own demo
  # rootfs from the same bucket — see build-firecracker-rootfs.sh's own
  # header for why a custom-built rootfs is used instead.
  [firecracker-vmlinux-6.1.155]="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/x86_64/vmlinux-6.1.155"
  # llm-chat Stage 5B — vendored websockets (see this file's own PACKAGES
  # comment above for why apt's jammy build can't be used). 16.1.1 is
  # the newest release that still supports Python 3.10 (17.x requires
  # 3.11+, confirmed via PyPI's own metadata) — a real manylinux wheel
  # for cp310/x86_64, pinned via PyPI's own JSON API (files.pythonhosted.org),
  # not guessed. Extracted into /opt/llama.cpp/vendor-py/ and referenced
  # via sandbox-terminal.service's own PYTHONPATH=, matching the "pinned
  # artifact, not live pip execution on the guest" convention this
  # project already applies to every other third-party dependency.
  [websockets-16.1.1-cp310-manylinux.whl]="https://files.pythonhosted.org/packages/f3/18/a17e2f0cde02dc10154c808deed7e1d8528afff93612f70d3f0a5b19b011/websockets-16.1.1-cp310-cp310-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl"
)
if [ "${SKIP_ZIM:-0}" = "1" ]; then
  unset "ARTIFACT_URLS[wikipedia_en_top_nopic_2026-06.zim]"
  unset "ARTIFACT_URLS[wikipedia_en_all_nopic_2026-06.zim]"
  unset "ARTIFACT_URLS[www.mankier.com_en_all_2026-07.zim]"
  unset "ARTIFACT_URLS[archlinux_en_all_maxi_2026-07.zim]"
  unset "ARTIFACT_URLS[docs.python.org_en_all_2026-08a.zim]"
  unset "ARTIFACT_URLS[peps.python_en_all_2026-08.zim]"
  unset "ARTIFACT_URLS[devdocs_en_python_2026-08.zim]"
  unset "ARTIFACT_URLS[devdocs_en_numpy_2026-07.zim]"
  unset "ARTIFACT_URLS[devdocs_en_pandas_2026-07.zim]"
  unset "ARTIFACT_URLS[devdocs_en_django_2026-07.zim]"
  unset "ARTIFACT_URLS[devdocs_en_flask_2026-07.zim]"
  unset "ARTIFACT_URLS[devdocs_en_fastapi_2026-04.zim]"
  unset "ARTIFACT_URLS[devdocs_en_matplotlib_2026-07.zim]"
  unset "ARTIFACT_URLS[devdocs_en_scikit-learn_2026-04.zim]"
  unset "ARTIFACT_URLS[devdocs_en_requests_2026-07.zim]"
  unset "ARTIFACT_URLS[devdocs_en_jinja_2026-07.zim]"
  unset "ARTIFACT_URLS[devdocs_en_click_2026-04.zim]"
  unset "ARTIFACT_URLS[devdocs_en_pygame_2026-07.zim]"
fi
if [ "${SKIP_LLM_MODEL:-0}" = "1" ]; then
  unset "ARTIFACT_URLS[Mistral-7B-Instruct-v0.3-Q4_K_M.gguf]"
  unset "ARTIFACT_URLS[Mistral-7B-Instruct-v0.3-Q8_0.gguf]"
  unset "ARTIFACT_URLS[Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf]"
  unset "ARTIFACT_URLS[Qwen2.5-Coder-7B-Instruct-Q8_0.gguf]"
  unset "ARTIFACT_URLS[Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf]"
fi
for name in "${!ARTIFACT_URLS[@]}"; do
  curl -fL --speed-limit 1024 --speed-time 30 -C - -o "$REPO_DIR/artifacts/$name" "${ARTIFACT_URLS[$name]}"
done

# wifi-sniffer's RTL8812AU driver is built from source via DKMS on the
# guest itself (kernel-specific, not pre-buildable as a binary), but the
# GitHub source clone it starts from can still be pre-cached — avoids a
# live git fetch from GitHub at every guest boot.
RTL8812AU_REF="v5.6.4.2"
if [ ! -f "$REPO_DIR/artifacts/rtl8812au-$RTL8812AU_REF.tar.gz" ]; then
  RTL_TMP="$(mktemp -d)"
  git clone -b "$RTL8812AU_REF" --depth 1 https://github.com/aircrack-ng/rtl8812au.git "$RTL_TMP/rtl8812au"
  tar -C "$RTL_TMP" -czf "$REPO_DIR/artifacts/rtl8812au-$RTL8812AU_REF.tar.gz" rtl8812au
  rm -rf "$RTL_TMP"
fi

# llm-chat Stage 5 — the Firecracker golden guest rootfs isn't a plain
# download like everything above (it's debootstrap-built, needs a real
# root chroot environment), so it gets its own dedicated builder script
# rather than an ARTIFACT_URLS entry — same "not a plain download" shape
# as the RTL8812AU source cache above, just heavier. Skippable with
# SKIP_FC_ROOTFS=1 (e.g. CODENAME != jammy runs, where it's not used).
if [ "${SKIP_FC_ROOTFS:-0}" != "1" ] && [ "$CODENAME" = "jammy" ]; then
  echo "=== Building the Firecracker golden rootfs (llm-chat Stage 5) ==="
  "$SCRIPT_DIR/build-firecracker-rootfs.sh"
fi

{
  echo "Built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for name in "${!ARTIFACT_URLS[@]}"; do
    echo "$name  sha256=$(sha256sum "$REPO_DIR/artifacts/$name" | cut -d' ' -f1)  source=${ARTIFACT_URLS[$name]}"
  done
  echo "rtl8812au-$RTL8812AU_REF.tar.gz  sha256=$(sha256sum "$REPO_DIR/artifacts/rtl8812au-$RTL8812AU_REF.tar.gz" | cut -d' ' -f1)  source=https://github.com/aircrack-ng/rtl8812au.git@$RTL8812AU_REF"
  if [ -f "$REPO_DIR/artifacts/firecracker-rootfs-jammy.ext4.gz" ]; then
    echo "firecracker-rootfs-jammy.ext4.gz  sha256=$(sha256sum "$REPO_DIR/artifacts/firecracker-rootfs-jammy.ext4.gz" | cut -d' ' -f1)  source=build-firecracker-rootfs.sh (debootstrap, built fresh each run)"
  fi
} > "$REPO_DIR/artifacts/MANIFEST.txt"
touch "$REPO_DIR/artifacts/.build-complete"

echo "=== Standing up a throwaway $CODENAME builder instance ==="
VPC_ID=$(curl -s -X POST "$API/v1/vpcs" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX-vpc\",\"cidr_block\":\"10.250.0.0/16\"}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
SUBNET_ID=$(curl -s -X POST "$API/v1/subnets" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX-subnet\",\"vpc_id\":\"$VPC_ID\",\"cidr_block\":\"10.250.0.0/16\",\"zone\":\"a\",\"public\":true}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
SG_ID=$(curl -s -X POST "$API/v1/security-groups" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX-sg\",\"vpc_id\":\"$VPC_ID\",\"ingress_rules\":[{\"protocol\":\"tcp\",\"from_port\":22,\"to_port\":22,\"cidr\":\"0.0.0.0/0\"}],\"egress_rules\":[{\"protocol\":\"-1\",\"cidr\":\"0.0.0.0/0\"}]}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
INSTANCE_ID=$(curl -s -X POST "$API/v1/instances" "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUFFIX\",\"image_id\":\"$IMAGE_ID\",\"flavor\":\"standard.medium\",\"vpc_id\":\"$VPC_ID\",\"subnet_id\":\"$SUBNET_ID\",\"security_group_ids\":[\"$SG_ID\"]}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")

echo "Waiting for $INSTANCE_ID to reach running with a real IP..."
INSTANCE_IP=""
for i in $(seq 1 60); do
  resp=$(curl -s "$API/v1/instances/$INSTANCE_ID" "${AUTH[@]}")
  status=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))")
  INSTANCE_IP=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('private_ip',''))")
  if [ "$status" = "running" ] && [ -n "$INSTANCE_IP" ]; then
    break
  fi
  sleep 5
done
if [ -z "$INSTANCE_IP" ]; then
  echo "Builder instance never reached running/IP-assigned — aborting" >&2
  exit 1
fi
echo "Builder at $INSTANCE_IP, waiting for SSH..."
for i in $(seq 1 30); do
  ssh "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP" true 2>/dev/null && break
  sleep 5
done

# linux-headers-$(uname -r)/linux-modules-extra-$(uname -r) are cached
# under whatever kernel version this builder boots with — valid as long
# as guests boot the same $IMAGE_ID at roughly the same time as this
# build ran. A guest that picks up a kernel bump via unattended-upgrades
# before this repo is next rebuilt will fall back to wifi-sniffer's own
# live apt-get for just those two packages (same as it always has).
echo "=== Building the repo on the throwaway instance ==="
ssh "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP" "
  set -e
  # Third-party apt repos — ghidra-workstation needs temurin-21-jdk
  # (Adoptium), wifi-sniffer needs kismet (kismetwireless.net), the
  # frontend tier needs a current nodejs (NodeSource — Ubuntu 22.04's
  # own archive only has an ancient 12.x), and ha-frontend-lb's
  # centralized-logging tier needs grafana/loki/promtail (Grafana Labs'
  # own apt repo hosts all three). None of these exist in Ubuntu's own
  # archive, so these repos have to be trusted before the download step
  # below can see them at all.
  sudo mkdir -p /etc/apt/keyrings
  curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public | sudo gpg --dearmor -o /etc/apt/keyrings/adoptium.gpg
  echo \"deb [signed-by=/etc/apt/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb \$(lsb_release -cs) main\" | sudo tee /etc/apt/sources.list.d/adoptium.list
  wget -O - https://www.kismetwireless.net/repos/kismet-release.gpg.key --quiet | sudo gpg --dearmor | sudo tee /usr/share/keyrings/kismet-archive-keyring.gpg >/dev/null
  echo \"deb [signed-by=/usr/share/keyrings/kismet-archive-keyring.gpg] https://www.kismetwireless.net/repos/apt/release/\$(lsb_release -cs) \$(lsb_release -cs) main\" | sudo tee /etc/apt/sources.list.d/kismet.list >/dev/null
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | sudo gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
  echo \"deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main\" | sudo tee /etc/apt/sources.list.d/nodesource.list
  curl -fsSL https://apt.grafana.com/gpg.key | sudo gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
  echo \"deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main\" | sudo tee /etc/apt/sources.list.d/grafana.list

  sudo apt-get update
  sudo apt-get install -y dpkg-dev
  sudo DEBIAN_FRONTEND=noninteractive apt-get install --download-only --reinstall -y \
    ${PACKAGES[*]} ${THIRDPARTY_PACKAGES[*]} \
    linux-headers-\$(uname -r) linux-modules-extra-\$(uname -r)
  mkdir -p /tmp/repo-build
  cp /var/cache/apt/archives/*.deb /tmp/repo-build/
  cd /tmp/repo-build
  dpkg-scanpackages . /dev/null > Packages
  gzip -9fc Packages > Packages.gz
  cat > MANIFEST.txt <<MANIFEST
Built: \$(date -u +%Y-%m-%dT%H:%M:%SZ)
Ubuntu release: \$(lsb_release -cs)
Source packages requested: ${PACKAGES[*]} ${THIRDPARTY_PACKAGES[*]} linux-headers-\$(uname -r) linux-modules-extra-\$(uname -r)
Package count (including transitive deps): \$(ls /tmp/repo-build/*.deb | wc -l)
MANIFEST
"

echo "=== Pulling the built repo back to the host ==="
rm -f "$REPO_DIR/apt-repo"/*.deb "$REPO_DIR/apt-repo/.build-complete"
scp "${SSH_OPTS[@]}" "ubuntu@$INSTANCE_IP:/tmp/repo-build/*" "$REPO_DIR/apt-repo/"
touch "$REPO_DIR/apt-repo/.build-complete"

echo "=== Done ==="
echo "Repo: $REPO_DIR/apt-repo ($(ls "$REPO_DIR/apt-repo"/*.deb | wc -l) packages)"
echo "Artifacts: $REPO_DIR/artifacts"
echo "Served at: http://192.168.100.1:8090/$CODENAME/apt-repo/ and /$CODENAME/artifacts/"
