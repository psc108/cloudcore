#!/usr/bin/env python3
"""Reverse proxy + grounded code verification for llm-chat's coordinator.

Sits in front of llama-server (which binds 127.0.0.1 only once this is
in place) on the port the load balancer actually points at. As of
Phase 4, this proxy IS the deployment's only interface -- llama-server's
own general-purpose webui is no longer reachable at all (GET / serves
the sandbox page instead; passthrough is now an explicit allowlist of
just /health for the LB's own health check, see do_GET/_not_found).
Every model interaction now goes through POST /sandbox/ask, which
builds its own messages list (a tightly scoped system prompt + the
browser's own held conversation + this turn's code/question) rather
than relaying an arbitrary caller-supplied one -- deliberately: a
free-form chat box invites exactly the ungrounded, off-topic question
this whole mechanism has no way to verify.

The model's response is relayed to the browser in real time exactly as
llama-server streams it, while this process also accumulates the full
assistant text. Once the model's own stream ends, if the text contains
a fenced Python code block, the code is run in a sandbox and the real
result is appended as more streamed content in the SAME turn -- never
a separate UI element, never summarized or reworded, clearly labeled
as actually executed rather than model output.

Phase 2: if that first execution fails, up to VERIFY_MAX_FIX_ROUNDS
grounded fix attempts follow automatically, in the same turn -- each
one a fresh internal completion (direct to llama-server's own internal
port, never back through this proxy) grounded in the REAL traceback
just captured, not another unverified guess. Only the truly final
block in the whole chain ever tells the student to ask again
themselves; an intermediate failure is followed by another automatic
attempt, so inviting the student to ask there would be misleading.

Phase 4 also adds POST /sandbox/run -- the student's own code, executed
as-is via the same sandbox, with no model involved and nothing
captured (there's no model claim to ground).

See llm-chat-interactive-sandbox-Phased-Implementation.md (Phase 4)
and llm-chat-verification-Phased-Implementation.md (Phases 1-3) for
the full design rationale.

Pure stdlib -- no new dependency on the guest image, matching the only
real precedent for a CloudCore-authored guest-side service found in
this codebase (examples/ha-frontend-lb's serve-ca-certs.py).
"""
from __future__ import annotations

import html
import http.client
import http.server
import json
import os
import pwd
import re
import resource
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8721

LISTEN_PORT = int(os.environ.get("VERIFY_LISTEN_PORT", "8620"))
ENABLE_VERIFICATION = os.environ.get("VERIFY_ENABLED", "true").lower() == "true"
VERIFY_TIMEOUT_S = int(os.environ.get("VERIFY_TIMEOUT_SECONDS", "15"))
VERIFY_MAX_MEMORY_MB = int(os.environ.get("VERIFY_MAX_MEMORY_MB", "256"))
VERIFY_MAX_FIX_ROUNDS = int(os.environ.get("VERIFY_MAX_FIX_ROUNDS", "3"))

# Direct report: real Ask/Linux Help answers were regularly cut off
# mid-word, and manually asking again reliably completed it. Root
# cause: llama-server's own -c context_size ceiling (not the model
# choosing to stop) -- its OpenAI-compatible endpoint's final streamed
# chunk carries finish_reason: "length" whenever generation is cut off
# by that ceiling rather than a real end-of-response token, which this
# file never used to even read. _relay_and_verify_stream() now checks
# for that and automatically fires up to MAX_CONTINUATION_ROUNDS
# "continue exactly where you left off" follow-ups, relayed seamlessly
# into the same SSE stream -- from the browser's own JS this reads as
# one continuous reply typing out, no student action needed. A
# ceiling, not a guarantee every round runs: it stops as soon as one
# round actually finishes naturally (finish_reason: "stop"). Only
# covers the initial streamed answer today -- verify_and_maybe_fix()'s
# own grounded-fix-round replies (a separate, buffered, non-streaming
# call via _call_llama_direct()) are typically much shorter and were
# not the reported symptom, so left out of scope for now.
MAX_CONTINUATION_ROUNDS = int(os.environ.get("MAX_CONTINUATION_ROUNDS", "3"))

# How much of the cut-off answer's own tail to quote back at the model
# when asking it to continue (see _continuation_user_turn below).
_CONTINUATION_TAIL_CHARS = 300


def _continuation_user_turn(prior_text: str) -> str:
    """Build the user turn for an automatic continuation round. Found
    live: an earlier, purely instructional version of this prompt
    ("continue exactly where you left off, do not repeat") was
    unreliable -- tested against a response deliberately cut off very
    early (a small max_tokens, to exercise this path quickly), the
    model repeatedly just restarted its whole answer from the
    beginning ("Certainly! ...") instead of truly resuming, which would
    have shown up as real, silently duplicated content in a genuine
    answer. Quoting the literal tail of what it already wrote back at
    it, and asking it to continue from that exact text, is a well-
    established stronger technique than an abstract instruction alone
    -- it gives the model concrete text to pattern-match against rather
    than trusting it to remember where a separate, fresh completion
    request left off."""
    tail = prior_text[-_CONTINUATION_TAIL_CHARS:]
    return (
        "Your previous reply was cut off before it finished. Here is the "
        "exact end of what you already wrote:\n\n"
        f"---\n{tail}\n---\n\n"
        "Continue writing from that exact point onward. Do not repeat any "
        "of the text above, do not say something like 'Certainly' or "
        "restart your explanation -- output ONLY the new text that comes "
        "next, picking up mid-sentence/mid-word if that's where it was cut."
    )

# Found live, chasing this same continuation feature: a real generation
# can silently DEADLOCK inside llama-server's own thread scheduling when
# RPC offloading (--rpc, this deployment's own worker split) is in play
# -- confirmed via /proc/<pid>/task/*/stack on both ends: the RPC worker
# sits healthily idle in recvfrom() waiting for a request that never
# comes, while every llama-server thread except the plain HTTP accept()
# listener sits in futex_wait, genuinely stuck, not doing compute or
# I/O. Once this happens the ENTIRE process is wedged for every future
# request, not just the one that triggered it -- confirmed live: a
# freshly restarted llama-server, given a brand-new small prompt,
# deadlocked again immediately. This is a real bug in llama-server/
# ggml-rpc itself (documented upstream as an experimental, not fully
# hardened backend) -- nothing in this file can fix the deadlock
# itself, only detect it and recover the SERVICE for whoever asks next.
#
# GENERATION_STALL_TIMEOUT_S: how long with zero real content (not
# just any byte -- llama-server's own SSE keep-alive pings keep the
# raw socket alive even while fully deadlocked, which is exactly why a
# plain connection-level read timeout never caught this) before a
# response is treated as stalled. Well above this platform's own
# measured worst-case per-token latency (~1-2s at the observed ~1.3
# tok/s) so a merely-slow response is never misdiagnosed as stuck.
GENERATION_STALL_TIMEOUT_S = int(os.environ.get("GENERATION_STALL_TIMEOUT_SECONDS", "120"))

# F-130: two independent live `strace -f` sessions on this process during
# a confirmed-busy, genuinely-stalled request showed ZERO syscalls of any
# kind from the thread that _relay_one_stream()'s own busy-gate logic
# proves must be alive and running -- an unresolved contradiction between
# external tracing and the code's own behavior. This flag turns on
# in-process logging of the read loop itself (thread id, every poll
# cycle, every real-content chunk) so the loop's actual behavior can be
# observed directly from inside the interpreter, removing the ptrace/
# strace-interaction variable entirely. Off by default -- a poll cycle
# fires roughly every _SOCKET_POLL_TIMEOUT_S seconds and a real content
# chunk roughly every generated token, so this is genuinely noisy over a
# multi-minute generation and is meant to be switched on only while
# actively chasing F-130, not left running in normal operation.
_RELAY_DEBUG = os.environ.get("RELAY_DEBUG", "0") == "1"


def _relay_debug(msg: str) -> None:
    if _RELAY_DEBUG:
        print(f"RELAY_DEBUG [{time.time():.3f}] tid={threading.get_ident()}: {msg}", flush=True)

# F-132: retrieval-grounding for Linux Help, direct request after a real
# hallucination ("explain ring 3 in detail" claimed other applications
# run in "different rings" -- they don't, every user app is Ring 3, only
# the kernel differs). KIWIX_HOST empty (e.g. a template built before
# this round, or kiwix_peer_id pointed somewhere unreachable) means
# _kiwix_search() always returns "" -- grounding is strictly additive,
# Linux Help must keep working exactly as before if it's ever missing.
KIWIX_HOST = os.environ.get("KIWIX_HOST", "")
KIWIX_PORT = int(os.environ.get("KIWIX_PORT", "8621"))
_KIWIX_TIMEOUT_S = 3
_KIWIX_TAG_RE = re.compile(r"<[^>]+>")

# Direct follow-up, after confirming grounding actually worked on a real
# question: "are we able to tell what resources the llama server used"
# -- the honest answer was "probably, but not provably" (only failures
# were ever logged). This round: grounding extended to the coding Ask
# panel too (not just Linux Help), and every completed ask on either
# panel is now pushed to Sentinel -- a fixed, pre-existing host-level
# service (unlike kiwix, not something this template provisions itself,
# so a plain host/port pair with a real default rather than a module
# output). Empty SENTINEL_HOST would disable the push the same way
# empty KIWIX_HOST disables search -- not currently offered as a toggle
# since Sentinel is always expected to be present, but the same "just
# returns/no-ops" shape is kept for consistency and so a build that
# genuinely doesn't have Sentinel reachable degrades the same way.
SENTINEL_HOST = os.environ.get("SENTINEL_HOST", "")
SENTINEL_PORT = int(os.environ.get("SENTINEL_PORT", "8900"))
_SENTINEL_PUSH_TIMEOUT_S = 5

# Found live verifying this same round's own retrieval mechanism: a
# large-corpus full-text search engine is very sensitive to exact query
# phrasing. The keyword-dense "ring 3 x86 privilege" correctly returns
# "Protection ring" as the #1 hit; the SAME topic asked the way a real
# student actually phrased it -- "explain ring 3 in detail" -- returns
# Tolkien novels and pop songs instead (common words like "explain"/
# "detail" swamp the ranking; confirmed the real article doesn't even
# appear in the top 30 results for that phrasing). Stripping filler
# words client-side doesn't reliably fix this either (tested: "ring 3"
# alone matches boxing rankings). The one thing that does: asking the
# model itself for a short, keyword-dense reformulation first.
_SEARCH_TERMS_TIMEOUT_S = 30
_SEARCH_TERMS_SYSTEM = (
    "Extract 3-6 specific technical search keywords from the user's "
    "question, suitable for a full-text search engine. Respond with "
    "ONLY the keywords, space-separated, no punctuation, no "
    "explanation. Prefer precise technical terms (protocol names, "
    "command names, CPU/kernel terminology) over generic words."
)


def _extract_search_terms(question: str) -> str:
    """One small, fast, non-streaming generation asking the model for a
    keyword-dense reformulation of `question`, used as the kiwix search
    pattern instead of the raw question text -- see the comment above
    for why this is necessary, not just an optimization. Adds one
    small round-trip of latency to this student's own request (the
    busy-gate already holds this slot for them; no other student is
    affected). Falls back to the raw question on ANY failure -- this
    is a quality improvement, never a blocking dependency, matching
    _kiwix_search's own defensive shape."""
    try:
        payload = {
            "messages": [
                {"role": "system", "content": _SEARCH_TERMS_SYSTEM},
                {"role": "user", "content": question},
            ],
            "max_tokens": 32,
            "stream": False,
            "temperature": 0.1,
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_SEARCH_TERMS_TIMEOUT_S) as resp:
            obj = json.loads(resp.read())
        terms = obj["choices"][0]["message"]["content"].strip()
        return terms if terms else question
    except Exception as e:
        print(f"verify-proxy: search-term extraction failed, using raw question: {e!r}", flush=True)
        return question


def _local_corpus_search(search_terms: str) -> tuple[str, list[dict]]:
    """Direct follow-up to F-136's own grounding_log: "could we benefit
    by checking that corpus first... and then grounding in the other
    areas?" -- checked first, before _kiwix_search() below, against
    Sentinel's own /api/grounding-log/match, which only ever returns a
    *human-approved* past entry (see grounding.find_match()'s own
    comment on the Sentinel side for the matching rules) -- an
    unreviewed or rejected entry, even an exact text match, is never
    returned. Same defensive shape as _kiwix_search(): empty
    SENTINEL_HOST, no match, or any failure all just mean "nothing
    local, fall through to kiwix" -- never blocks, never raises. The
    one real hit this returns is labeled "Verified Q&A" (not a source
    name like "Wikipedia") so the model's own prompt, this file's own
    log, and Sentinel's own UI can all tell a reused vetted answer
    apart from a fresh general-corpus one."""
    if not SENTINEL_HOST or not search_terms:
        return "", []
    try:
        qs = urllib.parse.urlencode({"q": search_terms})
        url = f"http://{SENTINEL_HOST}:{SENTINEL_PORT}/api/grounding-log/match?{qs}"
        # Same short, hard timeout as _kiwix_search()'s own -- this call
        # is synchronous, on the critical path of every ask (unlike the
        # backgrounded push below), so a slow Sentinel must never
        # meaningfully delay an answer over an optional local-corpus hit.
        with urllib.request.urlopen(url, timeout=_KIWIX_TIMEOUT_S) as resp:
            match = json.loads(resp.read())
        if not match:
            return "", []
        snippet = (match.get("answer") or "").strip()
        if not snippet:
            return "", []
        reference = {"source": "Verified Q&A", "title": match.get("question", ""), "snippet": snippet}
        block = ("Reference material (for fact-checking only -- explain in your own "
                 "words, and note plainly if this doesn't fully answer the question):\n"
                 f'[Verified Q&A] "{reference["title"]}": {snippet}\n\n')
        return block, [reference]
    except Exception as e:
        print(f"verify-proxy: local corpus lookup failed, trying kiwix instead: {e!r}", flush=True)
        return "", []


def _kiwix_search(search_pattern: str) -> tuple[str, list[dict]]:
    """Query the retrieval-grounding kiwix-serve instance (Wikipedia +
    ManKier man pages + ArchWiki, all three loaded into one instance --
    see kiwix-cloud-init.yaml.tftpl) for real reference snippets
    relevant to `search_pattern` (the output of _extract_search_terms(),
    NOT the raw student question -- see that function's own comment for
    why). Returns (block, references): `block` is a short prompt-ready
    string to prepend to the model's own prompt (or "" if nothing useful
    came back), `references` is the same hits as a list of
    {source, title, snippet} dicts -- kept separate rather than
    re-parsed later so _log_grounding() (see its own comment) can record
    exactly what was actually shown to the model, not a re-derived
    guess. Never raises -- unreachable, slow, or empty all just mean no
    grounding this turn, the same defensive shape
    _open_upstream_completion() already uses for llama-server itself. A
    short, hard timeout: this sits on the critical path of every ask
    request, so a slow/stuck kiwix instance must never meaningfully
    delay -- let alone stall -- a real answer over an optional accuracy
    improvement."""
    if not KIWIX_HOST or not search_pattern:
        return "", []
    try:
        qs = urllib.parse.urlencode({"pattern": search_pattern, "format": "xml", "pageLength": 3})
        url = f"http://{KIWIX_HOST}:{KIWIX_PORT}/search?{qs}"
        with urllib.request.urlopen(url, timeout=_KIWIX_TIMEOUT_S) as resp:
            body = resp.read()
        root = ET.fromstring(body)
        lines = []
        references = []
        for item in root.findall(".//item")[:2]:
            title_el = item.find("title")
            desc_el = item.find("description")
            if title_el is None or desc_el is None or not (desc_el.text or "").strip():
                continue
            title = (title_el.text or "").strip()
            snippet = _KIWIX_TAG_RE.sub("", desc_el.text or "").strip()
            book_title_el = item.find("book/title")
            source = (book_title_el.text or "").strip() if book_title_el is not None else "Reference"
            lines.append(f'[{source}] "{title}": {snippet}')
            references.append({"source": source, "title": title, "snippet": snippet})
        if not lines:
            return "", []
        return (("Reference material (for fact-checking only -- explain in your own "
                 "words, and note plainly if this doesn't fully answer the question):\n"
                 + "\n".join(lines) + "\n\n"), references)
    except Exception as e:
        print(f"verify-proxy: kiwix search failed, answering without grounding: {e!r}", flush=True)
        return "", []


def _push_grounding_to_sentinel(entry: dict) -> None:
    """Best-effort POST of one grounding-log entry to Sentinel's own
    /api/grounding-log -- same shape as api/scheduler.py's own
    _sentinel_post() (plain urllib, no auth header, matching that
    endpoint's own already-established precedent). Always called from a
    background thread (see _log_grounding below), never on the request-
    handling path itself, so a slow or unreachable Sentinel can never
    add latency to -- let alone block -- the student's own answer."""
    try:
        body = json.dumps(entry).encode()
        req = urllib.request.Request(
            f"http://{SENTINEL_HOST}:{SENTINEL_PORT}/api/grounding-log",
            data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=_SENTINEL_PUSH_TIMEOUT_S).close()
    except Exception as e:
        print(f"verify-proxy: Sentinel grounding-log push failed (non-fatal): {e!r}", flush=True)


