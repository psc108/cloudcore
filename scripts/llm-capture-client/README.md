# llm-capture-client

## Purpose

Lets a student who runs a language model on their own machine contribute to
the CloudCore llm-chat learning corpus. The client sends **what the model
was asked and the code it wrote**, and nothing else. The CloudCore host then
re-runs that code in an llm-chat coordinator's own sandbox and records
**that** result. Execution results reported by a client are refused, so
every result in the corpus was actually executed on the platform.

Submissions land as `pending` in the corpus, like every other capture. An
instructor publishes or hides them from the Dashboard's LLM Examples page.

## Prerequisites

- Python 3.9 or newer.
- A capture token, created by an instructor in the Dashboard (LLM Examples
  → Capture tokens). It's shown once; keep it private.
- Network access to the CloudCore host's capture listener (port 8083) on
  the lab LAN. The client refuses plain HTTP to any non-private address,
  because the token would travel unencrypted.
- For `watch`: a local OpenAI-compatible model server, such as Ollama
  (`http://127.0.0.1:11434`) or llama.cpp's `llama-server`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/llm-capture && chmod 700 ~/.config/llm-capture
printf '%s\n' 'lcc_...your token...' > ~/.config/llm-capture/token
chmod 600 ~/.config/llm-capture/token
export LLM_CAPTURE_SERVER=http://192.168.1.106:8083
```

## Usage

Submit one file (the language is inferred from the extension):

```bash
.venv/bin/python llm_capture_client.py submit \
    --file squares.c --model qwen2.5-coder:7b \
    --prompt "Write a C program that prints the first 5 square numbers" --wait 120
```

`--wait` polls until a coordinator has re-run it, then prints the real
result. Without it, the submission id is printed; check it later with:

```bash
.venv/bin/python llm_capture_client.py status <id>
```

Capture automatically while chatting: run the proxy and point your chat
tool at it instead of at the model server:

```bash
.venv/bin/python llm_capture_client.py watch --upstream http://127.0.0.1:11434
# then use http://127.0.0.1:8085 as the OpenAI-compatible base URL
```

Every answer whose first fenced code block is tagged with a supported
language (`python`, `bash`, `javascript`, `c`, `cpp`, `go`) is submitted in
the background. Untagged blocks are skipped rather than guessed. A capture
failure never interrupts the chat. `--dry-run` on either `submit` or
`watch` shows what would be sent and sends nothing.

A submission stays `pending` until a coordinator is running; the host
retries every minute and gives up (`expired`) after 24 hours. `rejected`
means the coordinator refused it (for example, an unsupported language).

Exit codes: `0` ok, `2` usage, `3` token problem, `4` server refused, `5`
network.

## Configuration

| Setting | Where | Default | Meaning |
|---|---|---|---|
| Server URL | `--server` or `LLM_CAPTURE_SERVER` | (required) | The CloudCore capture listener, e.g. `http://192.168.1.106:8083` |
| Token | `LLM_CAPTURE_TOKEN` or `~/.config/llm-capture/token` | (required) | Per-student capture token. The file must be mode `0600`. Never passed on the command line |
| `watch --upstream` | flag | (required) | Local model server to proxy |
| `watch --listen` | flag | `127.0.0.1:8085` | Where the proxy listens |
| `--model` | flag | request's `model` field (`watch`) | Model name recorded with the submission |

Server-side limits (set on the CloudCore host): 20 submissions per token
per hour (`LLM_CLIENT_SUBMISSIONS_PER_HOUR`), code ≤ 64KB, prompt ≤ 16KB,
note ≤ 2KB.