def _log_grounding(endpoint_label: str, question: str, answer: str, search_terms: str,
                    references: list, code_verified, grounding_source: str = "none") -> None:
    """Records what actually happened for one completed ask -- the
    search terms used, what (if anything) kiwix found, and for the
    coding panel, whether its own execution-verification passed. Direct
    follow-up to confirming grounding worked on a real question ("are
    we able to tell what resources the llama server used") -- until
    now only kiwix *failures* were ever logged (see _kiwix_search's own
    comment), never what was actually used on success. Called for
    every completed ask on either panel (grounded=False is itself real,
    useful data -- "no reference material existed for this question"),
    from _relay_and_verify_stream() once the full answer text (and, for
    the coding panel, its own verification outcome) are known -- not
    from _do_handle_ask() right after the search, so the record
    reflects the whole turn, not just the retrieval step.

    Two channels, both best-effort: a GROUNDING_LOG line to stdout,
    matching ASK_OUTCOME's own convention exactly (flows to Loki via
    the same journald->Promtail pipeline already proven zero-extra-work
    in F-132's own history) for local/Grafana visibility; and a direct
    push to Sentinel's own DB (see _push_grounding_to_sentinel), off
    the request-handling thread, for a queryable, browsable record --
    Sentinel's own Loki-polling watch loop is purpose-built for
    trouble-detection windows, not a general audit trail, so this
    mirrors the *other* existing precedent instead (api/scheduler.py's
    own direct pushes to /api/kb/import et al)."""
    entry = {
        "endpoint": endpoint_label, "question": question, "answer": answer,
        "search_terms": search_terms, "references": references,
        "grounded": bool(references), "code_verified": code_verified,
        "grounding_source": grounding_source,
    }
    print(f"GROUNDING_LOG {json.dumps(entry)}", flush=True)
    if SENTINEL_HOST:
        threading.Thread(target=_push_grounding_to_sentinel, args=(entry,), daemon=True).start()

# How often _relay_one_stream's read loop wakes up (via a short socket
# read timeout) to re-check the stall clock -- NOT the stall threshold
# itself. Short enough that GENERATION_STALL_TIMEOUT_S is honored
# reasonably promptly, long enough not to busy-loop.
_SOCKET_POLL_TIMEOUT_S = 5

# A stalled request means the WHOLE process is wedged, so several
# concurrent students could all detect the same stall within moments of
# each other -- this cooldown means only the first one actually fires
# `systemctl restart`, not one redundant restart per stalled request.
_LLAMA_RESTART_COOLDOWN_S = 30
_llama_restart_lock = threading.Lock()
_llama_last_restart_time = 0.0


def _restart_llama_server() -> None:
    """Best-effort self-heal for the deadlock described above: restart
    llama-server.service so the NEXT student's request has a healthy
    process to talk to, since nothing short of a restart clears this
    (confirmed live -- the deadlock reproduced immediately even on a
    freshly restarted process talking to a freshly restarted RPC
    worker, so this is not guaranteed to fully fix it, only to give
    the next attempt the best real chance). Runs in its own thread,
    fire-and-forget -- the request that detected the stall has already
    told its own student what happened and returns immediately rather
    than waiting out the ~90s model reload too. verify-proxy.service
    itself runs as root (see its own systemd unit), so this needs no
    sudo/password prompt.

    Confirmed live: verify-proxy.service's own unit has
    `Requires=llama-server.service`, so this restart briefly restarts
    verify-proxy.service TOO (systemd's own dependency propagation,
    pre-existing, not something this function causes deliberately) --
    including the very process this function is running in. That's an
    acceptable, low-cost side effect, not a bug: verify-proxy's own
    restart takes a couple of seconds (no model to reload), so the only
    real impact is any OTHER concurrent request landing in that brief
    window gets its own connection reset -- a clear, honest failure a
    retry resolves, not a silent hang. Nothing here waits for or
    depends on this process surviving past this point."""
    global _llama_last_restart_time
    with _llama_restart_lock:
        if time.time() - _llama_last_restart_time < _LLAMA_RESTART_COOLDOWN_S:
            return
        _llama_last_restart_time = time.time()
    try:
        subprocess.run(["systemctl", "restart", "llama-server.service"],
                        timeout=15, check=True, capture_output=True)
        print(f"verify-proxy: restarted llama-server.service after a generation "
              f"stall (no real content for over {GENERATION_STALL_TIMEOUT_S}s)", flush=True)
    except Exception as e:
        print(f"verify-proxy: FAILED to restart llama-server.service after a "
              f"generation stall: {e!r}", flush=True)

SANDBOX_USER = "sandboxrunner"

# Same fixed pool sandbox_terminal.py's own preview proxy listens on --
# read here only to show students a persistent reminder on the Terminal
# panel itself (SANDBOX_PAGE_HTML's own __PREVIEW_PORTS_HINT__ token,
# substituted once below at import time). The connected microVM's own
# fresh "connected" WS message repeats the same list at the moment a
# session actually starts -- this static copy is the one a student can
# still see after that scrolls away.
PREVIEW_PORTS = [p for p in os.environ.get("PREVIEW_PORTS", "").split(",") if p.strip()]

# Phase 3 -- central learning corpus capture (api/examples_listener.py,
# api/llm_examples_routes.py). Best-effort only: a capture failure must
# never affect the chat response itself, so every call site wraps this
# in try/except and only ever logs. Empty EXAMPLES_API_BASE (unset)
# disables capture entirely rather than failing outward -- lets this
# same proxy source run against an older API host with no Phase 3
# routes at all.
EXAMPLES_API_BASE = os.environ.get("EXAMPLES_API_BASE", "").rstrip("/")
EXAMPLES_API_TOKEN = os.environ.get("EXAMPLES_API_TOKEN", "")
EXAMPLES_MODEL_FILENAME = os.environ.get("EXAMPLES_MODEL_FILENAME", "")

# CloudCore Dashboard -- LLM Performance page's "live deployments" section
# (api/llm_deployments_routes.py). Reuses the EXAMPLES_API_* wiring above
# rather than new Terraform variables -- same host, same always-on
# listener, same shared ingestion token. Set from Terraform's own
# knowledge of this instance's eventual CloudCore-assigned name (locals.tf
# builds it from the same project/environment/name/index convention the
# instance-group module itself uses), not anything this guest could
# determine on its own at boot.
DEPLOYMENT_NAME = os.environ.get("DEPLOYMENT_NAME", "")

# Phase 4 -- the interactive sandbox's own system prompt. Deliberately
# separate from (and replaces the purpose of) webui_system_message,
# which only ever shaped llama-server's OWN webui -- moot now that
# GET / serves the sandbox instead (see do_GET). A mitigation, not a
# guarantee (a system prompt can still be talked around); the real
# safety net stays run_sandboxed()'s own grounding, same as Phases 1-2.
#
# Read from a plain text file, not an env var -- unlike every other
# VERIFY_*/EXAMPLES_* setting, this one is free-form prose (spaces,
# punctuation, a student's own template overrides), which a systemd
# Environment= line can't carry safely without real quoting risk. Same
# write-a-file convention coordinator-cloud-init.yaml.tftpl's own
# webui-config.json and verify_proxy.py entries already use. Falls
# back to a sensible built-in default so this file also runs correctly
# outside cloud-init (e.g. this module's own local tests).
_SANDBOX_SYSTEM_MESSAGE_DEFAULT = (
    "You are a lab coding assistant. Only discuss the Python code the "
    "student has provided in this conversation. If asked something "
    "unrelated to that code or to this lab exercise, politely decline "
    "and redirect the student back to their code. Only describe what "
    "code actually does -- never claim a function, sort, or check "
    "exists unless it is genuinely present in the code you just wrote "
    "or were shown; if you are not certain something is correct, say "
    "so explicitly rather than stating it as fact. When suggesting a "
    "fix, provide the complete corrected script in a single fenced "
    "python code block, and keep your own explanation concise -- this "
    "hardware generates slowly, so prefer a short, precise answer over "
    "a long one where both would be equally correct. Code you write "
    "runs in a real sandbox that supports interactive input() calls -- "
    "if a script you wrote is waiting for input, you will be shown "
    "exactly what it has printed so far and asked what to provide; "
    "reply with ONLY a fenced ```stdin block containing exactly the "
    "one line to send. This can happen a few times per script, not "
    "unlimited, so keep prompts short and avoid scripts that would "
    "need a long back-and-forth. This code sandbox is Python-only, "
    "one-shot, and has no network access. Separately, the page's own "
    "Terminal panel gives a real persistent Linux shell with genuine "
    "internet access (pip install, curl, cloning a repo) that is "
    "otherwise fully isolated, plus ports __PREVIEW_PORTS_LIST__ "
    "reachable from "
    "the browser for previewing a web app run there -- if asked about "
    "installing packages, running something long-lived, or viewing a "
    "web app's own output, say to use the Terminal (whose own panel "
    "lists the exact ports), not this code sandbox."
)
_SANDBOX_SYSTEM_MESSAGE_PATH = os.environ.get(
    "SANDBOX_SYSTEM_MESSAGE_FILE", "/opt/llama.cpp/sandbox-system-message.txt")
try:
    SANDBOX_SYSTEM_MESSAGE = open(_SANDBOX_SYSTEM_MESSAGE_PATH).read().strip() \
        or _SANDBOX_SYSTEM_MESSAGE_DEFAULT
except OSError:
    SANDBOX_SYSTEM_MESSAGE = _SANDBOX_SYSTEM_MESSAGE_DEFAULT

# Substituted here, not baked into the Terraform/Ansible default text
# directly, so the actual configured PREVIEW_PORTS (not a hardcoded
# guess) reaches the model even if sandbox_system_message is overridden
# with custom text that also carries this same placeholder. Found live
# testing the prompt update this token exists for: without a concrete
# port number, the model reliably filled the gap with Flask's own
# conventional default (5000) instead of a real, actually-proxied port
# -- worse than not mentioning ports at all, since it read as confident
# and was simply wrong for this deployment.
SANDBOX_SYSTEM_MESSAGE = SANDBOX_SYSTEM_MESSAGE.replace(
    "__PREVIEW_PORTS_LIST__",
    ", ".join(PREVIEW_PORTS) if PREVIEW_PORTS else "(none configured)")

# Stage 8 -- a second, genuinely separate system prompt for general
# Linux Q&A (POST /sandbox/linux-ask), kept apart from the coding Ask
# panel above per direct decision -- deliberately NOT scoped to "the
# student's own code", and deliberately NOT auto-executed/re-verified
# the way a Python code block is: a shell command a student didn't ask
# to run (rm, apt install, systemctl restart, sed -i) isn't safe to
# fire automatically against their own live Terminal session the way
# run_sandboxed()'s disposable namespace is. Grounding here is
# student-triggered instead -- see SANDBOX_PAGE_HTML's own "Run in
# Terminal" button on each fenced command block, sendToTerminal().
# Same file-based convention as SANDBOX_SYSTEM_MESSAGE above, for the
# same reason (free text can't safely ride a systemd Environment=
# line), and the same __PREVIEW_PORTS_LIST__ substitution -- F-106
# already found a prompt for this exact environment confidently
# inventing a wrong port when it wasn't told the real ones.
_LINUX_SYSTEM_MESSAGE_DEFAULT = (
    "You are a Linux help assistant for a lab environment. Answer any "
    "Linux question, from everyday usage (files, permissions, "
    "searching, editors) through real system administration (systemd, "
    "networking, package management, users and groups, disk and "
    "filesystem, cron, log inspection) -- the student may be a "
    "complete beginner or already comfortable at the command line, so "
    "don't assume either. Only describe what a command actually does "
    "-- never claim a flag or behavior exists unless you are genuinely "
    "sure of it; if you are not certain something is correct, say so "
    "explicitly rather than stating it as fact. When you suggest a "
    "command, put it in its own fenced ```bash code block so the "
    "student can run it with one click -- a command you suggest is "
    "NEVER run without the student clicking 'Run in Terminal' "
    "themselves, but once they do, the real result IS automatically "
    "checked: if it fails, you will be shown the real terminal "
    "transcript and exit code and asked to diagnose it and suggest a "
    "fix, same as this turn. This hardware generates slowly, so keep answers "
    "short and precise rather than long where both would be equally "
    "correct. The student's own Terminal panel is a real, minimal "
    "Ubuntu 22.04 shell with genuine internet access, but: its package "
    "index only covers the 'main' archive component (a 'universe' "
    "package needs another route), it has no persistent storage "
    "across sessions, and it cannot reach anything on the local "
    "network except the real internet. Ports __PREVIEW_PORTS_LIST__ "
    "are reachable from the student's browser for previewing anything "
    "they serve there. This is a genuinely minimal image -- ordinary "
    "tools you might expect (e.g. fdisk) are often not preinstalled. "
    "If a command you suggest might need one, give ONLY ONE fenced "
    "```bash block for it, combining the install check and the real "
    "command with `||` in that single block -- for example exactly "
    "`command -v fdisk >/dev/null || sudo apt-get install -y fdisk; "
    "fdisk -l /dev/vda` (adjust the tool/package name and real "
    "command). Do NOT also show a plain, naive version of the command "
    "on its own first -- that copy would just fail with 'command not "
    "found' if the tool is missing, defeating the whole point. The "
    "student should only ever need to click 'Run in Terminal' once, "
    "on the one block you give them. Politely decline anything "
    "clearly unrelated to Linux or this lab and redirect back to that."
)
_LINUX_SYSTEM_MESSAGE_PATH = os.environ.get(
    "LINUX_SYSTEM_MESSAGE_FILE", "/opt/llama.cpp/linux-system-message.txt")
try:
    LINUX_SYSTEM_MESSAGE = open(_LINUX_SYSTEM_MESSAGE_PATH).read().strip() \
        or _LINUX_SYSTEM_MESSAGE_DEFAULT
except OSError:
    LINUX_SYSTEM_MESSAGE = _LINUX_SYSTEM_MESSAGE_DEFAULT
LINUX_SYSTEM_MESSAGE = LINUX_SYSTEM_MESSAGE.replace(
    "__PREVIEW_PORTS_LIST__",
    ", ".join(PREVIEW_PORTS) if PREVIEW_PORTS else "(none configured)")

# Stage 2 -- CodeMirror assets embedded into this guest's own cloud-init
# (coordinator-cloud-init.yaml.tftpl's own write_files, same mechanism
# verify_proxy_source itself already proves) and served from here, not
# a CDN -- matches this whole project's offline-capable convention (see
# api/server.py's own GET /vendor/<path>, the dashboard's equivalent).
# An explicit filename allowlist, not a general static-file server --
# same least-exposure discipline this file already applies elsewhere
# (do_GET's own route allowlist, examples_listener.py's endpoint gate).
VENDOR_DIR = os.environ.get("SANDBOX_VENDOR_DIR", "/opt/llama.cpp/vendor")
VENDOR_CONTENT_TYPES = {
    "codemirror.min.js": "text/javascript",
    "codemirror.min.css": "text/css",
    "codemirror-theme-dracula.min.css": "text/css",
    "codemirror-addon-matchbrackets.min.js": "text/javascript",
    "codemirror-mode-python.min.js": "text/javascript",
    # Stage 5B -- already vendored for the Dashboard's own admin Terminal
    # feature (ui/vendor/, ui/src/js/11-terminal.js) -- reused as-is
    # rather than fetching/pinning a second copy.
    "xterm.min.js": "text/javascript",
    "xterm.min.css": "text/css",
    "xterm-addon-fit.min.js": "text/javascript",
}

# How often (seconds) to send an SSE keep-alive comment to the browser
# while waiting on an internal fix-round completion -- HAProxy's own
# timeout client/server (api/lb.py, 300s) is an inactivity timer, so a
# slow internal call needs *something* flowing periodically or the LB
# would sever the connection before the fix round ever finishes.
HEARTBEAT_INTERVAL_S = 20

# Real ceiling on what's shown back -- a runaway print loop shouldn't
# blow up the response; still a wide enough window to see real output.
MAX_OUTPUT_CHARS = 8000

CODE_BLOCK_RE = re.compile(r"```(python|py)?[ \t]*\n(.*?)```", re.DOTALL)
_PY_HINTS = ("def ", "import ", "print(", "class ", "for ", "if __name__")

# Stage 3 -- true interactive execution (run_sandboxed_interactive()).
# No new stdin channel to the browser at all: the MODEL drives an
# interactive session as a tool while answering (per direct decision),
# so this is entirely an internal detail of _handle_sandbox_ask's own
# orchestration -- a small fenced-block convention, same shape
# CODE_BLOCK_RE already proves reliable, for the model to supply
# exactly one line of stdin at a time.
STDIN_BLOCK_RE = re.compile(r"```stdin[ \t]*\n(.*?)```", re.DOTALL)

# How long the sandboxed process's own stdout/stderr must stay quiet
# (while it's still alive) before it's treated as "likely waiting for
# input" -- a heuristic, not a certainty (a script merely computing
# something slowly looks identical); confirmed live this session that
# a genuine input() block produces silence immediately and
# indefinitely, so a few seconds is a real, working threshold without
# being so short it misfires on ordinary brief pauses.
INTERACTIVE_QUIET_S = 3

# Hard caps enforced regardless of what the model/provide_input
# callback decides -- real generation latency observed this session
# ranges from ~170s (quiet host) to 1200s+ (contended host) *per
# exchange*, so the exchange count is the real practical control;
# the wall-clock figure is a generous backstop, not the primary one.
INTERACTIVE_MAX_EXCHANGES = 3
INTERACTIVE_MAX_WALL_S = 1800

# Stage 4 -- per-client rate limiting + a hard interrupt, rolled up
# from the Phase 4 doc's own "Explicitly out of scope" list. Keyed by
# the REAL client IP, not the TCP peer address: examples/llm-chat's
# own LB runs in HTTP mode with `option forwardfor` (confirmed in
# api/lb.py) specifically so this works -- without it every request
# would appear to come from the LB itself, one shared IP for every
# student, making per-IP limiting meaningless.
RATE_LIMIT_RUN_PER_MINUTE = int(os.environ.get("RATE_LIMIT_RUN_PER_MINUTE", "10"))
RATE_LIMIT_ASK_PER_10MIN = int(os.environ.get("RATE_LIMIT_ASK_PER_10MIN", "10"))

# One shared, thread-safe registry: per-IP request-time history (for
# the rate limits above), whether that IP currently has an /sandbox/ask
# in flight (the actual concurrency cap -- one at a time, per IP; a
# real student only ever has one live question, and this doubles as
# the key an interrupt request needs no other identifier to find), and
# the threading.Event a /sandbox/interrupt call sets to stop it.
_client_lock = threading.Lock()
_client_state: dict[str, dict] = {}

# Direct request: rather than let a second student's question queue up
# behind whoever's already asking, reject it outright ("we're busy",
# thrown away -- never sent upstream at all) and let that student know
# once the coordinator is free again, rather than silently queuing or
# leaving them guessing when to retry.
#
# This isn't only UX polish -- it directly targets a real, confirmed
# upstream llama.cpp bug (F-119, ggml-org/llama.cpp#28908, unmerged as
# of this writing): the RPC worker's own accept() loop is single-
# threaded, so a SECOND concurrent connection from this coordinator to
# the same worker while a first is still being served starves forever,
# which is exactly what two different students asking at once could
# trigger. `_client_state`'s own `ask_active` flag above only caps
# concurrency PER IP (one student can't double-ask), which does
# nothing to stop two DIFFERENT students colliding -- this is a
# separate, global gate on top of that one, checked first.
_llm_busy_lock = threading.Lock()
_llm_busy = False


def extract_python_code(text: str) -> str | None:
    """First fenced code block that's explicitly tagged python/py, or
    (untagged fences only) looks like Python by a cheap keyword check.
    Returns None if nothing worth running was found."""
    for lang, code in CODE_BLOCK_RE.findall(text):
        if lang in ("python", "py"):
            return code
        if not lang and any(h in code for h in _PY_HINTS):
            return code
    return None


def extract_stdin_value(text: str) -> str | None:
    """The first line of the first fenced ```stdin block, or None if
    the model's reply doesn't contain one -- treated by
    run_sandboxed_interactive()'s own caller as the model choosing to
    stop the interactive session there, same shape the fix loop
    already uses for "no runnable code block found"."""
    m = STDIN_BLOCK_RE.search(text)
    if not m:
        return None
    value = m.group(1).strip("\n")
    return value.splitlines()[0] if value else ""


def _sandbox_uid_gid() -> tuple[int, int]:
    pw = pwd.getpwnam(SANDBOX_USER)
    return pw.pw_uid, pw.pw_gid


def run_sandboxed(code: str) -> dict:
    """Executes `code` in an isolated net+pid namespace as an
    unprivileged, resource-limited user, and returns the REAL result
    verbatim -- never summarized, never reworded. {"stdout", "stderr",
    "exit_code", "timed_out"}."""
    tmpdir = tempfile.mkdtemp(prefix="verify-")
    script_path = os.path.join(tmpdir, "script.py")
    uid, gid = _sandbox_uid_gid()
    try:
        with open(script_path, "w") as f:
            f.write(code)
        os.chown(tmpdir, uid, gid)
        os.chown(script_path, uid, gid)

        mem_bytes = VERIFY_MAX_MEMORY_MB * 1024 * 1024
        # This bootstrap runs as root (unshare needs CAP_SYS_ADMIN, which
        # only the still-privileged verify-proxy process has) INSIDE the
        # freshly created net/pid namespace, then immediately drops to
        # the unprivileged sandbox user, applies resource limits, and
        # execs the real interpreter -- so the process that actually
        # runs the model's code is never root and is confined to this
        # one isolated namespace throughout.
        bootstrap = (
            "import os,resource;"
            f"os.setgid({gid});os.setuid({uid});"
            f"resource.setrlimit(resource.RLIMIT_CPU,({VERIFY_TIMEOUT_S},{VERIFY_TIMEOUT_S}));"
            f"resource.setrlimit(resource.RLIMIT_AS,({mem_bytes},{mem_bytes}));"
            "resource.setrlimit(resource.RLIMIT_NPROC,(32,32));"
            f"resource.setrlimit(resource.RLIMIT_FSIZE,({10*1024*1024},{10*1024*1024}));"
            f"os.execvp('python3',['python3',{script_path!r}])"
        )
        cmd = ["unshare", "--net", "--pid", "--fork", "--mount-proc", "--",
               "python3", "-c", bootstrap]

        import subprocess
        proc = subprocess.Popen(
            cmd, cwd=tmpdir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=VERIFY_TIMEOUT_S + 5)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()

        stderr_text = stderr.decode(errors="replace")
        if timed_out:
            stderr_text += f"\n[Execution killed: exceeded {VERIFY_TIMEOUT_S}s]"
        return {
            "stdout": stdout.decode(errors="replace")[:MAX_OUTPUT_CHARS],
            "stderr": stderr_text[:MAX_OUTPUT_CHARS],
            "exit_code": proc.returncode,
            "timed_out": timed_out,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_sandboxed_interactive(code: str, provide_input, interrupt=None) -> dict:
    """Like run_sandboxed(), same isolation exactly (same unshare +
    unprivileged user + resource.setrlimit CPU/memory/proc/fsize
    limits -- CPU-time based, not wall-clock, so still correctly
    bounds a longer-lived session), but the process's stdin stays open
    and live instead of DEVNULL.

    Whenever the process goes quiet (no new stdout/stderr for
    INTERACTIVE_QUIET_S seconds) while still running,
    `provide_input(transcript_so_far)` is called and expected to
    return either a string to send as the next stdin line (no trailing
    newline -- one is added), or None to stop the session there (the
    process is then killed; whatever real output already happened
    stays in the transcript). Bounded by INTERACTIVE_MAX_EXCHANGES
    separate hand-offs and INTERACTIVE_MAX_WALL_S of total wall-clock
    time regardless of what provide_input decides -- enforced here,
    not left to the caller's own good behavior.

    `interrupt` (a threading.Event), if given, is checked every poll
    cycle (~0.5s) regardless of what the process is doing -- a student
    Stop request kills it promptly even mid-run, not just at the next
    pause point.

    Returns {"transcript", "stdout", "stderr", "exit_code",
    "timed_out", "interrupted", "exchanges"}. `transcript` interleaves
    stdout/stderr in the real order they arrived, with an inline
    marker at each point input was actually provided -- the honest,
    readable record of what really happened, not just two separate
    buffers with the ordering lost. `timed_out` means the overall
    session budget (exchanges or wall-clock) was exceeded; `interrupted`
    means a student explicitly stopped it -- distinct so the student is
    told honestly which one happened, never conflated."""
    tmpdir = tempfile.mkdtemp(prefix="verify-")
    script_path = os.path.join(tmpdir, "script.py")
    uid, gid = _sandbox_uid_gid()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    transcript: list[str] = []
    exchanges = 0
    session_timed_out = False
    was_interrupted = False
    try:
        with open(script_path, "w") as f:
            f.write(code)
        os.chown(tmpdir, uid, gid)
        os.chown(script_path, uid, gid)

        mem_bytes = VERIFY_MAX_MEMORY_MB * 1024 * 1024
        # Same bootstrap as run_sandboxed() -- see its own comment for
        # why this runs as root only long enough to drop privileges.
        bootstrap = (
            "import os,resource;"
            f"os.setgid({gid});os.setuid({uid});"
            f"resource.setrlimit(resource.RLIMIT_CPU,({VERIFY_TIMEOUT_S},{VERIFY_TIMEOUT_S}));"
            f"resource.setrlimit(resource.RLIMIT_AS,({mem_bytes},{mem_bytes}));"
            "resource.setrlimit(resource.RLIMIT_NPROC,(32,32));"
            f"resource.setrlimit(resource.RLIMIT_FSIZE,({10*1024*1024},{10*1024*1024}));"
            f"os.execvp('python3',['python3',{script_path!r}])"
        )
        cmd = ["unshare", "--net", "--pid", "--fork", "--mount-proc", "--",
               "python3", "-c", bootstrap]

        import subprocess
        proc = subprocess.Popen(
            cmd, cwd=tmpdir, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
        os.set_blocking(out_fd, False)
        os.set_blocking(err_fd, False)

        start = time.monotonic()
        last_output_at = start
        fds = [proc.stdout, proc.stderr]

        while True:
            if interrupt is not None and interrupt.is_set():
                was_interrupted = True
                break
            if time.monotonic() - start > INTERACTIVE_MAX_WALL_S:
                session_timed_out = True
                break

            readable, _, _ = select.select(fds, [], [], 0.5)
            got_output = False
            for f in readable:
                fd = f.fileno()
                try:
                    chunk = os.read(fd, 65536)
                except (BlockingIOError, OSError):
                    chunk = b""
                if chunk:
                    got_output = True
                    text = chunk.decode(errors="replace")
                    (stdout_parts if fd == out_fd else stderr_parts).append(text)
                    transcript.append(text)
            if got_output:
                last_output_at = time.monotonic()
                continue

            if proc.poll() is not None:
                break  # process exited on its own

            if time.monotonic() - last_output_at >= INTERACTIVE_QUIET_S:
                if exchanges >= INTERACTIVE_MAX_EXCHANGES:
                    session_timed_out = True
                    break
                value = provide_input("".join(transcript))
                if value is None:
                    break
                exchanges += 1
                transcript.append(f"\n>>> INPUT PROVIDED: {value!r}\n")
                try:
                    proc.stdin.write((value + "\n").encode())
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
                last_output_at = time.monotonic()

        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        # Drain anything left buffered in the pipes after the process
        # ended -- a real final chunk can still be sitting there.
        for fd, buf in ((out_fd, stdout_parts), (err_fd, stderr_parts)):
            try:
                rest = os.read(fd, 65536)
            except (BlockingIOError, OSError):
                rest = b""
            if rest:
                text = rest.decode(errors="replace")
                buf.append(text)
                transcript.append(text)

        if was_interrupted:
            transcript.append("\n[Interactive session stopped by the student]\n")
        elif session_timed_out:
            transcript.append(
                f"\n[Interactive session ended: exceeded its "
                f"{INTERACTIVE_MAX_EXCHANGES}-exchange / "
                f"{INTERACTIVE_MAX_WALL_S}s budget]\n"
            )

        return {
            "transcript": "".join(transcript)[:MAX_OUTPUT_CHARS],
            "stdout": "".join(stdout_parts)[:MAX_OUTPUT_CHARS],
            "stderr": "".join(stderr_parts)[:MAX_OUTPUT_CHARS],
            "exit_code": proc.returncode,
            "timed_out": session_timed_out,
            "interrupted": was_interrupted,
            "exchanges": exchanges,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def format_verification_block(result: dict, final: bool = True) -> str:
    """`final` controls only the trailing "ask again" invite -- an
    intermediate round in the Phase 2 fix loop is followed automatically
    by another attempt, so inviting the student to ask again there
    would be misleading; only the actual last block in a chain passes
    final=True."""
    passed = (not result["timed_out"]) and result["exit_code"] == 0
    lines = ["\n\n---\n### ACTUALLY EXECUTED (not model output)\n"]
    if result["stdout"].strip():
        lines.append("**stdout:**\n```\n" + result["stdout"].rstrip() + "\n```\n")
    if result["stderr"].strip():
        lines.append("**stderr:**\n```\n" + result["stderr"].rstrip() + "\n```\n")
    lines.append(f"**exit code:** {result['exit_code']}\n")
    if not passed and final:
        lines.append(
            "\n_This code did not run successfully. You can ask for "
            "another attempt in your next message._\n"
        )
    return "".join(lines)


def format_interactive_verification_block(result: dict, final: bool = True) -> str:
    """Stage 3's own counterpart to format_verification_block() --
    shows the real interleaved transcript (stdout/stderr/input in the
    order they actually happened) instead of two separate buffers,
    since order is exactly what makes an interactive session honest
    and readable rather than confusing."""
    passed = (not result["timed_out"]) and result["exit_code"] == 0
    lines = ["\n\n---\n### ACTUALLY EXECUTED, interactively (not model output)\n"]
    if result["transcript"].strip():
        lines.append("**session transcript:**\n```\n" + result["transcript"].rstrip() + "\n```\n")
    lines.append(f"**exit code:** {result['exit_code']}, **inputs provided:** {result['exchanges']}\n")
    if result.get("interrupted"):
        lines.append("\n_Stopped at your request._\n")
    elif not passed and final:
        lines.append(
            "\n_This code did not run successfully. You can ask for "
            "another attempt in your next message._\n"
        )
    return "".join(lines)


class Interrupted(Exception):
    """Raised by _call_llama_direct() (and treated equivalently by
    run_sandboxed_interactive()) when a student's own
    POST /sandbox/interrupt stopped this mid-flight. A plain Exception
    subclass, not a special control-flow type -- every existing caller
    already catches Exception generically for "failed to generate" /
    "no input provided", and this reads sensibly through that same
    path; no new handling required at most call sites."""


def _call_llama_direct(messages: list, heartbeat=None, timeout: int = 3600,
                        max_tokens: int | None = None, interrupt=None) -> str:
    """A fresh, non-streaming completion direct to llama-server's own
    internal port -- deliberately never back through this proxy itself
    (would re-enter this same interception logic pointlessly and risks
    recursion). Used only by the Phase 2 fix loop's own follow-up
    requests. `heartbeat`, if given, is called roughly every
    HEARTBEAT_INTERVAL_S while this blocks, to keep the browser's own
    SSE connection alive during a slow internal generation.

    `interrupt`, if given (a threading.Event), is polled every second
    -- independent of HEARTBEAT_INTERVAL_S, so a Stop button feels
    responsive rather than waiting a full heartbeat cycle -- and closes
    the upstream connection the moment it's set, unblocking the
    otherwise-blocking read and raising Interrupted. llama-server
    itself has no cancellation endpoint, so the model's own generation
    keeps computing server-side regardless; this only stops US from
    waiting on/relaying it further, same as an ordinary dropped
    connection already does today.

    `timeout` defaults to a full hour, not a few minutes: this is a
    non-streaming request, so the socket sits waiting for the ENTIRE
    generation to finish before any bytes arrive at all (unlike the
    browser-facing SSE path, which stays alive on its own via
    continuously streamed chunks) -- confirmed live that the previous
    900s default was too short for a real generation on this hardware
    and aborted a genuinely-still-working fix round outright. Also
    caps `max_tokens` to match the original request when known --
    found live that leaving it unset let llama-server fall back to its
    own effectively-unbounded default (n_predict=-1), making a fix
    round's own real duration unpredictable."""
    stop = threading.Event()
    conn_holder: dict = {}
    was_interrupted = threading.Event()

    def _beat():
        elapsed = 0.0
        while True:
            if stop.wait(1.0):
                return
            if interrupt is not None and interrupt.is_set():
                was_interrupted.set()
                c = conn_holder.get("conn")
                if c is not None:
                    try:
                        c.close()
                    except Exception:
                        pass
                return
            elapsed += 1.0
            if heartbeat is not None and elapsed >= HEARTBEAT_INTERVAL_S:
                elapsed = 0.0
                try:
                    heartbeat()
                except Exception:
                    return

    beat_thread = threading.Thread(target=_beat, daemon=True)
    beat_thread.start()
    try:
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=timeout)
        conn_holder["conn"] = conn
        payload = {"messages": messages, "temperature": 0.2, "stream": False}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload).encode()
        conn.request("POST", "/v1/chat/completions", body=body,
                      headers={"Content-Type": "application/json",
                               "Content-Length": str(len(body))})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return data["choices"][0]["message"]["content"]
    except (http.client.HTTPException, OSError):
        if was_interrupted.is_set():
            raise Interrupted("stopped by the student")
        raise
    finally:
        stop.set()
        beat_thread.join(timeout=2)


def _make_input_provider(messages: list, heartbeat, max_tokens, interrupt=None):
    """Returns a provide_input callback for run_sandboxed_interactive():
    each call asks the model, grounded in the REAL transcript so far
    (not a summary), what to supply -- via the same small ```stdin
    fenced-block convention extract_stdin_value() parses, the
    interactive counterpart to extract_python_code(). Returns None
    (stop the session) if the model's reply doesn't contain one -- the
    model choosing not to continue, same shape the fix loop already
    uses for "no runnable code block found" -- and also if `interrupt`
    fires mid-call (Interrupted from _call_llama_direct(), caught here
    like any other failure-to-generate). `messages` is mutated in
    place (appended to) on every call, so a script that asks for
    several separate inputs gets each one grounded in the full real
    conversation so far, not just the original snapshot."""
    def provide_input(transcript_so_far: str) -> str | None:
        prompt = (
            "The script you wrote is now running. Here is everything it "
            f"has printed so far:\n\n```\n{transcript_so_far.strip()}\n```\n\n"
            "It appears to be waiting for input on stdin. If it needs a "
            "value, reply with ONLY a fenced ```stdin block containing "
            "exactly the line to send. If you believe nothing more should "
            "be provided, reply without one."
        )
        messages.append({"role": "user", "content": prompt})
        try:
            reply = _call_llama_direct(messages, heartbeat=heartbeat, max_tokens=max_tokens,
                                        interrupt=interrupt)
        except Exception:
            return None
        messages.append({"role": "assistant", "content": reply})
        return extract_stdin_value(reply)
    return provide_input


def verify_and_maybe_fix(original_messages: list, code: str, heartbeat=None,
                          max_tokens: int | None = None, interrupt=None) -> tuple[str, dict]:
    """Runs the initial sandboxed execution and, if it fails, up to
    VERIFY_MAX_FIX_ROUNDS grounded fix attempts (Phase 2) -- each one
    grounded in the REAL traceback from the attempt before it, not
    another unverified guess. `max_tokens`, when known, is passed
    through to each fix round's own completion so it isn't left
    effectively unbounded.

    Stage 3: every execution (the initial one and any fix-round
    re-execution) runs through run_sandboxed_interactive() rather than
    the one-shot run_sandboxed() -- if the code calls input(), the
    model itself is consulted for what to supply, grounded in the real
    output so far, up to INTERACTIVE_MAX_EXCHANGES times. The plain
    Run button (_handle_sandbox_run) is deliberately untouched by this
    -- per direct decision, only the model-driven Ask flow gets
    interactive stdin, never the student's own direct Run.

    Returns (markdown, capture) -- markdown is the complete text to
    append to the model's own response, unchanged from before this
    return type grew a second element; capture is the same real data
    shaped for Phase 3's learning-corpus record (llm_examples_store's
    own field names): the initial code/result always, plus the LAST
    fix round actually attempted (if any) -- the DB schema holds one
    fix slot, representing where the chain ended up, not every
    intermediate round.

    Stage 4: `interrupt` (a threading.Event), if given, is threaded
    through every sandboxed execution and every internal model call
    below, and checked again before starting each new fix round --
    a student's own POST /sandbox/interrupt stops this at its next
    real check point rather than only between whole turns."""
    messages = list(original_messages)
    messages.append({"role": "assistant", "content": f"```python\n{code}\n```"})

    result = run_sandboxed_interactive(
        code, _make_input_provider(messages, heartbeat, max_tokens, interrupt), interrupt=interrupt)
    capture = {
        "generated_code": code,
        "exec_stdout": result["stdout"], "exec_stderr": result["stderr"],
        "exec_exit_code": result["exit_code"],
        "passed": (not result["timed_out"] and result["exit_code"] == 0),
    }
    if capture["passed"] or VERIFY_MAX_FIX_ROUNDS <= 0:
        return format_interactive_verification_block(result, final=True), capture

    blocks = [format_interactive_verification_block(result, final=False)]

    for round_num in range(1, VERIFY_MAX_FIX_ROUNDS + 1):
        if interrupt is not None and interrupt.is_set():
            blocks.append("\n\n---\n### Stopped at your request\n")
            break

        is_last_round = round_num == VERIFY_MAX_FIX_ROUNDS
        fix_prompt = (
            "This code was executed and failed with the following real "
            f"output:\n\n```\n{(result['stderr'] or result['transcript'] or result['stdout']).strip()}\n```\n\n"
            "Explain exactly what is wrong, quoting the failing line, then "
            "provide a corrected version of the complete script."
        )
        messages.append({"role": "user", "content": fix_prompt})

        try:
            fix_text = _call_llama_direct(messages, heartbeat=heartbeat, max_tokens=max_tokens,
                                           interrupt=interrupt)
        except Exception as e:
            blocks.append(
                f"\n\n---\n### Fix attempt {round_num} of {VERIFY_MAX_FIX_ROUNDS} "
                f"failed to generate ({e})\n"
            )
            break

        messages.append({"role": "assistant", "content": fix_text})
        blocks.append(f"\n\n---\n### Fix attempt {round_num} of {VERIFY_MAX_FIX_ROUNDS}\n\n{fix_text}")
        capture["fix_explanation"] = fix_text

        new_code = extract_python_code(fix_text)
        if not new_code:
            blocks.append(
                "\n\n_No runnable code block found in this fix attempt._\n"
                + ("\n_This code did not run successfully. You can ask for "
                   "another attempt in your next message._\n" if is_last_round else "")
            )
            break

        result = run_sandboxed_interactive(
            new_code, _make_input_provider(messages, heartbeat, max_tokens, interrupt), interrupt=interrupt)
        passed = not result["timed_out"] and result["exit_code"] == 0
        blocks.append(format_interactive_verification_block(result, final=(passed or is_last_round)))
        code = new_code
        capture.update({
            "fixed_code": new_code,
            "fix_exec_stdout": result["stdout"], "fix_exec_stderr": result["stderr"],
            "fix_passed": passed,
        })
        if passed:
            break

    return "".join(blocks), capture


def _extract_prompt(original_messages: list) -> str:
    """The most recent user-role message -- what the student actually
    asked in this turn, not the whole running conversation."""
    for msg in reversed(original_messages or []):
        if msg.get("role") == "user":
            return msg.get("content") or ""
    return ""


def capture_example(original_messages: list, capture: dict,
                     source: str = "llm-chat-coordinator") -> None:
    """POSTs one grounded-verification transaction back to the CloudCore
    API's examples-capture endpoint (api/llm_examples_routes.py). Fully
    best-effort -- any failure (capture disabled, host unreachable,
    non-2xx) is swallowed after one stderr line for journald, since a
    capture problem must never affect the chat response itself.
    `source` distinguishes the sandbox's own on-demand Ask transactions
    ("llm-chat-sandbox") from the default chat-originated ones -- see
    do_POST's /sandbox/ask branch."""
    if not EXAMPLES_API_BASE or not EXAMPLES_MODEL_FILENAME:
        return
    payload = {
        "source": source,
        "model_filename": EXAMPLES_MODEL_FILENAME,
        "prompt": _extract_prompt(original_messages),
        **capture,
    }
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            EXAMPLES_API_BASE + "/v1/llm-chat/examples", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {EXAMPLES_API_TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"verify-proxy: example capture failed (non-fatal): {e}", flush=True)


def register_llm_deployment() -> None:
    """One-shot, best-effort self-registration with the CloudCore
    Dashboard's LLM Performance page (api/llm_deployments_routes.py) --
    same reasoning and wiring as capture_example() above. Skipped
    entirely if DEPLOYMENT_NAME is unset, same as capture_example skips
    when EXAMPLES_MODEL_FILENAME is unset -- lets this same proxy source
    run against an older API host/template with no llm-deployments route
    at all. Registration is by name, not by CloudCore instance id -- this
    guest has no way to know the id the API assigned it (assigned only
    after apply, long after this cloud-init template was rendered); the
    API resolves name -> current instance -> current private_ip itself
    at poll time (store.find_instance_by_name), the same trust boundary
    api/lb.py already relies on rather than a self-reported address."""
    if not EXAMPLES_API_BASE or not DEPLOYMENT_NAME:
        return
    payload = {
        "name": DEPLOYMENT_NAME,
        "example": "llm-chat",
        "port": LISTEN_PORT,
        "stats_path": "/llm-stats",
    }
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            EXAMPLES_API_BASE + "/v1/llm-deployments/register", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {EXAMPLES_API_TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"verify-proxy: LLM deployment registration failed (non-fatal): {e}", flush=True)


# CloudCore Dashboard -- LLM Performance page's live "right now" stats
# (GET /llm-stats below). Aggregate counters only, in-memory, reset on
# restart -- this is a live snapshot the Dashboard polls, not a logged
# time series (api/scheduler.py's llm_ingestions table already owns
# historical run-by-run numbers for the separate ingest-schedule case).
_llm_stats_lock = threading.Lock()
_llm_stats = {
    "model": None,
    "requests_served": 0,
    "completion_tokens_total": 0,
    "last_tokens_per_second": None,
    "avg_tokens_per_second": None,
    "last_request_at": None,
}
_LLM_STATS_STARTED_AT = time.time()


def _record_llm_stats(model: str, timings: dict) -> None:
    """Called once per completed /sandbox/ask turn, from
    _relay_and_verify_stream's own tail -- see its call site below.
    `timings` is llama-server's own OpenAI-compatible-extension block,
    present on the final streamed chunk of each response."""
    tokens = timings.get("predicted_n")
    tps = timings.get("predicted_per_second")
    with _llm_stats_lock:
        _llm_stats["requests_served"] += 1
        if tokens:
            _llm_stats["completion_tokens_total"] += tokens
        if tps:
            n = _llm_stats["requests_served"]
            prev_avg = _llm_stats["avg_tokens_per_second"]
            _llm_stats["last_tokens_per_second"] = tps
            _llm_stats["avg_tokens_per_second"] = tps if prev_avg is None else (prev_avg * (n - 1) + tps) / n
        if model:
            _llm_stats["model"] = model
        _llm_stats["last_request_at"] = time.time()


def _llm_stats_snapshot() -> dict:
    with _llm_stats_lock:
        snap = dict(_llm_stats)
    snap["uptime_seconds"] = round(time.time() - _LLM_STATS_STARTED_AT, 1)
    return snap


def _log_ask_outcome(endpoint_label: str, outcome: str, duration_s: float,
                      question_chars: int = 0, answer_chars: int = 0,
                      continuation_rounds: int = 0, tokens=None, tps=None) -> None:
    """One structured line per ask request, whatever happened to it --
    direct request, prompted by a real incident (F-119/F-122 and
    friends) where the only way to understand a student's own report
    after the fact was live SSH into the coordinator mid-investigation.
    _record_llm_stats() above only ever sees the SUCCESS case (it's fed
    from llama-server's own timings block, which only exists on a
    genuine completion) -- this covers every outcome, including the
    ones that matter most for understanding a real failure: stalled,
    disconnected, interrupted, upstream_unreachable, and
    length_exhausted (hit MAX_CONTINUATION_ROUNDS without ever landing
    a real "stop"). Plain print(..., flush=True) to stdout, same as
    every other diagnostic line in this file -- captured by
    verify-proxy.service's own journal (`journalctl -u verify-proxy`),
    no new log file or service to manage. JSON, not a hand-rolled
    key=value format, so a future analysis pass can just
    json.loads() each ASK_OUTCOME line rather than write a parser."""
    entry = {
        "event": "ask_outcome", "endpoint": endpoint_label, "outcome": outcome,
        "duration_s": round(duration_s, 2), "question_chars": question_chars,
        "answer_chars": answer_chars, "continuation_rounds": continuation_rounds,
        "tokens": tokens, "tokens_per_second": tps,
    }
    print(f"ASK_OUTCOME {json.dumps(entry)}", flush=True)


# Phase 4 -- the interactive sandbox itself. Stdlib-rendered, no new
# frontend framework, matching /examples' own convention. Plain
# <textarea> for Stage 1 (see the Interactive Sandbox phased-
# implementation doc's own Stage 1/2 split). Stage 2: CodeMirror 5,
# same version already vendored for the Dashboard's own Editor page
# (ui/vendor/), shipped into THIS guest's own cloud-init instead (the
# dashboard's /vendor/ route serves the admin host, not this
# student-facing coordinator -- see GET /vendor/<name> below) and
# served from local files, never a CDN, same offline-capable
# convention this whole project already holds to.
# All state (code buffer, ask conversation) lives client-side in
# localStorage -- no server-side student identity, matching Phase 3's
# own privacy stance. Every fetch to /sandbox/run or /sandbox/ask
# disables its own button while in flight -- the simplest real abuse
# mitigation for Stage 1 (see the doc's own "Abuse/rate consideration").
SANDBOX_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llm-chat -- Sandbox</title>
<link rel="stylesheet" href="/vendor/codemirror.min.css">
<link rel="stylesheet" href="/vendor/codemirror-theme-dracula.min.css">
<link rel="stylesheet" href="/vendor/xterm.min.css">
<script src="/vendor/codemirror.min.js"></script>
<script src="/vendor/codemirror-mode-python.min.js"></script>
<script src="/vendor/codemirror-addon-matchbrackets.min.js"></script>
<script src="/vendor/xterm.min.js"></script>
<script src="/vendor/xterm-addon-fit.min.js"></script>
<style>
/* Deliberately no `color-scheme: light dark` -- found live that
   declaring it without actually authoring a dark palette let the
   browser paint a dark background under this page's own fixed dark
   text colors whenever the OS/browser was in dark mode, making most
   of the page barely legible without selecting it. Forcing a plain,
   guaranteed-white background sidesteps that entirely; darkened the
   muted grays below a bit further too, for real margin either way. */
body { font-family: system-ui, sans-serif; max-width: 1000px; margin: 1.5rem auto; padding: 0 1rem; color: #1a1a1a; background: #fff; }
h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
.sub { color: #444; font-size: 0.85rem; margin: 0 0 1.25rem; }
.panel { border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1.25rem; }
.panel h2 { font-size: 1rem; margin: 0 0 0.75rem; }
#codeHost { border: 1px solid #ccc; border-radius: 6px; overflow: hidden; }
#codeHost .CodeMirror { height: 320px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.9rem; }
.row { display: flex; gap: 0.6rem; align-items: center; margin-top: 0.75rem; flex-wrap: wrap; }
button { font: inherit; padding: 0.45rem 1rem; border-radius: 6px; border: 1px solid #999; background: #f2f2f2; cursor: pointer; color: #1a1a1a; }
button:hover:not(:disabled) { background: #e8e8e8; }
button:disabled { opacity: 0.5; cursor: default; }
button.primary { background: #2a5db0; border-color: #2a5db0; color: #fff; }
button.primary:hover:not(:disabled) { background: #234f96; }
.status { font-size: 0.85rem; color: #444; }
.status.warn { color: #b02a2a; font-weight: 600; }
.code-block { margin: 0.4rem 0; }
.code-block pre { margin: 0 0 0.3rem; }
.use-code-btn { font-size: 0.78rem; padding: 0.25rem 0.6rem; }
pre { background: #f6f6f6; border-radius: 4px; padding: 0.6rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; margin: 0.5rem 0 0; color: #1a1a1a; }
.result h4 { margin: 0.75rem 0 0.25rem; font-size: 0.85rem; }
.result.pass .exitline { color: #0a7a2f; font-weight: 600; }
.result.fail .exitline { color: #b02a2a; font-weight: 600; }
#transcript { display: flex; flex-direction: column; gap: 0.75rem; max-height: 420px; overflow-y: auto; padding: 0.25rem 0; }
.msg { border-radius: 6px; padding: 0.5rem 0.75rem; }
.msg.student { background: #eef3fb; }
.msg.model { background: #f6f6f6; }
.msg .who { font-size: 0.75rem; color: #555; margin-bottom: 0.25rem; text-transform: uppercase; letter-spacing: 0.03em; }
.msg .content { white-space: pre-wrap; word-break: break-word; font-size: 0.9rem; color: #1a1a1a; }
.msg .content.thinking { color: #666; font-style: italic; animation: bm-pulse 1.4s ease-in-out infinite; }
@keyframes bm-pulse { 0%, 100% { opacity: 0.4; } 50% { opacity: 1; } }
#question { flex: 1; min-width: 200px; font: inherit; padding: 0.45rem 0.6rem; border: 1px solid #ccc; border-radius: 6px; color: #1a1a1a; }
#linuxQuestion { flex: 1; min-width: 200px; font: inherit; padding: 0.45rem 0.6rem; border: 1px solid #ccc; border-radius: 6px; color: #1a1a1a; resize: vertical; line-height: 1.4; }
#termHost { border: 1px solid #ccc; border-radius: 6px; overflow: hidden; background: #0d0d0d; padding: 0.4rem; display: none; }
#termHost.open { display: block; }
#termHost .xterm { height: 360px; }
#previewPortRow button.primary { background: #2a5db0; border-color: #2a5db0; color: #fff; }
#previewHost { border: 1px solid #ccc; border-radius: 6px; overflow: hidden; background: #fff; margin-top: 0.6rem; }
#previewFrame { width: 100%; height: 420px; border: 0; display: block; }
#previewOpenLink { font-size: 0.85rem; color: #2a5db0; }
footer { margin-top: 1.5rem; font-size: 0.8rem; color: #555; }
footer a { color: #2a5db0; }
</style></head>
<body>
<h1>Sandbox</h1>
<p class="sub">Write real Python, run it for real, and ask the model about it -- every response you get back is grounded in an actual execution, not just the model's own word for it.</p>

<div class="panel">
  <h2>Your code</h2>
  <div id="codeHost"></div>
  <div class="row">
    <button id="runBtn" class="primary" onclick="runCode()">Run</button>
    <button onclick="clearAll()">Clear session</button>
    <button id="sendToTermBtn" onclick="sendCodeToTerminal()" disabled title="Start a terminal below first">Send to Terminal</button>
    <span id="runStatus" class="status"></span>
  </div>
  <div id="runResult"></div>
</div>

<div class="panel">
  <h2>Ask the model</h2>
  <p class="sub" style="margin-bottom:0.75rem">Leave the code box empty to ask for something new to be written, or ask about the code above to get it explained or fixed. Either way, the answer is always re-run for real before you see it.</p>
  <div id="transcript"></div>
  <div class="row">
    <input id="question" type="text" placeholder="e.g. write a function that checks if a number is prime — or: why does this fail on an empty list?" onkeydown="if(event.key==='Enter')codeAsk.askModel()">
    <button id="askBtn" class="primary" onclick="codeAsk.askModel()">Ask</button>
    <button id="stopBtn" onclick="codeAsk.stopAsk()" disabled>Stop</button>
    <button id="regenBtn" onclick="codeAsk.regenerateAsk()" disabled title="Ask again with no changes">Regenerate</button>
    <span id="askStatus" class="status"></span>
  </div>
</div>

<div class="panel">
  <h2>Terminal</h2>
  <p class="sub" style="margin-bottom:0.75rem">A real, isolated Linux shell with genuine internet access -- separate from the sandbox above, so <code>pip install</code>, <code>curl</code>, and anything else you'd do on a normal machine all work for real. It can't reach anything except the internet: not this lab, not other students, nothing else on the network. Closes automatically after a period of inactivity.__PREVIEW_PORTS_HINT__</p>
  <div class="row">
    <button id="termStartBtn" class="primary" onclick="startTerminal()">Start terminal</button>
    <button id="termStopBtn" onclick="stopTerminal()" disabled>Disconnect</button>
    <button id="termRestartBtn" onclick="restartTerminal()" style="display:none">Start a fresh session</button>
    <span id="termStatus" class="status"></span>
  </div>
  <div id="termHost"></div>
</div>

<div class="panel">
  <h2>Linux Help</h2>
  <p class="sub" style="margin-bottom:0.75rem">Ask any Linux question -- from everyday commands to real system administration -- kept separate from the coding Ask panel above. Answers aren't automatically run or checked the way code is; use the "Run in Terminal" button on a suggested command to actually try it in the Terminal panel and see the real result.</p>
  <div id="linuxTranscript"></div>
  <div class="row" style="align-items:flex-end">
    <textarea id="linuxQuestion" rows="4" placeholder="e.g. how do I check disk usage? -- or: how do I add a new user? (Shift+Enter for a new line)" onkeydown="if(event.key==='Enter' && !event.shiftKey){event.preventDefault();linuxAsk.askModel();}"></textarea>
    <button id="linuxAskBtn" class="primary" onclick="linuxAsk.askModel()">Ask</button>
    <button id="linuxStopBtn" onclick="linuxAsk.stopAsk()" disabled>Stop</button>
    <button id="linuxRegenBtn" onclick="linuxAsk.regenerateAsk()" disabled title="Ask again with no changes">Regenerate</button>
    <span id="linuxAskStatus" class="status"></span>
  </div>
</div>

<div class="panel">
  <h2>Preview</h2>
  <p class="sub" style="margin-bottom:0.75rem">View whatever your own program is serving on one of the Terminal's ports, right here -- no need to open a new tab yourself. If a page doesn't render (some apps refuse to be embedded), use "Open in new tab" instead; either way it's reaching the exact same thing.</p>
  <div class="row" id="previewPortRow"></div>
  <div class="row" style="margin-top:0.5rem">
    <button onclick="refreshPreview()">Refresh</button>
    <a id="previewOpenLink" href="#" target="_blank" rel="noopener">Open in new tab &#8599;</a>
  </div>
  <div id="previewHost"><iframe id="previewFrame"></iframe></div>
</div>

<footer>Published examples from sessions like this one: <a href="/examples">/examples</a></footer>

<script>
const CODE_KEY = 'sandboxCode';

// CodeMirror(host, {...}), not .fromTextArea() -- same init pattern
// the Dashboard's own Editor page already uses (ui/src/js/18-editor.js).
const cm = CodeMirror(document.getElementById('codeHost'), {
  mode: 'python', theme: 'dracula', lineNumbers: true, matchBrackets: true,
  indentUnit: 4, tabSize: 4, viewportMargin: Infinity,
});
cm.on('change', saveCode);

function saveCode() { localStorage.setItem(CODE_KEY, cm.getValue()); }

// Stage 8 -- both the coding "Ask the model" panel and the new
// "Linux Help" panel share this exact same shape: their own history
// key, their own transcript/status/buttons, streaming from the model
// via SSE, and rendering the model's fenced code blocks with a
// per-panel action button on each one. Built once here, instantiated
// twice below (codeAsk/linuxAsk) with only the real differences
// (endpoint, request body, what a code block's own button does)
// passed in as config -- rather than two ~150-line near-duplicates
// that would only drift apart over time.
function makeAskPanel(cfg) {
  const transcriptEl = document.getElementById(cfg.transcriptId);
  const questionEl = document.getElementById(cfg.questionId);
  const askBtn = document.getElementById(cfg.askBtnId);
  const stopBtn = document.getElementById(cfg.stopBtnId);
  const regenBtn = document.getElementById(cfg.regenBtnId);
  const statusEl = document.getElementById(cfg.statusId);

  function getHistory() {
    try { return JSON.parse(localStorage.getItem(cfg.historyKey) || '[]'); }
    catch (e) { return []; }
  }
  function saveHistory(h) { localStorage.setItem(cfg.historyKey, JSON.stringify(h)); }

  // Splits a fenced triple-backtick code block out of the model's own
  // plain text and gives it a real, per-panel action button -- still
  // never innerHTML'd from the model's own words (every text/code
  // fragment below goes in via createTextNode/.textContent, same
  // no-markup-from-untrusted-text rule renderTranscript() below also
  // follows), just structured instead of one flat blob. Only applied
  // to the model's own messages -- a student's own submitted question
  // has nothing to act on.
  function renderAssistantContent(el, text) {
    el.innerHTML = '';
    const parts = text.split(/```[a-zA-Z0-9_+-]*\\n?([\\s\\S]*?)```/);
    parts.forEach((part, i) => {
      if (i % 2 === 0) {
        if (part) el.appendChild(document.createTextNode(part));
        return;
      }
      const wrap = document.createElement('div');
      wrap.className = 'code-block';
      const pre = document.createElement('pre');
      pre.textContent = part;
      const btn = document.createElement('button');
      btn.className = 'use-code-btn';
      btn.textContent = cfg.codeBlockLabel;
      btn.onclick = () => cfg.onCodeBlock(part, statusEl);
      wrap.appendChild(pre);
      wrap.appendChild(btn);
      el.appendChild(wrap);
    });
  }

  // Regenerate is only meaningful once at least one question has been
  // asked -- checked against saved history (not just "did a request
  // just finish") so a returning student (page reload, history
  // restored from localStorage) and a post-Clear student both see the
  // right state without needing to ask a fresh question first.
  function updateRegenBtnState() {
    const h = getHistory();
    regenBtn.disabled = !h.some(m => m.role === 'user');
  }

  function renderTranscript() {
    const h = getHistory();
    transcriptEl.innerHTML = h.map(m => `
      <div class="msg ${m.role === 'user' ? 'student' : 'model'}">
        <div class="who">${m.role === 'user' ? 'You' : 'Model'}</div>
        <div class="content"></div>
      </div>`).join('');
    [...transcriptEl.children].forEach((el, i) => {
      const contentEl = el.querySelector('.content');
      if (h[i].role === 'user') {
        // textContent, not innerHTML -- never trust/render student text
        // as markup either.
        contentEl.textContent = h[i].content;
      } else {
        renderAssistantContent(contentEl, h[i].content);
      }
    });
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    updateRegenBtnState();
  }

  // Started only after a real "someone else is asking" rejection (503
  // -- see _send_busy()'s own comment on the Python side) -- polls the
  // trivial, instant /sandbox/ask-status endpoint (no LLM call at all)
  // every few seconds until the coordinator reports free, then tells
  // the student directly via statusEl rather than leaving them to
  // guess-and-retry. A generous overall cap, not an unbounded
  // background poll forever, in case something stays wedged well past
  // this platform's own stall-recovery window.
  let availabilityPollTimer = null;

  function stopAvailabilityPoll() {
    if (availabilityPollTimer) {
      clearInterval(availabilityPollTimer);
      availabilityPollTimer = null;
    }
  }

  function startAvailabilityPoll() {
    stopAvailabilityPoll();
    const deadline = Date.now() + 10 * 60 * 1000;
    availabilityPollTimer = setInterval(async () => {
      if (Date.now() > deadline) { stopAvailabilityPoll(); return; }
      try {
        const r = await fetch('/sandbox/ask-status');
        const data = await r.json();
        if (!data.busy) {
          stopAvailabilityPoll();
          statusEl.textContent = 'Available again -- you can ask your question now.';
        }
      } catch (e) { /* transient network hiccup -- keep polling */ }
    }, 4000);
  }

  function clearHistory() {
    localStorage.removeItem(cfg.historyKey);
    renderTranscript();
  }

  async function stopAsk() {
    // Best-effort -- see _handle_ask()'s own docstring: this stops US
    // from waiting on/relaying the response further, not the model's
    // own generation on the coordinator, which has no cancellation
    // endpoint. The streamed response itself (awaited in runAsk()
    // below) is what reports whether it actually landed. Shared
    // /sandbox/interrupt across both panels -- a student only ever has
    // one live question in flight regardless of which one asked it.
    stopBtn.disabled = true;
    try { await fetch('/sandbox/interrupt', {method: 'POST'}); } catch (e) { /* best-effort */ }
  }

  // Shared by askModel() (reads the textarea) and Stage 9's own
  // automatic fault-diagnosis message (system-constructed, never
  // touches the textarea at all) -- both are just "ask this question",
  // the only difference is where the text comes from.
  async function askWithText(question) {
    if (!question) return;
    const history = getHistory();
    history.push({role: 'user', content: question});
    saveHistory(history);
    renderTranscript();
    await runAsk(question, history.slice(0, -1));
  }

  async function askModel() {
    const question = questionEl.value.trim();
    if (!question) return;
    questionEl.value = '';
    await askWithText(question);
  }

  // Re-asks the same last question with no changes. Doesn't touch
  // history's own last user entry -- context passed to the model
  // (history.slice(0, lastUserIdx), same as a first ask) is identical
  // either way, and the new attempt is appended alongside the old one,
  // not replacing it, so a student can compare rather than silently
  // lose the previous answer.
  async function regenerateAsk() {
    const history = getHistory();
    // Find the most recent question, not just the last entry -- by the
    // time this button is enabled, history normally already ends with
    // that question's own assistant reply.
    let lastUserIdx = -1;
    for (let i = history.length - 1; i >= 0; i--) {
      if (history[i].role === 'user') { lastUserIdx = i; break; }
    }
    if (lastUserIdx === -1) {
      statusEl.textContent = 'Ask a question first.';
      return;
    }
    await runAsk(history[lastUserIdx].content, history.slice(0, lastUserIdx));
  }

  async function runAsk(question, contextHistory) {
    askBtn.disabled = true;
    regenBtn.disabled = true;
    stopBtn.disabled = false;
    statusEl.textContent = 'Thinking…';

    // Placeholder model bubble, filled in as tokens stream -- textContent
    // only, same no-markup-from-untrusted-text rule as renderTranscript().
    // Starts showing "Thinking..." (pulsing, via the .thinking class) so
    // a long real wait before the first token arrives doesn't look like
    // the page has just frozen -- found live this needed to be explicit,
    // an empty bubble alone wasn't enough of a signal.
    const bubble = document.createElement('div');
    bubble.className = 'msg model';
    bubble.innerHTML = '<div class="who">Model</div><div class="content thinking">Thinking…</div>';
    transcriptEl.appendChild(bubble);
    const bubbleContent = bubble.querySelector('.content');
    transcriptEl.scrollTop = transcriptEl.scrollHeight;

    let assistantText = '';
    try {
      const resp = await fetch(cfg.endpoint, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(Object.assign(cfg.buildBody(), {question, history: contextHistory})),
      });
      if (!resp.ok || !resp.body) {
        // 429 (rate limit or "already have a question in progress") and
        // 503 (someone ELSE is currently asking -- see _send_busy()'s
        // own comment) both come back as plain text with the real,
        // useful reason -- show that instead of just the bare status
        // code. 503 specifically starts polling for availability so
        // the student is told the moment it's actually worth retrying,
        // rather than left to guess-and-spam Ask themselves.
        bubbleContent.classList.remove('thinking');
        bubbleContent.textContent = (resp.status === 429 || resp.status === 503)
          ? await resp.text() : 'Request failed (' + resp.status + ')';
        if (resp.status === 503) startAvailabilityPoll();
      } else {
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        // F-133/F-134: a mid-stream connection drop (the backend VM
        // itself dying, not just a network blip) makes reader.read()
        // resolve with done:true -- indistinguishable, at this level,
        // from a real, complete response. The server always writes a
        // final "data: [DONE]" once it's genuinely finished (see
        // _do_handle_ask's own last line, common to every stop_reason
        // branch including its own "stalled"/"empty_response" messages)
        // -- sawDone tracks whether that sentinel actually arrived, so
        // a truncated answer can be told apart from a real one instead
        // of silently being shown as if it were complete.
        let sawDone = false;
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          buf += decoder.decode(value, {stream: true});
          const events = buf.split('\\n\\n');
          buf = events.pop();
          for (const evt of events) {
            if (evt.startsWith(':')) {
              // A heartbeat comment (": verifying...") -- sent while a
              // real sandboxed re-execution or grounded fix round is
              // running server-side, after the model's own text has
              // already fully arrived. No new content to show, but this
              // is the other place a real wait needs a visible signal.
              statusEl.textContent = 'Verifying…';
              continue;
            }
            const line = evt.split('\\n').find(l => l.startsWith('data: '));
            if (!line) continue;
            const payload = line.slice(6);
            if (payload === '[DONE]') { sawDone = true; continue; }
            try {
              const obj = JSON.parse(payload);
              const delta = (obj.choices[0].delta || {}).content || '';
              if (delta) {
                assistantText += delta;
                bubbleContent.classList.remove('thinking');
                bubbleContent.textContent = assistantText;
                transcriptEl.scrollTop = transcriptEl.scrollHeight;
                statusEl.textContent = 'Thinking…';
              }
            } catch (e) { /* skip malformed lines */ }
          }
        }
        if (!sawDone) {
          bubbleContent.classList.remove('thinking');
          assistantText += '\\n\\n---\\n_Connection to the model backend was lost before ' +
            'this answer finished -- the text above may be incomplete. Please try again._';
          bubbleContent.textContent = assistantText;
        }
      }
    } catch (e) {
      bubbleContent.classList.remove('thinking');
      bubbleContent.textContent = 'Request failed: ' + e.message;
    } finally {
      askBtn.disabled = false;
      regenBtn.disabled = false;
      stopBtn.disabled = true;
      statusEl.textContent = '';
    }

    const h = getHistory();
    h.push({role: 'assistant', content: assistantText || bubbleContent.textContent});
    saveHistory(h);
    // Re-render from the now-saved history -- turns the plain streamed
    // text just shown above into the same structured, button-equipped
    // form renderTranscript() gives every other message (and what a page
    // reload would show anyway), so the code-block button appears
    // without needing a refresh.
    renderTranscript();
  }

  return {askModel, askWithText, regenerateAsk, stopAsk, renderTranscript, clearHistory};
}

const codeAsk = makeAskPanel({
  historyKey: 'sandboxHistory', endpoint: '/sandbox/ask',
  transcriptId: 'transcript', questionId: 'question', askBtnId: 'askBtn',
  stopBtnId: 'stopBtn', regenBtnId: 'regenBtn', statusId: 'askStatus',
  codeBlockLabel: 'Use this code',
  buildBody: () => ({code: cm.getValue()}),
  onCodeBlock: (part) => {
    cm.setValue(part);
    const rs = document.getElementById('runStatus');
    rs.textContent = 'Loaded from Ask.';
    setTimeout(() => { if (rs.textContent === 'Loaded from Ask.') rs.textContent = ''; }, 2000);
  },
});

// Stage 8 -- the Linux Help panel, genuinely separate from codeAsk
// above: its own endpoint/history, no "current code" in the request
// body, and its own code-block action (run the suggested command in
// the live Terminal, rather than load it into the Python editor).
const linuxAsk = makeAskPanel({
  historyKey: 'linuxHistory', endpoint: '/sandbox/linux-ask',
  transcriptId: 'linuxTranscript', questionId: 'linuxQuestion', askBtnId: 'linuxAskBtn',
  stopBtnId: 'linuxStopBtn', regenBtnId: 'linuxRegenBtn', statusId: 'linuxAskStatus',
  codeBlockLabel: 'Run in Terminal',
  buildBody: () => ({}),
  onCodeBlock: (part, statusEl) => runCommandInTerminal(part, statusEl),
});

function loadState() {
  cm.setValue(localStorage.getItem(CODE_KEY) || '');
  codeAsk.renderTranscript();
  linuxAsk.renderTranscript();
}

function clearAll() {
  // Only clears the code editor and its own Ask conversation -- the
  // Linux Help panel is a deliberately separate, unrelated
  // conversation (that's the whole point of keeping them apart), so
  // clearing a Python session shouldn't silently wipe it too.
  if (!confirm('Clear your code and conversation? This only affects this browser.')) return;
  localStorage.removeItem(CODE_KEY);
  codeAsk.clearHistory();
  cm.setValue('');
  document.getElementById('runResult').innerHTML = '';
}

async function runCode() {
  const btn = document.getElementById('runBtn');
  const status = document.getElementById('runStatus');
  const out = document.getElementById('runResult');
  if (!cm.getValue().trim()) return;
  btn.disabled = true;
  status.textContent = 'Running...';
  out.innerHTML = '';
  try {
    const resp = await fetch('/sandbox/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: cm.getValue()}),
    });
    if (resp.status === 429) {
      // Rate-limit/concurrency responses are plain text, not JSON --
      // parsing them as JSON below would throw and mask the real,
      // useful message (e.g. "limit is 10 per minute") behind a
      // generic "Request failed" from the outer catch.
      status.textContent = await resp.text();
      return;
    }
    const data = await resp.json();
    if (!resp.ok) { status.textContent = 'Error: ' + (data.error || resp.status); return; }
    const passed = !data.timed_out && data.exit_code === 0;
    status.textContent = '';
    const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
    out.innerHTML = `<div class="result ${passed ? 'pass' : 'fail'}">
      ${data.stdout.trim() ? `<h4>stdout</h4><pre>${esc(data.stdout)}</pre>` : ''}
      ${data.stderr.trim() ? `<h4>stderr</h4><pre>${esc(data.stderr)}</pre>` : ''}
      <p class="exitline">exit code: ${data.exit_code}${data.timed_out ? ' (timed out)' : ''}</p>
    </div>`;
  } catch (e) {
    status.textContent = 'Request failed: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

// ── Terminal panel ──────────────────────────────────────────────────
// Same xterm.js + WS<->PTY wiring shape as the Dashboard's own admin
// Terminal feature (ui/src/js/11-terminal.js) -- one fixed panel here
// rather than that page's multi-window instance picker, since there's
// only ever one sandbox terminal per student session.
let termState = null;  // { ws, term, fitAddon }

function startTerminal() {
  if (termState) return;
  const startBtn = document.getElementById('termStartBtn');
  const stopBtn = document.getElementById('termStopBtn');
  const status = document.getElementById('termStatus');
  const host = document.getElementById('termHost');

  startBtn.disabled = true;
  host.classList.add('open');
  status.textContent = 'Booting a fresh sandboxed shell...';

  const term = new Terminal({
    theme: { background: '#0d0d0d', foreground: '#e2e6f0', cursor: '#4f8ef7' },
    fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace",
    fontSize: 13,
    cursorBlink: true,
    scrollback: 2000,
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(host);
  fitAddon.fit();

  // Reached through the SAME load balancer the page itself was loaded
  // through (path-routed to sandbox_terminal.py's own service, a
  // separate backend port -- see main.tf's own routing_rules) --
  // deliberately window.location.host, not a hardcoded address, so
  // this works the same whether the page was reached via the real LB
  // or a local port-forward used for testing.
  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${wsProto}//${location.host}/terminal`);
  termState = {ws, term, fitAddon};

  ws.onopen = () => {
    const {cols, rows} = term;
    ws.send(JSON.stringify({type: 'resize', cols, rows}));
    stopBtn.disabled = false;
    document.getElementById('sendToTermBtn').disabled = false;
  };

  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'output' || msg.type === 'connected') {
        status.classList.remove('warn');
        status.textContent = '';
        // Real output arriving is proof the shell isn't actually stuck
        // (even if sandbox_terminal.py already warned once) -- hide any
        // earlier "may be stuck" hint rather than leave it lingering.
        document.getElementById('termRestartBtn').style.display = 'none';
        term.write(msg.data);
      } else if (msg.type === 'error') {
        term.write('\\r\\n\\x1b[31m' + msg.data + '\\x1b[0m\\r\\n');
        status.classList.remove('warn');
        status.textContent = msg.data;
      } else if (msg.type === 'warning') {
        // Sandbox_terminal.py's own idle/max-session countdown -- a
        // real heads-up before the session just vanishes, not just
        // terminal output a student could easily miss scrolling past.
        status.classList.add('warn');
        status.textContent = msg.data;
      } else if (msg.type === 'unresponsive') {
        // sandbox_terminal.py's own "input sent, nothing came back"
        // signal -- a real command that's just slow looks identical
        // from this signal alone, so this is offered as an option, not
        // forced: the student can keep waiting, or click through to a
        // guaranteed-fresh microVM without needing to know that's what
        // "Disconnect" + "Start terminal" together would also do.
        status.classList.add('warn');
        status.textContent = msg.data;
        document.getElementById('termRestartBtn').style.display = '';
      }
    } catch (e) { /* skip malformed frames */ }
  };

  ws.onclose = () => {
    term.write('\\r\\n\\x1b[33m[Session closed]\\x1b[0m\\r\\n');
    stopBtn.disabled = true;
    startBtn.disabled = false;
    document.getElementById('sendToTermBtn').disabled = true;
    document.getElementById('termRestartBtn').style.display = 'none';
    status.classList.remove('warn');
  };

  ws.onerror = () => {
    status.textContent = 'Connection error.';
  };

  term.onData(data => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({type: 'input', data}));
    }
  });

  const ro = new ResizeObserver(() => {
    fitAddon.fit();
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({type: 'resize', cols: term.cols, rows: term.rows}));
    }
  });
  ro.observe(host);
  termState.ro = ro;
}

function stopTerminal() {
  if (!termState) return;
  termState.ws.close();
  termState.term.dispose();
  termState.ro.disconnect();
  document.getElementById('termHost').innerHTML = '';
  document.getElementById('termHost').classList.remove('open');
  document.getElementById('termStartBtn').disabled = false;
  document.getElementById('termStopBtn').disabled = true;
  document.getElementById('sendToTermBtn').disabled = true;
  document.getElementById('termRestartBtn').style.display = 'none';
  termState = null;
}

// Tears down whatever's there (presumed dead or genuinely unresponsive
// -- stopTerminal() only ever does a client-side ws.close(), which
// doesn't depend on the guest responding at all, and the server's own
// teardown() SIGKILLs the VM process if it doesn't exit cleanly) and
// immediately starts a completely fresh microVM. One click instead of
// the student needing to know that Disconnect + Start terminal
// together is what actually recovers from a stuck session.
function restartTerminal() {
  stopTerminal();
  startTerminal();
}

// Writes the editor's current content into the terminal session as a
// real file, without retyping it -- base64, not the raw text, since
// this goes over the exact same channel as real keystrokes (term.onData
// above) and the editor's own content can contain anything (quotes,
// backticks, $, newlines) that would otherwise need the same class of
// careful shell-escaping this project's own build scripts have
// repeatedly gotten wrong live this session. base64's alphabet has none
// of those characters, so a quoted heredoc (no $/backtick expansion)
// can carry it with zero escaping at all.
function sendCodeToTerminal() {
  if (!termState || termState.ws.readyState !== WebSocket.OPEN) return;
  const code = cm.getValue();
  if (!code.trim()) return;
  const b64 = btoa(unescape(encodeURIComponent(code)));
  const cmd = `base64 -d <<'CLOUDCORE_EOF' > sandbox_code.py\n${b64}\nCLOUDCORE_EOF\n`;
  termState.ws.send(JSON.stringify({type: 'input', data: cmd}));
  const rs = document.getElementById('runStatus');
  rs.textContent = 'Sent to Terminal as sandbox_code.py.';
  setTimeout(() => { if (rs.textContent === 'Sent to Terminal as sandbox_code.py.') rs.textContent = ''; }, 3000);
}

// Stage 8 -- runs a Linux Help code block's own suggested command(s)
// directly in the live terminal session, exactly as if typed there.
// Deliberately NOT the same base64-into-a-file mechanism as
// sendCodeToTerminal() above -- that one exists so python3 can run the
// result afterward, and needs the shell to NOT interpret its content.
// Here the whole point IS for the shell to interpret $vars/backticks/
// etc. normally -- that's what "running a command" means -- so only
// the OUTER interactive shell needs protecting from expanding the
// heredoc body early, which a quoted heredoc (<<'DELIM') already does
// for arbitrary content, same as the encoded approach guards against
// shell-special characters elsewhere on this page. The delimiter
// carries a random suffix rather than a fixed literal -- this panel's
// own answers can plausibly include a heredoc example of their own
// using a plain "EOF"-style name, which a fixed delimiter could
// collide with.
function runCommandInTerminal(code, statusEl) {
  if (!code.trim()) return;
  if (!termState || termState.ws.readyState !== WebSocket.OPEN) {
    if (statusEl) statusEl.textContent = 'Start a terminal below first.';
    return;
  }
  const delim = 'CLOUDCORE_EOF_' + Math.random().toString(36).slice(2, 10);
  // Stage 9 -- a second, separate random-suffixed marker (same
  // collision reasoning as the heredoc delimiter above), echoed by the
  // OUTER interactive shell immediately after the heredoc-fed bash
  // invocation finishes, so $? genuinely reflects that invocation's
  // real exit status -- lets _captureAndDiagnose() below learn the
  // real result without guessing.
  const marker = 'CLOUDCORE_RC_' + Math.random().toString(36).slice(2, 10);
  const cmd = `bash <<'${delim}'\n${code}\n${delim}\necho "${marker}:$?"\n`;
  const startLine = termState.term.buffer.active.length;
  termState.ws.send(JSON.stringify({type: 'input', data: cmd}));
  if (statusEl) statusEl.textContent = 'Running…';
  _captureAndDiagnose(code, marker, startLine, statusEl);
}

// Stage 9 -- watches the real terminal output for the sentinel
// runCommandInTerminal() just appended, to learn a Linux Help
// suggested command's real exit code without guessing, then -- only
// on a real failure -- automatically asks the model to diagnose it
// with the real transcript, never re-running anything on its own
// (that stays a real click, same as every command always has). Polls
// the already-rendered, ANSI-stripped xterm.js buffer rather than the
// raw WS byte stream, which still carries cursor/color/bracketed-paste
// escape codes -- the same buffer-reading approach this session's own
// live CDP verification already relied on for the unresponsive-
// terminal feature. Deliberately only one capture in flight at a time:
// a second "Run in Terminal" click while one is pending still sends
// normally, it just doesn't get its own automatic diagnosis -- a
// documented simplification, not a silent gap, since queuing or
// multiplexing several concurrent polls against the one shared buffer
// isn't worth the complexity for what's realistically one student
// typing at a time.
let _captureInFlight = false;
async function _captureAndDiagnose(command, marker, startLine, statusEl) {
  if (_captureInFlight) return;
  _captureInFlight = true;
  try {
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 500));
      if (!termState) return;  // session ended (Disconnect, closed) mid-wait
      const buf = termState.term.buffer.active;
      for (let i = startLine; i < buf.length; i++) {
        const line = buf.getLine(i);
        if (!line) continue;
        const text = line.translateToString(true);
        // Requires real digits right after the marker's own colon --
        // found live that the shell's own echo of the typed command
        // (`echo "MARKER:$?"`, shown back before it even runs) also
        // contains "MARKER:" as literal text, just followed by the
        // literal characters `$?"` rather than a number. A plain
        // indexOf+slice matched that echoed line first every time,
        // parsed NaN, and NaN !== 0 is always true -- misreporting
        // every single command, success included, as a failure.
        // marker's own value (CLOUDCORE_RC_ + Math.random().toString(36))
        // is always plain alphanumeric/underscore -- never contains a
        // single regex-special character -- so it's embedded directly,
        // no escaping needed (and, found live, easy to get wrong: an
        // earlier version tried to defensively escape it and broke the
        // regex literal entirely).
        const m = text.match(new RegExp(marker + ':(\\d+)'));
        if (!m) continue;
        const rc = parseInt(m[1], 10);
        const transcriptLines = [];
        for (let j = startLine; j < i; j++) {
          const l = buf.getLine(j);
          if (l) transcriptLines.push(l.translateToString(true));
        }
        const transcript = transcriptLines.join('\\n').slice(0, 4000);
        if (statusEl) statusEl.textContent = `Exited ${rc}.`;
        if (rc !== 0) {
          // Plainly labeled, same transparency standard as the coding
          // panel's own "ACTUALLY EXECUTED" blocks -- never let a
          // system-constructed turn be mistaken for something the
          // student typed themselves. Comes back through the exact
          // same renderAssistantContent() path as any other answer, so
          // any command the model suggests here gets its own real
          // "Run in Terminal" button automatically -- no special-
          // casing needed for a second (or third...) round.
          await linuxAsk.askWithText(
            '[Automatic -- result of your last suggested command, sent via Run in Terminal]\\n\\n' +
            'I ran:\\n```bash\\n' + command + '\\n```\\n\\n' +
            'Real terminal transcript:\\n```\\n' + transcript + '\\n```\\n\\n' +
            `It exited with status ${rc} (failure). Explain what went wrong and suggest a corrected command.`
          );
        }
        return;
      }
    }
    // Timed out, not failed -- a genuinely long-running or interactive
    // command (a server, htop, tail -f) never prints the sentinel at
    // all until the student stops it themselves. Silence here would
    // look broken; claiming success or failure would be dishonest --
    // this is the one message that's actually true.
    if (statusEl) statusEl.textContent = 'Sent -- no automatic result available (may be long-running).';
  } finally {
    _captureInFlight = false;
  }
}

// ── Preview panel ────────────────────────────────────────────────────
// A plain <iframe> onto the same per-session proxy the Terminal panel's
// own reminder already points students at (sandbox_terminal.py's
// PREVIEW_PORTS listeners) -- this is a browser-side convenience only,
// not a new capability: everything shown here was already reachable by
// opening the same URL in a new tab. __PREVIEW_PORTS_JSON__ is
// substituted server-side (same mechanism __PREVIEW_PORTS_HINT__ above
// already uses) so this always matches the real deployed port list,
// never a hardcoded guess.
const PREVIEW_PORTS = __PREVIEW_PORTS_JSON__;
let _previewPort = PREVIEW_PORTS.length ? PREVIEW_PORTS[0] : null;

function _previewUrl(port) {
  // location.hostname, not location.host -- the preview ports are
  // separate LB listeners on the same host, never the sandbox page's
  // own port.
  return `${location.protocol}//${location.hostname}:${port}/`;
}

function _renderPreviewPorts() {
  const row = document.getElementById('previewPortRow');
  if (!PREVIEW_PORTS.length) {
    row.innerHTML = '<span class="status">No preview ports configured for this deployment.</span>';
    return;
  }
  row.innerHTML = PREVIEW_PORTS.map(p =>
    `<button class="${p === _previewPort ? 'primary' : ''}" onclick="selectPreviewPort(${p})">${p}</button>`
  ).join('');
}

function selectPreviewPort(port) {
  _previewPort = port;
  _renderPreviewPorts();
  refreshPreview();
}

function refreshPreview() {
  if (_previewPort === null) return;
  const url = _previewUrl(_previewPort);
  // Reassigning .src (even to the same value) forces a real reload --
  // this is also what the Refresh button relies on to pick up a
  // student's own newly (re)started server on the same port.
  document.getElementById('previewFrame').src = url;
  document.getElementById('previewOpenLink').href = url;
}

_renderPreviewPorts();
refreshPreview();

// A plain timer, not real "did something start listening" detection --
// found while building this that the browser genuinely can't tell a
// student's own real app apart from this proxy's own "no session"
// response cross-origin: fetch() needs CORS cooperation from the
// student's own program to read a status code at all (mode:'no-cors'
// makes every response opaque, 200 and 502 indistinguishable), and
// <img>/iframe load events fire the same way for "connected, got some
// response" regardless of whether that response was a real page or
// this proxy's own error text. Reloading on a fixed interval instead
// -- costs one small proxied request every few seconds, but a student
// starting a server sees it appear here without hunting for Refresh.
setInterval(refreshPreview, 5000);

loadState();
</script>
</body></html>
"""

SANDBOX_PAGE_HTML = SANDBOX_PAGE_HTML.replace(
    "__PREVIEW_PORTS_HINT__",
    (" Ports " + ", ".join(PREVIEW_PORTS) + " are also reachable from your browser at this same "
     "host -- run a web server on one of them (e.g. Flask's <code>app.run(host='0.0.0.0', "
     "port=" + PREVIEW_PORTS[0] + ")</code>) and open that port in a new tab to see it.")
    if PREVIEW_PORTS else "")
SANDBOX_PAGE_HTML = SANDBOX_PAGE_HTML.replace(
    "__PREVIEW_PORTS_JSON__", json.dumps([int(p) for p in PREVIEW_PORTS]))


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "verify-proxy/1"

    def log_message(self, fmt, *args):
        pass  # journald already captures stdout/stderr for this unit; avoid double-logging

    # --- dispatch -----------------------------------------------------

    def _clean_path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def do_GET(self):
        path = self._clean_path()
        if path == "/":
            self._serve_sandbox_page()
        elif path == "/examples":
            self._serve_examples_page()
        elif path == "/health":
            self._proxy_passthrough()
        elif path == "/llm-stats":
            self._serve_llm_stats()
        elif path == "/sandbox/ask-status":
            self._handle_ask_status()
        elif path.startswith("/vendor/"):
            self._serve_vendor_file(path[len("/vendor/"):])
        else:
            self._not_found()

    def do_HEAD(self):
        if self._clean_path() == "/health":
            self._proxy_passthrough()
        else:
            self._not_found()

    def do_PUT(self):
        self._not_found()

    def do_DELETE(self):
        self._not_found()

    def do_PATCH(self):
        self._not_found()

    def do_POST(self):
        path = self._clean_path()
        if path == "/sandbox/run":
            self._handle_sandbox_run()
        elif path == "/sandbox/ask":
            self._handle_sandbox_ask()
        elif path == "/sandbox/linux-ask":
            self._handle_linux_ask()
        elif path == "/sandbox/interrupt":
            self._handle_sandbox_interrupt()
        else:
            self._not_found()

    def _not_found(self):
        # Phase 4: the sandbox is the only interface this deployment
        # exposes now -- llama-server's own webui and its raw
        # /v1/chat/completions are deliberately no longer reachable
        # from outside (only /sandbox/ask calls that internally). Same
        # least-exposure discipline api/examples_listener.py's own
        # endpoint allowlist already uses.
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"not found")

    # --- plain reverse proxy (/health only -- see do_GET/do_HEAD) -----

    def _upstream_request(self, body: bytes | None):
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=600)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection")}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn.request(self.command, self.path, body=body, headers=headers)
        return conn, conn.getresponse()

    def _proxy_passthrough(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        try:
            conn, resp = self._upstream_request(body)
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"verify-proxy: upstream unreachable: {e}".encode())
            return
        resp_body = resp.read()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp_body)
        conn.close()

    def _serve_llm_stats(self):
        """CloudCore Dashboard's LLM Performance page polls this --
        see register_llm_deployment() and _llm_stats_snapshot() above.
        Unauthenticated, same as /health -- aggregate counters only, no
        prompt/response content, nothing a public /health-style endpoint
        wouldn't already reveal about this deployment being up."""
        body = json.dumps(_llm_stats_snapshot()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- Phase 4: the sandbox's own two actions -------------------------

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return {}

    def _client_ip(self) -> str:
        # See _client_lock's own module-level comment for why
        # X-Forwarded-For, not self.client_address -- the LB sits in
        # between and that header is where the real browser IP lives.
        # Falls back to the raw TCP peer for direct testing (bypassing
        # the LB entirely, as this session's own verification already
        # does repeatedly).
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def _check_and_record_rate(self, ip: str, bucket: str, max_requests: int, window_s: float) -> bool:
        """Sliding-window per-IP rate check -- returns False (and does
        NOT record this attempt) if `ip` has already made `max_requests`
        requests to `bucket` within the last `window_s` seconds."""
        now = time.monotonic()
        with _client_lock:
            state = _client_state.setdefault(ip, {})
            times = state.setdefault(bucket, [])
            cutoff = now - window_s
            while times and times[0] < cutoff:
                times.pop(0)
            if len(times) >= max_requests:
                return False
            times.append(now)
            return True

    def _send_rate_limited(self, message: str):
        out = message.encode()
        self.send_response(429)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _send_busy(self, message: str):
        # A distinct status (503, not 429's rate-limit/per-IP-already-
        # active meaning) so the browser's own JS can tell "someone ELSE
        # is asking, we're deliberately not queuing you" apart from
        # those other two 429 cases, and knows to start polling
        # /sandbox/ask-status rather than just showing a static message.
        out = message.encode()
        self.send_response(503)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_ask_status(self):
        """GET /sandbox/ask-status -- a trivial, instant read of the
        global busy flag (see _llm_busy's own comment), no LLM call
        involved. Polled by the browser only after it's been told
        "busy" once, to know the moment it's worth telling the student
        to retry rather than leaving them to guess-and-spam Ask."""
        with _llm_busy_lock:
            busy = _llm_busy
        out = json.dumps({"busy": busy}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_sandbox_run(self):
        """Plain Run -- executes the student's own current buffer as-is,
        synchronously, through the exact same run_sandboxed() Phases 1-2
        already proved live. Bounded to a few seconds by
        VERIFY_TIMEOUT_SECONDS, so a plain JSON response is enough; no
        SSE/streaming needed for this action. Not captured -- this is
        the student's own code, not a model claim, so there's nothing
        to ground against."""
        ip = self._client_ip()
        if not self._check_and_record_rate(ip, "run", RATE_LIMIT_RUN_PER_MINUTE, 60):
            self._send_rate_limited(
                f"Too many Run requests -- limit is {RATE_LIMIT_RUN_PER_MINUTE} per minute. "
                f"Wait a moment and try again.")
            return

        req_json = self._read_json_body()
        code = req_json.get("code") or ""
        if not code.strip():
            out = json.dumps({"error": "code is required"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        result = run_sandboxed(code)
        out = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_sandbox_ask(self):
        """Ask the model about the current code -- builds a fresh
        messages list from SANDBOX_SYSTEM_MESSAGE + the browser's own
        held conversation history + this turn's code/question, streams
        the real generation back via the same SSE mechanics the old
        chat endpoint used, then grounds and captures it exactly the
        same way (source="llm-chat-sandbox", distinguishing these rows
        from chat-originated ones in the shared Phase 3 corpus)."""
        self._handle_ask(SANDBOX_SYSTEM_MESSAGE, capture_source="llm-chat-sandbox",
                          verify=True, include_code=True, endpoint_label="/sandbox/ask")

    def _handle_linux_ask(self):
        """Stage 8 -- general Linux Q&A, genuinely separate from the
        coding Ask panel above: its own system prompt
        (LINUX_SYSTEM_MESSAGE), no "current code" concept
        (include_code=False), and no automatic execution/grounding
        (verify=False) -- see LINUX_SYSTEM_MESSAGE's own comment for
        why an auto-run loop doesn't belong here the way it does for
        disposable Python code. Not captured into the Phase 3 corpus
        either (verify=False also skips that, see _relay_and_verify_stream) --
        there is no code/exec grounding data for this panel to offer,
        and that corpus requires it."""
        self._handle_ask(LINUX_SYSTEM_MESSAGE, capture_source="llm-chat-linux",
                          verify=False, include_code=False, endpoint_label="/sandbox/linux-ask")

    def _handle_ask(self, system_message: str, capture_source: str, verify: bool,
                     include_code: bool, endpoint_label: str):
        """Shared by both ask endpoints above -- rate limiting, the
        one-in-flight-per-IP concurrency gate, and POST
        /sandbox/interrupt's own Stop button all stay keyed on IP
        alone, not on which endpoint, since a real student only ever
        has one live question at a time regardless of which panel it
        came from, and both hit the same shared, slow backend.

        Stage 4: rate-limited (RATE_LIMIT_ASK_PER_10MIN) and capped at
        one in-flight ask per IP -- a real student only ever has one
        live question, and this concurrency cap is also what makes
        POST /sandbox/interrupt unambiguous with no extra token needed:
        the IP alone identifies which session to stop.

        On top of that per-IP cap, a GLOBAL one-at-a-time gate (see
        _llm_busy's own comment) rejects a second, DIFFERENT student's
        question outright -- thrown away, never even sent upstream --
        rather than queuing it, per direct request. Checked after the
        per-IP gate so a student re-clicking their OWN in-flight
        question still gets the friendlier "you already have one"
        message instead of being told someone else is busy."""
        ip = self._client_ip()
        if not self._check_and_record_rate(ip, "ask", RATE_LIMIT_ASK_PER_10MIN, 600):
            self._send_rate_limited(
                f"Too many Ask requests -- limit is {RATE_LIMIT_ASK_PER_10MIN} per 10 minutes. "
                f"Wait a moment and try again.")
            return

        with _client_lock:
            state = _client_state.setdefault(ip, {})
            if state.get("ask_active"):
                self._send_rate_limited(
                    "You already have a question in progress -- wait for it to finish, "
                    "or stop it, before asking another.")
                return

        global _llm_busy
        with _llm_busy_lock:
            if _llm_busy:
                self._send_busy(
                    "The assistant is currently answering another student's question -- "
                    "your question was not sent. This page will let you know the moment "
                    "it's free so you can ask again.")
                return
            _llm_busy = True

        with _client_lock:
            state["ask_active"] = True
            interrupt_event = threading.Event()
            state["interrupt"] = interrupt_event

        try:
            self._do_handle_ask(interrupt_event, system_message, capture_source,
                                 verify, include_code, endpoint_label)
        finally:
            with _client_lock:
                state["ask_active"] = False
                state["interrupt"] = None
            with _llm_busy_lock:
                _llm_busy = False

    def _do_handle_ask(self, interrupt_event, system_message: str, capture_source: str,
                        verify: bool, include_code: bool, endpoint_label: str):
        ask_started_at = time.time()
        req_json = self._read_json_body()
        code = (req_json.get("code") or "").strip() if include_code else ""
        question = (req_json.get("question") or "").strip()
        history = req_json.get("history") or []
        max_tokens = req_json.get("max_tokens")

        if not question:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"question is required")
            return

        user_turn = (f"Here is my current code:\n```python\n{code}\n```\n\n{question}"
                     if code else question)
        # Grounding stays strictly additive to the model's own input
        # (question_chars/history below stay based on the clean
        # user_turn, not the grounded version actually sent). The raw
        # question is reformulated into keyword-dense search terms
        # first -- confirmed live that full-text search against a large
        # corpus is too sensitive to natural phrasing otherwise (see
        # _extract_search_terms's own comment for the real evidence).
        # Originally Linux Help only (F-132: Wikipedia/man-page/ArchWiki
        # content was judged unlikely to help code-execution questions);
        # extended to the coding panel too per direct request, on the
        # same defensive shape -- a question this corpus genuinely has
        # nothing for still costs one small extraction round-trip, but
        # never blocks or degrades the answer either way.
        search_terms = _extract_search_terms(question)
        # Check-corpus-first, direct follow-up to F-136: a human-approved
        # past answer (see _local_corpus_search's own comment) is tried
        # before the general-purpose kiwix corpus, not alongside it --
        # kiwix is the fallback for whatever the local corpus doesn't
        # yet cover, not a second opinion on every question.
        grounding, references = _local_corpus_search(search_terms)
        grounding_source = "local_corpus" if references else "none"
        if not references:
            grounding, references = _kiwix_search(search_terms)
            if references:
                grounding_source = "kiwix"
        messages = ([{"role": "system", "content": system_message}]
                    + list(history) + [{"role": "user", "content": grounding + user_turn}])

        conn, resp, last_err = self._open_upstream_completion(messages, max_tokens, endpoint_label)
        if resp is None:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"verify-proxy: upstream unreachable after 3 attempts: {last_err}".encode())
            _log_ask_outcome(endpoint_label, "upstream_unreachable", time.time() - ask_started_at,
                              question_chars=len(user_turn))
            return

        self._relay_and_verify_stream(resp, messages, max_tokens, capture_source=capture_source,
                                       interrupt=interrupt_event, verify=verify, conn=conn,
                                       endpoint_label=endpoint_label, question_chars=len(user_turn),
                                       started_at=ask_started_at, question=question,
                                       search_terms=search_terms, references=references,
                                       grounding_source=grounding_source)
        conn.close()  # redundant once _relay_and_verify_stream closes it early -- harmless, idempotent

    def _open_upstream_completion(self, messages: list, max_tokens=None,
                                   endpoint_label: str = "ask") -> tuple:
        """Open a new streaming POST /v1/chat/completions connection to
        the coordinator's own local llama-server. Shared by the initial
        ask (_do_handle_ask) and every automatic continuation round
        (_relay_and_verify_stream, see MAX_CONTINUATION_ROUNDS) -- same
        brief-retry tolerance the original single-caller version already
        had for a purely local, otherwise-reliable TCP connect (found
        live: a single failed attempt reached the browser as a bare 502
        with nothing useful logged server-side to diagnose afterward).
        Returns (conn, resp, None) on success, or (None, None, last_err)
        after 3 failed attempts -- callers decide how to tell the client
        about that failure in whatever form fits where they are in their
        own response (a fresh 502 before anything's been sent, or an
        honest SSE note appended to a stream already in progress)."""
        payload = {"messages": messages, "stream": True}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload).encode()

        # F-130: this used to be _SOCKET_POLL_TIMEOUT_S (5s), on the
        # assumption that a timed-out resp.read(1) could just be caught
        # and retried indefinitely -- confirmed live, via internal
        # RELAY_DEBUG instrumentation and CPython's own socket.py
        # source, that this was wrong: socket.SocketIO.readinto() sets
        # self._timeout_occurred = True the FIRST time a real
        # socket.timeout fires, and every subsequent read on that same
        # file object raises OSError("cannot read from timed out
        # object") *without ever calling recv() again* -- permanently,
        # for the life of the connection, regardless of how long real
        # content keeps arriving on the (perfectly healthy) underlying
        # socket. That single stray timeout was effectively guaranteed
        # on any real generation, since prefill alone routinely exceeds
        # 5s -- this connection-level timeout is now large enough that
        # a genuine socket.timeout() essentially never fires in
        # practice; _relay_one_stream's own read loop instead uses
        # select() to wait for actual readability, which operates on
        # the raw fd and never touches this internal CPython state at
        # all, so it stays the thing that makes the loop interruptible.
        conn = None
        last_err = None
        for attempt in range(3):
            try:
                conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT,
                                                    timeout=GENERATION_STALL_TIMEOUT_S + 60)
                conn.request("POST", "/v1/chat/completions", body=body,
                              headers={"Content-Type": "application/json",
                                       "Content-Length": str(len(body))})
                return conn, conn.getresponse(), None
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                last_err = e
                print(f"verify-proxy: {endpoint_label} upstream connect attempt "
                      f"{attempt + 1}/3 failed: {e!r}", flush=True)
                if conn is not None:
                    conn.close()
                if attempt < 2:
                    time.sleep(0.5)
        return None, None, last_err

    def _handle_sandbox_interrupt(self):
        """Signals the caller's own in-flight /sandbox/ask (if any) to
        stop at its next real check point (see _call_llama_direct(),
        run_sandboxed_interactive(), and _relay_and_verify_stream()'s
        own interrupt handling). Best-effort, not instant, and not a
        cancellation on llama-server's own side -- it has no such
        endpoint, so the model's own generation keeps computing
        server-side regardless; this only stops US from waiting on or
        relaying it further, same as an ordinary dropped connection
        already does today, and tells the student honestly that's what
        happened rather than pretending it stopped instantly."""
        ip = self._client_ip()
        with _client_lock:
            state = _client_state.get(ip)
            interrupted = bool(state and state.get("interrupt"))
            if interrupted:
                state["interrupt"].set()
        out = json.dumps({"interrupted": interrupted}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _relay_one_stream(self, resp, interrupt=None, sock=None) -> tuple:
        """Read one streamed /v1/chat/completions response chunk by
        chunk, forwarding each raw SSE line to the client as it arrives
        (so the browser sees one continuous typing effect regardless of
        which underlying request -- the original ask, or an automatic
        continuation round -- produced it) while accumulating the full
        text and the final chunk's own finish_reason ("stop" for a
        genuine model-chosen end, "length" for hitting llama-server's
        own context_size ceiling -- see MAX_CONTINUATION_ROUNDS).
        Swallows the upstream's own "data: [DONE]" -- the caller decides
        when the real, final [DONE] goes out, since it may still need to
        relay one or more continuation rounds, or a verification block,
        first.

        `sock` is the raw socket behind `resp` (conn.sock) -- see
        F-130's own root-cause note above _open_upstream_completion's
        connection timeout for why polling happens via select() on this
        raw fd rather than via resp.read()'s own built-in timeout. If
        `sock` is None (should not happen in practice, but defensive),
        falls back to the old read-with-timeout behavior, which is
        still correct for a genuinely fast response -- it just loses
        the ability to survive one real stall-check wakeup.

        Returns (text, finish_reason, stop_reason, meta, timings), where
        stop_reason is None (finished normally), "interrupted" (the
        student's own Stop button), "disconnected" (the student
        navigated away -- a dead pipe, nothing left to write to), or
        "stalled" (no real content for over GENERATION_STALL_TIMEOUT_S
        -- a genuinely slow, not deadlocked, backend -- see F-129)."""
        accumulated = []
        last_chunk_meta: dict = {}
        last_timings: dict = {}
        finish_reason = None
        buf = b""
        last_real_content_time = time.time()
        loop_start = last_real_content_time
        poll_count = 0
        content_chunks = 0
        _relay_debug(f"loop start, resp.status={getattr(resp, 'status', '?')}, sock={'yes' if sock else 'NONE'}")
        while True:
            if interrupt is not None and interrupt.is_set():
                _relay_debug(f"interrupted after {poll_count} polls, {content_chunks} content chunks")
                return "".join(accumulated), finish_reason, "interrupted", last_chunk_meta, last_timings
            if sock is not None:
                # F-130 root cause, confirmed against CPython's own
                # socket.py source: resp.read()'s underlying
                # socket.SocketIO.readinto() sets self._timeout_occurred
                # = True the FIRST time its own settimeout()-governed
                # read genuinely times out, and every subsequent read on
                # that same file object raises OSError("cannot read
                # from timed out object") WITHOUT ever calling recv()
                # again -- permanently, for the rest of the connection's
                # life, regardless of how much real content keeps
                # arriving on the (perfectly healthy) underlying socket.
                # That single stray timeout was effectively guaranteed
                # on any real generation (prefill alone routinely
                # exceeds a few seconds), which is exactly why every
                # slow-but-working response was misdiagnosed as
                # "stalled" with zero content. select() on the raw fd
                # never touches this internal state at all, so it's
                # what actually provides the periodic wakeup now --
                # _open_upstream_completion's own connection timeout is
                # large enough that resp.read() itself essentially never
                # times out in practice.
                ready, _, _ = select.select([sock], [], [], _SOCKET_POLL_TIMEOUT_S)
                if not ready:
                    poll_count += 1
                    elapsed_since_content = time.time() - last_real_content_time
                    _relay_debug(f"poll #{poll_count} not readable, elapsed_since_content={elapsed_since_content:.1f}s, "
                                 f"elapsed_total={time.time() - loop_start:.1f}s")
                    if elapsed_since_content > GENERATION_STALL_TIMEOUT_S:
                        _relay_debug(f"STALL declared after {poll_count} polls, {content_chunks} content chunks")
                        return "".join(accumulated), finish_reason, "stalled", last_chunk_meta, last_timings
                    continue
            try:
                chunk = resp.read(1)
            except (socket.timeout, OSError) as e:
                # Defensive fallback for sock=None only in ordinary
                # operation -- see this function's own docstring. If
                # this fires with sock set, resp.fp's own
                # _timeout_occurred latch has already tripped (e.g.
                # during the initial getresponse() headers read, before
                # this loop ever started) and every future read on this
                # object will raise the same way forever; nothing left
                # to do but surface it as a stall rather than spin.
                poll_count += 1
                elapsed_since_content = time.time() - last_real_content_time
                _relay_debug(f"poll #{poll_count} {e!r}, elapsed_since_content={elapsed_since_content:.1f}s, "
                             f"elapsed_total={time.time() - loop_start:.1f}s")
                if elapsed_since_content > GENERATION_STALL_TIMEOUT_S:
                    _relay_debug(f"STALL declared after {poll_count} polls, {content_chunks} content chunks")
                    return "".join(accumulated), finish_reason, "stalled", last_chunk_meta, last_timings
                time.sleep(0.2)
                continue
            except Exception as e:
                # Never seen in practice -- logged loudly rather than
                # silently caught, since an exception type outside the
                # pair above would otherwise propagate uncaught and kill
                # this handler thread with a bare traceback, same as any
                # other unhandled exception in this file.
                _relay_debug(f"UNEXPECTED exception from resp.read(1): {e!r}")
                raise
            if not chunk:
                _relay_debug(f"EOF (empty read) after {poll_count} polls, {content_chunks} content chunks")
                break
            buf += chunk
            if not buf.endswith(b"\n"):
                continue
            line = buf
            buf = b""

            text = line.decode(errors="replace").strip()
            is_done = text.startswith("data: ") and text[len("data: "):] == "[DONE]"
            if is_done:
                _relay_debug(f"[DONE] after {poll_count} polls, {content_chunks} content chunks")
                break

            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                _relay_debug(f"disconnected (client write failed) after {poll_count} polls, "
                             f"{content_chunks} content chunks")
                return "".join(accumulated), finish_reason, "disconnected", last_chunk_meta, last_timings

            if not text.startswith("data: "):
                continue
            payload = text[len("data: "):]
            try:
                obj = json.loads(payload)
                choice = obj["choices"][0]
                delta = choice.get("delta", {})
                if "content" in delta and delta["content"]:
                    accumulated.append(delta["content"])
                    last_real_content_time = time.time()
                    content_chunks += 1
                    if content_chunks <= 3 or content_chunks % 50 == 0:
                        _relay_debug(f"content chunk #{content_chunks}: {delta['content']!r}")
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                    _relay_debug(f"finish_reason={finish_reason!r} after {content_chunks} content chunks")
                last_chunk_meta = {k: obj.get(k) for k in ("id", "model", "system_fingerprint")}
                # llama-server puts this on the final chunk of each
                # response (timings set) -- see the module-level
                # docstring on _record_llm_stats for where it's read.
                if obj.get("timings"):
                    last_timings = obj["timings"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
                _relay_debug(f"unparseable SSE line, skipped: {e!r} line={line[:200]!r}")
                continue

        return "".join(accumulated), finish_reason, None, last_chunk_meta, last_timings

    def _relay_and_verify_stream(self, resp, request_messages: list, request_max_tokens=None,
                                  capture_source: str = "llm-chat-coordinator", interrupt=None,
                                  verify: bool = True, conn=None, endpoint_label: str = "ask",
                                  question_chars: int = 0, started_at=None, question: str = "",
                                  search_terms: str = "", references: list | None = None,
                                  grounding_source: str = "none"):
        self.send_response(resp.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        text, finish_reason, stop_reason, last_chunk_meta, last_timings = \
            self._relay_one_stream(resp, interrupt, sock=conn.sock if conn else None)
        accumulated_text = [text]

        # Found live, via a direct A/B comparison against the LB path:
        # an upstream connection that gets accepted (a real HTTP
        # response, status included) but whose body then closes with
        # ZERO bytes -- no delta content, no finish_reason -- used to
        # be silently treated as an ordinary, if content-free,
        # "success": stop_reason stays None (the loop's own default)
        # and finish_reason stays None (never "length", so the
        # continuation loop correctly never enters either), so nothing
        # in the rest of this function ever flagged it as wrong. The
        # actual visible result was a bare "data: [DONE]" and nothing
        # else -- exactly the confusing artifact a student reported
        # live. This happens right as the backend is transitioning
        # (e.g. mid-restart-cascade, see F-119/F-120's own Requires=
        # chain) -- HAProxy's own health check correctly refuses to
        # route to a backend in that state at all (a clean 503), but a
        # direct connection can land in the split-second window where
        # the TCP connect succeeds yet the process is already tearing
        # its own response stream down.
        if stop_reason is None and finish_reason is None and not text:
            stop_reason = "empty_response"

        # Found live, chasing a real recurring stall: the caller
        # (_do_handle_ask) used to hold this connection open until this
        # whole function returned, meaning it stayed open through every
        # continuation round below -- each of which opens its OWN
        # separate connection to llama-server, on top of this
        # already-fully-consumed one. Two connections open at once is
        # exactly the kind of concurrent-connection condition F-119's
        # own #28908 starvation bug is triggered by, self-inflicted by
        # this feature rather than just an unlucky collision. This
        # response's own body is fully read at this point (whatever
        # stop_reason/finish_reason came back), so nothing further
        # needs it -- close it now, immediately, same as every
        # continuation round already closes its own conn right after
        # its own _relay_one_stream call returns, not at the very end.
        if conn is not None:
            conn.close()

        # Direct report: real answers were regularly cut off mid-word by
        # llama-server's own context_size ceiling, not a genuine model
        # decision to stop -- the fix is the same "automatically keep
        # going, no student action needed" shape this platform already
        # uses for a failed Run-in-Terminal command, just triggered by
        # finish_reason == "length" instead of a bad exit code. Each
        # round is relayed into the exact same SSE stream, seamlessly,
        # before the real closing [DONE] goes out.
        continuation_rounds = 0
        while (stop_reason is None and finish_reason == "length"
               and continuation_rounds < MAX_CONTINUATION_ROUNDS):
            continuation_rounds += 1
            prior_text = "".join(accumulated_text)
            continue_messages = (list(request_messages)
                                  + [{"role": "assistant", "content": prior_text},
                                     {"role": "user", "content": _continuation_user_turn(prior_text)}])
            conn, cresp, _ = self._open_upstream_completion(continue_messages, request_max_tokens,
                                                              endpoint_label="continuation")
            if cresp is None:
                self._write_sse_delta(
                    "\n\n_[Automatic continuation failed — the model backend became "
                    "unreachable. The answer above may be incomplete.]_\n", last_chunk_meta)
                break
            text, finish_reason, stop_reason, meta, timings = \
                self._relay_one_stream(cresp, interrupt, sock=conn.sock if conn else None)
            conn.close()
            accumulated_text.append(text)
            if meta:
                last_chunk_meta = meta
            if timings:
                last_timings = timings

        if stop_reason is None and finish_reason == "length":
            # Hit MAX_CONTINUATION_ROUNDS without ever seeing a real
            # "stop" -- said honestly rather than silently presenting a
            # reply that may still be missing its ending (a long enough
            # conversation history will eventually refill even a large
            # context_size regardless of how many rounds are allowed).
            self._write_sse_delta(
                "\n\n_[This answer may still be incomplete — it kept hitting the "
                "model's context limit after several automatic continuations. Ask "
                "a follow-up if something's missing.]_\n", last_chunk_meta)

        if last_timings:
            _record_llm_stats(last_chunk_meta.get("model"), last_timings)

        full_text = "".join(accumulated_text)

        if started_at is not None:
            # See _log_ask_outcome's own comment -- covers every real
            # outcome, not just the success case _record_llm_stats above
            # already captures. finish_reason == "length" with
            # stop_reason still None is only reachable here at all
            # because the continuation loop's own condition just exited
            # it -- i.e. MAX_CONTINUATION_ROUNDS got exhausted (or a
            # continuation round itself failed to reach the upstream).
            if stop_reason:
                outcome = stop_reason
            elif finish_reason == "length":
                outcome = "length_exhausted"
            else:
                outcome = "success"
            _log_ask_outcome(endpoint_label, outcome, time.time() - started_at,
                              question_chars=question_chars, answer_chars=len(full_text),
                              continuation_rounds=continuation_rounds,
                              tokens=(last_timings or {}).get("predicted_n"),
                              tps=(last_timings or {}).get("predicted_per_second"))

        capture = None  # only the verify branch below ever sets this -- see _log_grounding's own call
        if stop_reason == "disconnected":
            return  # student navigated away mid-stream -- nothing more to do
        elif stop_reason == "interrupted":
            # Stopped before the model's own response even finished --
            # nothing coherent to ground/verify yet, so skip straight
            # to an honest note instead of running verify_and_maybe_fix()
            # on a deliberately truncated response.
            self._write_sse_delta("\n\n---\n_Stopped at your request._\n", last_chunk_meta)
        elif stop_reason == "stalled":
            # See GENERATION_STALL_TIMEOUT_S's own comment -- a real,
            # live-confirmed llama-server/RPC deadlock, not a slow
            # response. Told honestly rather than left hanging forever;
            # the restart fires in the background so THIS student's own
            # request doesn't also sit through the ~90s model reload --
            # they're told to simply try again shortly instead.
            self._write_sse_delta(
                "\n\n---\n_The model backend appears to have stalled. It's being "
                "automatically restarted -- please try again in about a minute._\n",
                last_chunk_meta)
            threading.Thread(target=_restart_llama_server, daemon=True).start()
        elif stop_reason == "empty_response":
            # See this function's own comment above on the exact
            # accepted-connection-then-zero-bytes race this catches --
            # told honestly rather than silently showing nothing. A
            # plain retry is genuinely the right advice here (unlike
            # "stalled", this isn't a hung backend needing a restart --
            # it's a momentary transition window that's almost
            # certainly already over by the time the student re-asks).
            self._write_sse_delta(
                "\n\n---\n_The model backend closed the connection unexpectedly "
                "before answering (likely a momentary restart in progress) -- "
                "please try again._\n", last_chunk_meta)
        elif verify:
            # Stage 8 -- verify=False (the Linux Q&A panel) skips this
            # whole block: no auto-execution of a suggested shell
            # command (see LINUX_SYSTEM_MESSAGE's own comment for why),
            # and therefore no capture_example() call either -- that
            # corpus hard-requires real code/exec grounding data this
            # panel deliberately never produces.
            code = extract_python_code(full_text) if ENABLE_VERIFICATION else None
            if code:
                extra, capture = verify_and_maybe_fix(request_messages, code, heartbeat=self._sse_heartbeat,
                                                        max_tokens=request_max_tokens, interrupt=interrupt)
                self._write_sse_delta(extra, last_chunk_meta)
                capture_example(request_messages, capture, source=capture_source)

        # stop_reason == "disconnected" already returned above, before
        # this point -- unreachable here, so every remaining path
        # (including interrupted/stalled/empty_response) still gets a
        # real record; a partial full_text there is itself informative.
        # Explicit "passed"/"failed" strings, not a bare bool -- a raw
        # Python True/False stored into grounding_log's own TEXT column
        # would get silently coerced by SQLite's own TEXT affinity into
        # "1"/"0" on the way in, breaking a clean boolean comparison on
        # the way back out; spelling it out avoids that entirely.
        code_verified = None
        if capture is not None:
            code_verified = "passed" if capture.get("passed") else "failed"
        _log_grounding(endpoint_label, question, full_text, search_terms, references or [],
                        code_verified, grounding_source)

        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _write_sse_delta(self, text: str, meta: dict):
        obj = {
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            "object": "chat.completion.chunk",
            **meta,
        }
        try:
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse_heartbeat(self):
        # SSE comment line (leading ":") -- valid per the SSE spec,
        # silently ignored by any real client, exists purely to keep
        # HAProxy's own inactivity timer from firing while a slow
        # Phase 2 fix-round completion is still in flight. Called from
        # _call_llama_direct's own background timer thread, not the
        # main request thread -- raises on a dead connection so that
        # thread's own loop stops cleanly instead of retrying forever.
        self.wfile.write(b": verifying...\n\n")
        self.wfile.flush()

    # --- Phase 4: the sandbox itself ------------------------------------

    def _serve_sandbox_page(self):
        out = SANDBOX_PAGE_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _serve_vendor_file(self, name: str):
        """Stage 2's own CodeMirror assets, embedded into this guest's
        cloud-init at build time -- see VENDOR_DIR/VENDOR_CONTENT_TYPES'
        own comment. Pinned, versioned files, so a long-lived cache is
        fine, same reasoning api/server.py's own GET /vendor/<path>
        already documents for the dashboard's equivalent."""
        content_type = VENDOR_CONTENT_TYPES.get(name)
        if not content_type:
            self._not_found()
            return
        try:
            with open(os.path.join(VENDOR_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            self._not_found()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- Phase 3: student review page ----------------------------------

    def _serve_examples_page(self):
        """Small, self-rendered HTML page (stdlib only, no new frontend
        framework -- matches this whole proxy's own convention) showing
        every published grounded-verification example's full journey:
        prompt -> wrong code -> real failure -> grounded explanation ->
        fix -> real re-verification result. Served on the SAME URL/port
        students already use for chat -- no new credentials, no new
        address to distribute, matches Phase 3's own design intent
        ('for the benefit of all students', not gated per-person)."""
        items = []
        error = None
        if EXAMPLES_API_BASE:
            try:
                req = urllib.request.Request(
                    EXAMPLES_API_BASE + "/v1/llm-chat/examples/published?limit=100")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    items = json.loads(resp.read()).get("items", [])
            except (urllib.error.URLError, OSError, ValueError) as e:
                error = str(e)
        else:
            error = "example capture is not configured for this deployment"

        body = self._render_examples_html(items, error)
        out = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    @staticmethod
    def _render_examples_html(items: list, error: str | None) -> str:
        esc = html.escape

        def _block(label: str, text: str) -> str:
            if not (text or "").strip():
                return ""
            return f"<h4>{esc(label)}</h4><pre>{esc(text)}</pre>"

        cards = []
        for it in items:
            passed_badge = '<span class="pass">PASSED</span>' if it.get("passed") else '<span class="fail">FAILED</span>'
            parts = [
                f'<div class="card">',
                f'<div class="meta">{esc(it.get("model_filename",""))} &middot; {esc(it.get("created_at",""))} &middot; {passed_badge}</div>',
                _block("Prompt", it.get("prompt", "")),
                _block("Generated code", it.get("generated_code", "")),
                _block("Actually executed -- stdout", it.get("exec_stdout", "")),
                _block("Actually executed -- stderr", it.get("exec_stderr", "")),
            ]
            if it.get("fix_explanation"):
                fix_badge = '<span class="pass">FIX PASSED</span>' if it.get("fix_passed") else '<span class="fail">FIX FAILED</span>'
                parts += [
                    f'<div class="meta">{fix_badge}</div>',
                    _block("Grounded explanation + fix", it.get("fix_explanation", "")),
                    _block("Fixed code", it.get("fixed_code", "")),
                    _block("Re-execution -- stdout", it.get("fix_exec_stdout", "")),
                    _block("Re-execution -- stderr", it.get("fix_exec_stderr", "")),
                ]
            parts.append("</div>")
            cards.append("".join(parts))

        body_html = (
            f'<p class="error">Examples aren\'t available right now: {esc(error)}</p>' if error
            else ('<p class="empty">No examples have been published yet.</p>' if not items
                  else "\n".join(cards))
        )

        return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>llm-chat -- Learning Examples</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
h1 {{ font-size: 1.4rem; }}
.card {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; margin: 1.25rem 0; }}
.meta {{ color: #666; font-size: 0.85rem; margin-bottom: 0.5rem; }}
h4 {{ margin: 0.75rem 0 0.25rem; font-size: 0.9rem; }}
pre {{ background: #f6f6f6; border-radius: 4px; padding: 0.6rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }}
.pass {{ color: #0a7a2f; font-weight: 600; }}
.fail {{ color: #b02a2a; font-weight: 600; }}
.error, .empty {{ color: #666; }}
</style></head>
<body>
<h1>Learning Examples</h1>
<p class="meta">Real prompts, real code, real execution results -- curated from this deployment's own chat sessions. Nothing here is summarized or reworded.</p>
{body_html}
</body></html>
"""


def main():
    # Fire-and-forget -- a slow/unreachable CloudCore API must never
    # delay this service actually binding and serving real traffic.
    threading.Thread(target=register_llm_deployment, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
