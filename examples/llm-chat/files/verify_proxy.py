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

import array
import collections
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hmac
import secrets
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
import sys
import tempfile
import termios
import threading
import traceback
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
# llm-chat-kiwix-expansion K3/K4: kiwix-serve runs with --urlRootLocation
# at this prefix, so every link it generates (search hits, article links,
# its own skin/toolbar) already points through verify-proxy's read-only
# /kiwix/ passthrough below -- students can open what the model cited.
KIWIX_URL_ROOT = "/kiwix"
# Book ids (ZIM filename minus ".zim", which is what kiwix-serve's
# books.name filter matches) each panel searches first, then the general
# fallback books (Wikipedia) for any of the 2 slots left empty. Rendered
# from examples/llm-chat/kiwix-zims.json. Empty = search everything, the
# pre-K3 behaviour.
KIWIX_BOOKS = {
    "coding": [b for b in os.environ.get("KIWIX_BOOKS_CODING", "").split(",") if b],
    "linux": [b for b in os.environ.get("KIWIX_BOOKS_LINUX", "").split(",") if b],
}
KIWIX_BOOKS_FALLBACK = [b for b in os.environ.get("KIWIX_BOOKS_FALLBACK", "").split(",") if b]
# Books that get a reserved slot of their own (Stack Overflow): slot 1 stays
# the panel's best curated hit, slot 2 is the reserved book's best hit. As
# a first-tier source Stack Overflow took 25 of 32 coding slots and pushed
# out the textbooks; as a fallback it was never used, because weak curated
# hits filled both slots -- exactly on the error messages students paste,
# where it has the exact thread (K7 benchmark).
KIWIX_BOOKS_RESERVED = [b for b in os.environ.get("KIWIX_BOOKS_RESERVED", "").split(",") if b]
_KIWIX_HITS = 2
# How the two hit slots are filled. K5 benchmark on the full 59-ZIM
# library (26 real queries, every hit judged by hand): fill ~47/52
# relevant, combined ~45, split ~41 -- forcing a Wikipedia hit pulled in
# nonsense like "load average" -> "Genetic load". Latency is not a factor
# (all strategies: max 0.76s against the 3s budget).
#   combined -- one search across every book (pre-K3 behaviour)
#   fill     -- the panel's specialist books first, general books only
#               for slots they leave empty
#   split    -- one specialist hit plus one general hit, each backfilling
#               the other when it has nothing
KIWIX_MERGE = os.environ.get("KIWIX_MERGE", "fill")
# Per kiwix request. Raised from 3s (K7): with the library read over the
# host's read-only NFS export, a never-before-seen query against Stack
# Overflow's index took up to 3.1s cold (1.8s after mount tuning + warm-up),
# and a timeout silently drops the grounding. An ask spends 1-3 minutes
# generating, so a few more seconds here costs nothing by comparison.
_KIWIX_TIMEOUT_S = int(os.environ.get("KIWIX_TIMEOUT_SECONDS", "8"))
# Sentinel lookups (approved-answer corpus, codebase index) keep the
# original short budget: they are local and fast, and previously just
# shared the kiwix constant.
_SENTINEL_LOOKUP_TIMEOUT_S = 3
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
# Direct report: a student pasted a full multi-line pip error (a few
# hundred characters) and got an answer with a real factual mistake in
# it, traced back to this call timing out ("TimeoutError('timed out')")
# at the old 30s -- prefill time scales with input length, and a long
# pasted error/log is exactly the shape that pushes this past a timeout
# tuned for a normal short question. Raised 30 -> 90 for real headroom
# (measured prefill throughput on this hardware has been as low as
# ~4.75 tok/s -- see F-129 -- so even a few hundred extra tokens of
# input meaningfully add to this call's own prefill time), *and*
# _SEARCH_TERMS_MAX_QUESTION_CHARS below bounds the worst case rather
# than just raising the timeout indefinitely to chase an unbounded
# paste.
_SEARCH_TERMS_TIMEOUT_S = 90
# A student pasting a full stack trace or a long terminal error dump,
# not a short question, is exactly the case that (a) risks exhausting
# even the raised timeout above and (b) makes a poor full-text search
# query on its own regardless (the earlier "ring 3" investigation's own
# finding -- excess words dilute ranking -- applies just as much to an
# excess wall of text as to filler words). Only the text handed to
# *this* extraction call is truncated; the model still sees and answers
# the student's full, untruncated question either way -- this only
# ever affects what's used to search for reference material.
_SEARCH_TERMS_MAX_QUESTION_CHARS = 1000
_SEARCH_TERMS_SYSTEM = (
    "Extract 3-6 specific technical search keywords from the user's "
    "question, suitable for a full-text search engine. Respond with "
    "ONLY the keywords, space-separated, no punctuation, no "
    "explanation. Prefer precise technical terms (protocol names, "
    "command names, CPU/kernel terminology) over generic words. "
    "Spell out abbreviations the way documentation words them (MFA -> "
    "two-factor authentication) and name the mechanism involved (PAM, "
    "sshd, systemd) when the question implies one."
)


def _extract_search_terms(question: str) -> str:
    """One small, fast, non-streaming generation asking the model for a
    keyword-dense reformulation of `question`, used as the kiwix search
    pattern instead of the raw question text -- see the comment above
    for why this is necessary, not just an optimization. Adds one
    small round-trip of latency to this student's own request (the
    busy-gate already holds this slot for them; no other student is
    affected). Falls back to the (still-truncated) question on ANY
    failure -- this is a quality improvement, never a blocking
    dependency, matching _kiwix_search's own defensive shape."""
    question = question[:_SEARCH_TERMS_MAX_QUESTION_CHARS]
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
        # Short, hard timeout -- this call
        # is synchronous, on the critical path of every ask (unlike the
        # backgrounded push below), so a slow Sentinel must never
        # meaningfully delay an answer over an optional local-corpus hit.
        with urllib.request.urlopen(url, timeout=_SENTINEL_LOOKUP_TIMEOUT_S) as resp:
            match = json.loads(resp.read())
        if not match:
            return "", []
        snippet = (match.get("answer") or "").strip()
        if not snippet:
            return "", []
        # L7: an answer a lab run verified by running it, not one a person
        # approved, says so -- to the model and in the page's sources.
        label = ("Verified in the lab" if match.get("review_status") == "lab_verified"
                 else "Verified Q&A")
        reference = {"source": label, "title": match.get("question", ""), "snippet": snippet}
        block = ("Reference material (for fact-checking only -- explain in your own "
                 "words, and note plainly if this doesn't fully answer the question):\n"
                 f'[{label}] "{reference["title"]}": {snippet}\n\n')
        return block, [reference]
    except Exception as e:
        print(f"verify-proxy: local corpus lookup failed, trying kiwix instead: {e!r}", flush=True)
        return "", []


def _lab_facts(search_terms: str) -> str:
    """L7: facts earlier lab runs established about similar advice ("X is
    not an installable package in Ubuntu 22.04", a config check that
    failed), from Sentinel's /api/lab-facts, as a prompt block. Sits
    alongside whichever grounding tier answered, not instead of it. Empty
    on no facts or any failure -- never blocks an answer."""
    if not SENTINEL_HOST or not search_terms:
        return ""
    try:
        qs = urllib.parse.urlencode({"q": search_terms})
        url = f"http://{SENTINEL_HOST}:{SENTINEL_PORT}/api/lab-facts?{qs}"
        with urllib.request.urlopen(url, timeout=_SENTINEL_LOOKUP_TIMEOUT_S) as resp:
            facts = json.loads(resp.read()) or []
    except Exception as e:  # noqa: BLE001 -- optional grounding, never fatal
        print(f"verify-proxy: lab facts lookup failed (non-fatal): {e!r}", flush=True)
        return ""
    lines = [f"- {f['fact']}" for f in facts if isinstance(f, dict) and f.get("fact")]
    if not lines:
        return ""
    return ("Lab findings -- established by actually running earlier answers to similar "
            "questions in a fresh Ubuntu 22.04 sandbox. Do not repeat advice these show "
            "does not work:\n" + "\n".join(lines) + "\n\n")


def _push_advice_run_to_sentinel(entry: dict) -> None:
    """L6: best-effort POST of a finished advice run to Sentinel's corpus.
    Called from the advice worker thread, never a request path."""
    try:
        req = urllib.request.Request(
            f"http://{SENTINEL_HOST}:{SENTINEL_PORT}/api/advice-runs",
            data=json.dumps(entry).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:  # noqa: BLE001 -- the corpus is optional; the run itself stands
        print(f"verify-proxy: Sentinel advice-run push failed (non-fatal): {e!r}", flush=True)


# llm-chat-kiwix-expansion (F-167): the codebase tier is only consulted
# for questions about the lab/platform itself. Checked first and
# unconditionally, it answered generic questions from CloudCore's own
# source whenever a keyword overlapped -- found live: "What does the load
# average in uptime actually mean?" was grounded in api/host_stats.py
# (it calls os.getloadavg()), kiwix's Super User / Server Fault answers
# were never consulted, and the model described "the provided code
# snippet" to a student who had provided none. Matched as whole words or
# phrases against the student's own question, case-insensitively.
_PLATFORM_TERMS_RE = re.compile(
    r"\b(cloudcore|llm[- ]chat|sentinel|verify[-_]proxy|coordinator|kiwix|firecracker|"
    r"micro-?vms?|sandbox|this lab|the lab|this platform|the platform|dashboard|"
    r"lab (?:template|environment|instance|build)s?|opentofu template|hafullstack|"
    r"terminal panel|linux help panel|preview ports?)\b", re.I)


def _is_platform_question(question: str) -> bool:
    return bool(_PLATFORM_TERMS_RE.search(question or ""))


def _codebase_search(search_terms: str) -> tuple[str, list[dict]]:
    """A third grounding tier, direct follow-up ("can we include this
    repo's codebase... in the ask the model and linux help"): checked
    after _local_corpus_search() above (a human-approved answer still
    wins) but before _kiwix_search() below -- a real hit here is
    narrower and more specific to how this platform actually works
    than general reference material, so it should win over Wikipedia/
    DevDocs when both might match. Queries Sentinel's own
    /api/codebase-search, an FTS5 index over this project's own source
    (see codebase_index.py on the Sentinel side for what's indexed and
    why it's a full-rebuild-every-time index, kept fresh with one CLI
    command rather than a ZIM-style rebuild+redeploy). Same defensive
    shape as the other two tiers: empty SENTINEL_HOST, no match, or any
    failure all just mean "nothing here, try the next tier" -- never
    blocks, never raises. References are labeled "CloudCore source"
    (the file path as the title) so the model's own prompt, this
    file's own log, and Sentinel's own UI can all tell this apart from
    a Verified Q&A or a kiwix hit."""
    if not SENTINEL_HOST or not search_terms:
        return "", []
    try:
        qs = urllib.parse.urlencode({"q": search_terms})
        url = f"http://{SENTINEL_HOST}:{SENTINEL_PORT}/api/codebase-search?{qs}"
        with urllib.request.urlopen(url, timeout=_SENTINEL_LOOKUP_TIMEOUT_S) as resp:
            hits = json.loads(resp.read())
        lines = []
        references = []
        for hit in (hits or [])[:2]:
            path = (hit.get("path") or "").strip()
            snippet = (hit.get("snippet") or "").strip()
            if not path or not snippet:
                continue
            # F-140 follow-up: the index moved from one row per whole
            # file to one row per chunk -- start_line is real
            # provenance (this chunk, not a claim about the whole
            # file), not just a display nicety.
            start_line = hit.get("start_line")
            title = f"{path}:{start_line}" if start_line else path
            lines.append(f'[CloudCore source] "{title}": {snippet}')
            references.append({"source": "CloudCore source", "title": title, "snippet": snippet})
        if not lines:
            return "", []
        # Said outright, because the model otherwise reads a code snippet in
        # its prompt as code the student wrote (F-167).
        block = ("Source code from the CloudCore platform this lab runs on -- NOT the "
                 "student's own code; use it only to explain how the platform itself "
                 "works, in your own words, and note plainly if it doesn't fully answer "
                 "the question:\n"
                 + "\n".join(lines) + "\n\n")
        return block, references
    except Exception as e:
        print(f"verify-proxy: codebase search failed, trying kiwix instead: {e!r}", flush=True)
        return "", []


# `<p` followed by whitespace or `>` only (F-174): a bare `<p[^>]*>` also
# matched SVG `<path ...>` (Stack Overflow's logo), so the "lead paragraph"
# began at the logo and swept up the site navigation.
_KIWIX_PARA_RE = re.compile(r"<p(?:\s[^>]*)?>(.*?)</p>", re.S | re.I)


def _kiwix_article_lead(link: str, limit: int = 400) -> str:
    """The first substantial paragraph of a kiwix article, as plain text --
    the stand-in snippet for hits kiwix-serve returns without one. Empty on
    any failure; this is best-effort grounding, never a hard dependency."""
    try:
        url = f"http://{KIWIX_HOST}:{KIWIX_PORT}{link}"
        with urllib.request.urlopen(url, timeout=_KIWIX_TIMEOUT_S) as resp:
            page = resp.read(512 * 1024).decode("utf-8", "replace")
        for para in _KIWIX_PARA_RE.findall(page):
            text = " ".join(html.unescape(_KIWIX_TAG_RE.sub("", para)).split())
            if len(text) >= 80:
                return text[:limit] + ("..." if len(text) > limit else "")
    except (OSError, ValueError):
        pass
    return ""


def _kiwix_query(search_pattern: str, books: list[str], want: int) -> list[dict]:
    """One kiwix-serve search, optionally restricted to `books`. Raises on
    failure; the caller decides what that means. kiwix-serve rejects the
    WHOLE request (HTTP 400, "No such book") if any listed book isn't
    loaded, so a single ZIM that failed to register would otherwise
    silently turn off every filtered search -- the caller falls back to an
    unfiltered search on 400."""
    # Generous headroom beyond `want`: tag-listing pages are dropped below,
    # and a big Stack Exchange archive can return nothing BUT those for a
    # popular tag (Stack Overflow: ~100 for "nginx-reverse-proxy").
    params = [("pattern", search_pattern), ("format", "xml"), ("pageLength", str(want + 10))]
    params += [("books.name", b) for b in books]
    url = f"http://{KIWIX_HOST}:{KIWIX_PORT}{KIWIX_URL_ROOT}/search?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=_KIWIX_TIMEOUT_S) as resp:
        body = resp.read()
    hits = []
    for item in ET.fromstring(body).findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title:
            continue
        # Stack Exchange ZIMs index their tag-listing pages too ("Highest
        # Voted 'chmod' Questions", ".../questions/tagged/fdisk_page=20"):
        # lists of question titles that teach nothing, yet they took 22 of
        # the K5 benchmark's result slots.
        if "/questions/tagged/" in link or title.startswith("Highest Voted '"):
            continue
        snippet = _KIWIX_TAG_RE.sub("", item.findtext("description") or "").strip()
        # Near-empty counts as empty (F-174): Stack Exchange hits came back
        # as "...a" and "...", which slipped past a plain emptiness check and
        # reached the model as references with no content at all.
        if sum(c.isalnum() for c in snippet) < 20:
            snippet = ""
        if not snippet and link.startswith(KIWIX_URL_ROOT + "/content/"):
            # kiwix-serve returns some hits with an EMPTY snippet -- found in
            # K5 on Wikipedia's own "Big O notation", the best possible hit.
            # These used to be skipped, silently dropping the most relevant
            # reference; fetch the article's opening text instead.
            snippet = _kiwix_article_lead(link)
        if not snippet:
            continue
        book_title_el = item.find("book/title")
        hits.append({
            "source": (book_title_el.text or "").strip() if book_title_el is not None else "Reference",
            "title": title,
            "snippet": snippet,
            # Only links that stay inside the /kiwix/ passthrough are kept.
            "link": link if link.startswith(KIWIX_URL_ROOT + "/content/") else "",
        })
        if len(hits) >= want:
            break
    return hits


def _kiwix_fallback(search_pattern: str, want: int) -> list[dict]:
    """Fill `want` slots from the general fallback books (Wikipedia, Stack
    Overflow), searched ONE BOOK AT A TIME and taken alternately. Searched
    together, Stack Overflow's tag-listing pages alone filled every result
    for "nginx reverse proxy" and Wikipedia's good hit never surfaced."""
    per_book = []
    for book in KIWIX_BOOKS_FALLBACK:
        try:
            per_book.append(_kiwix_query(search_pattern, [book], want))
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            print(f"verify-proxy: fallback book {book} not loaded; skipping", flush=True)
    picked = []
    for rank in range(want):
        for hits in per_book:
            if rank < len(hits) and len(picked) < want:
                picked.append(hits[rank])
    return picked


_KIWIX_RELAX_TRIES = 4


def _kiwix_references(search_pattern: str, panel: str) -> list[dict]:
    """One merged search for `panel` (the strategy is KIWIX_MERGE). Raises
    on failure; _kiwix_search() owns the never-raise contract."""
    # K3: the panel's specialist books first. In one combined search
    # Wikipedia outranks them almost every time (K5 baseline: 1 of 20
    # coding top-2 slots came from the Python docs/DevDocs), so the
    # general books only fill slots the specialists leave empty.
    specialist = KIWIX_BOOKS.get(panel, [])
    try:
        if KIWIX_MERGE == "combined" or not specialist:
            references = _kiwix_query(search_pattern, [], _KIWIX_HITS)
        elif KIWIX_MERGE == "fill" or not KIWIX_BOOKS_FALLBACK:
            if KIWIX_BOOKS_RESERVED:
                # Both searches at once: the slower of the two, not the sum.
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f_spec = pool.submit(_kiwix_query, search_pattern, specialist, _KIWIX_HITS)
                    f_res = pool.submit(_kiwix_query, search_pattern, KIWIX_BOOKS_RESERVED, 1)
                    spec_hits, res_hits = f_spec.result(), f_res.result()
                references = spec_hits[:1] + res_hits[:1] + spec_hits[1:]
                references = references[:_KIWIX_HITS]
            else:
                references = _kiwix_query(search_pattern, specialist, _KIWIX_HITS)
            if len(references) < _KIWIX_HITS and KIWIX_BOOKS_FALLBACK:
                references += _kiwix_fallback(search_pattern, _KIWIX_HITS - len(references))
        else:  # split
            spec = _kiwix_query(search_pattern, specialist, _KIWIX_HITS)
            gen = _kiwix_query(search_pattern, KIWIX_BOOKS_FALLBACK, _KIWIX_HITS)
            references = spec[:1] + gen[:1]
            for extra in spec[1:] + gen[1:]:
                if len(references) >= _KIWIX_HITS:
                    break
                references.append(extra)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        print(f"verify-proxy: kiwix rejected the book filter ({e.read()[:200]!r}); "
              f"searching all books instead", flush=True)
        references = _kiwix_query(search_pattern, [], _KIWIX_HITS)
    return references


def _kiwix_search(search_pattern: str, panel: str = "") -> tuple[str, list[dict]]:
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
        # kiwix-serve matches ALL terms, so a long keyword list can find
        # nothing (F-174: "MFA PAM two-factor authentication Linux login
        # process" -> 0 hits, the first five words -> 2 good ones). The
        # model lists the most specific terms first, so drop from the end.
        words = search_pattern.split()
        references = _kiwix_references(search_pattern, panel)
        tries = 0
        while not references and len(words) > 2 and tries < _KIWIX_RELAX_TRIES:
            words, tries = words[:-1], tries + 1
            references = _kiwix_references(" ".join(words), panel)
        if tries and references:
            print(f"verify-proxy: kiwix found nothing for {search_pattern!r}; "
                  f"relaxed to {' '.join(words)!r}", flush=True)
        lines = [f'[{r["source"]}] "{r["title"]}": {r["snippet"]}' for r in references]
        if not lines:
            return "", []
        return (("Reference material (for fact-checking only -- explain in your own "
                 "words, and note plainly if this doesn't fully answer the question):\n"
                 + "\n".join(lines) + "\n\n"), references)
    except Exception as e:
        print(f"verify-proxy: kiwix search failed, answering without grounding: {e!r}", flush=True)
        return "", []


# Linux Help answer notices (F-175) -- structured warnings sent after the
# answer as their own SSE event, rendered by the page under the answer and
# never fed back to the model. The model is small and Linux Help answers are
# not executed or checked, so every answer carries a general caution; two
# kinds of answer get a specific one on top.
# llm-chat-lab-sandbox L2: the lab installs from the whole archive, so the
# useful check is whether a suggested package exists in Ubuntu 22.04 at all
# -- a model inventing a package name is one of the failures the lab exists
# to catch. Every component of the release and -updates pockets, ~26MB,
# fetched once per service start.
_UBUNTU_ARCHIVE = os.environ.get("UBUNTU_ARCHIVE_URL", "http://archive.ubuntu.com/ubuntu")
_TERMINAL_PACKAGE_INDEXES = [
    f"{_UBUNTU_ARCHIVE}/dists/{pocket}/{component}/binary-amd64/Packages.xz"
    for pocket in ("jammy", "jammy-updates")
    for component in ("main", "restricted", "universe", "multiverse")]
_terminal_packages: frozenset | None = None


def _load_terminal_packages() -> None:
    """Fetch every Ubuntu 22.04 package name once, in the background. Names
    (and Provides:, so virtual packages like mail-transport-agent count)
    change rarely, so one fetch per service start is enough. On any failure
    the check is skipped -- no notice is better than a wrong one."""
    global _terminal_packages
    import lzma
    names: set[str] = set()
    try:
        for url in _TERMINAL_PACKAGE_INDEXES:
            with urllib.request.urlopen(url, timeout=120) as resp:
                text = lzma.decompress(resp.read()).decode("utf-8", "replace")
            for line in text.splitlines():
                if line.startswith("Package: "):
                    names.add(line[9:].strip())
                elif line.startswith("Provides: "):
                    for prov in line[10:].split(","):
                        names.add(prov.strip().split(" ", 1)[0])
        _terminal_packages = frozenset(names)
        print(f"verify-proxy: loaded {len(_terminal_packages)} Ubuntu 22.04 package names", flush=True)
    except (OSError, lzma.LZMAError, ValueError) as e:
        print(f"verify-proxy: Ubuntu package index unavailable, skipping that notice: {e!r}",
              flush=True)


_APT_INSTALL_RE = re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\s+([^\n;&|`)]*)")
_APT_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
_CODE_SPAN_RE = re.compile(r"```[^\n]*\n(.*?)```|`([^`\n]+)`", re.S)

_NOTICE_GENERAL = (
    "This answer comes from a small local model and has not been run or checked. "
    "Treat it as a starting point: check it against the sources below and the man "
    "pages before using it on a real system.")
_NOTICE_AREAS = [
    (re.compile(r"/etc/pam\.d|\bpam_\w+\.so\b|sshd_config|/etc/sudoers|\bvisudo\b|/etc/shadow"
                r"|\bAuthenticationMethods\b|\bPermitRootLogin\b"),
     "login and authentication"),
    # Changing forms only: listing rules (iptables -L, ufw status) is harmless.
    (re.compile(r"\bip6?tables\s+(?:-t\s+\w+\s+)?-[AIDFPXNR]\b"
                r"|\bufw\s+(?:enable|disable|allow|deny|reject|limit|delete|default|reset)\b"
                r"|\bnft\s+(?:add|delete|flush|insert|replace|create)\b"
                r"|\bfirewall-cmd\s+.*--(?:add|remove|set|permanent)"),
     "the firewall"),
    # Likewise fdisk -l / parted print only read the partition table.
    (re.compile(r"/etc/fstab|update-grub|grub-install|/etc/default/grub|\bmkfs(?:\.\w+)?\b"
                r"|\bfdisk\s+/dev|\bparted\b.*\b(?:mklabel|mkpart|rm|resizepart)\b|\bdd\s+if="),
     "disks or boot"),
]


_NOTICE_GENERAL_LAB = (
    "This answer comes from a small local model. It is tried automatically in a fresh "
    "sandbox below, but a step that runs is not proof the advice is good: check it "
    "against the sources and the man pages before using it on a real system.")


def _answer_notices(answer: str) -> list[dict]:
    notices = [{"level": "info",
                "text": _NOTICE_GENERAL_LAB if ADVICE_RUN_ENABLED else _NOTICE_GENERAL}]
    pkgs = _terminal_packages
    if pkgs is not None:
        missing = []
        # Code only: prose ("install it with apt install and then ...") is not a command.
        code = "\n".join(c for pair in _CODE_SPAN_RE.findall(answer) for c in pair if c)
        for m in _APT_INSTALL_RE.finditer(code):
            for tok in m.group(1).split():
                if tok.startswith("-"):
                    continue
                name = tok.split("=", 1)[0].split(":", 1)[0]
                if _APT_PKG_RE.match(name) and name not in pkgs and name not in missing:
                    missing.append(name)
        if missing:
            notices.append({"level": "warn", "text": (
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not "
                f"{'a package' if len(missing) == 1 else 'packages'} in Ubuntu 22.04, in any "
                "component, so that install step will fail. The name may be wrong, or it "
                "may need a third-party repository the answer doesn't mention.")})
    areas = [label for rx, label in _NOTICE_AREAS if rx.search(answer)]
    if areas:
        notices.append({"level": "warn", "text": (
            f"This answer changes {' and '.join(areas)}. A mistake here can lock you out "
            "or lose data: keep a root session open and test from a second one, have "
            "console access or a backup first, and read each change before applying it.")})
    return notices


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
                    references: list, code_verified, grounding_source: str = "none",
                    outcome=None, duration_s=None, tokens=None, tokens_per_second=None,
                    continuation_rounds=None) -> None:
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

    outcome/duration_s/tokens/tokens_per_second/continuation_rounds:
    direct follow-up, "we need an actual performance dashboard...
    quality of questions, failures to answer" -- the exact same facts
    _log_ask_outcome() (below) already computes at this same call site
    moments earlier, for ASK_OUTCOME's own stdout-only/Loki-only line.
    Rather than a second push mechanism, this one call now also carries
    them into grounding_log's own row -- one logical event (this
    completed ask) gaining more recorded facts, not a different
    lifecycle needing its own table the way ask_queue_events' own
    before-the-ask transitions genuinely do.

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
        "outcome": outcome, "duration_s": duration_s, "tokens": tokens,
        "tokens_per_second": tokens_per_second, "continuation_rounds": continuation_rounds,
    }
    print(f"GROUNDING_LOG {json.dumps(entry)}", flush=True)
    if SENTINEL_HOST:
        threading.Thread(target=_push_grounding_to_sentinel, args=(entry,), daemon=True).start()


def _push_llm_stats_to_sentinel(entry: dict) -> None:
    """Best-effort POST of this deployment's own current _llm_stats
    snapshot to Sentinel's own /api/llm-stats -- same shape as
    _push_grounding_to_sentinel above. Sentinel upserts by
    deployment_name (see its own llm_stats.py), so this is always
    "latest known state", not a history -- matches _llm_stats itself
    being a live snapshot, not a log."""
    try:
        body = json.dumps(entry).encode()
        req = urllib.request.Request(
            f"http://{SENTINEL_HOST}:{SENTINEL_PORT}/api/llm-stats",
            data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=_SENTINEL_PUSH_TIMEOUT_S).close()
    except Exception as e:
        print(f"verify-proxy: Sentinel llm-stats push failed (non-fatal): {e!r}", flush=True)


def _push_ask_queue_event(entry: dict) -> None:
    """Best-effort POST of one ask-queue event to Sentinel's own
    /api/ask-queue -- same shape as _push_grounding_to_sentinel above
    (plain urllib, no auth, always backgrounded, a slow/unreachable
    Sentinel must never add latency to a student's own request)."""
    try:
        body = json.dumps(entry).encode()
        req = urllib.request.Request(
            f"http://{SENTINEL_HOST}:{SENTINEL_PORT}/api/ask-queue",
            data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=_SENTINEL_PUSH_TIMEOUT_S).close()
    except Exception as e:
        print(f"verify-proxy: Sentinel ask-queue push failed (non-fatal): {e!r}", flush=True)


def _log_ask_queue_event(request_id: str, endpoint_label: str, event: str,
                          queue_position: int | None = None, queue_depth: int | None = None,
                          wait_seconds: float | None = None) -> None:
    """Records one queued/started/finished transition for one ask,
    correlated by request_id -- direct follow-up to "some sort of
    monitor running from sentinel to oversee the llm, the questions
    posted... the order". Only called when ASK_QUEUE_ENABLED (see
    _ask_queue's own comment) -- with the feature off there is no
    queue to report order/wait-time for. Same two-channel, best-effort
    shape as _log_grounding above: a stdout line (Loki, free) plus a
    backgrounded push to Sentinel's own DB for a queryable record."""
    entry = {
        "request_id": request_id, "endpoint": endpoint_label, "event": event,
        "queue_position": queue_position, "queue_depth": queue_depth,
        "wait_seconds": wait_seconds,
    }
    print(f"ASK_QUEUE_EVENT {json.dumps(entry)}", flush=True)
    if SENTINEL_HOST:
        threading.Thread(target=_push_ask_queue_event, args=(entry,), daemon=True).start()

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

    verify-proxy.service's own unit used to be
    `Requires=llama-server.service`, which restarted verify-proxy.service
    itself in lockstep with this restart (systemd's own dependency
    propagation) -- accepted at the time as a brief, low-cost side
    effect. Changed to `Wants=` after wait-for-rpc-workers.sh's own
    fast-fail (a direct follow-up, "we do get a lot of rpc or crash
    looping with this") exposed a real problem with that: once
    llama-server.service's own START JOB can genuinely fail (not just
    crash after a successful start), a `Requires=` dependent's own start
    fails right along with it ("Dependency failed") -- a failure
    Restart=on-failure does NOT retry, confirmed live to leave
    verify-proxy.service permanently inactive (hard connection-refused
    for every student) rather than the brief bounce this comment used to
    describe. `Wants=` keeps the same startup ordering and still starts
    llama-server alongside verify-proxy, but no longer hard-fails this
    process over that one's own job outcome -- a request arriving while
    llama-server is down now gets the real, already-existing graceful
    502 "upstream unreachable" from _open_upstream_completion's own
    retry logic instead. Nothing here waits for or depends on
    llama-server surviving past this point either way."""
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
    "You are a lab coding assistant. Only discuss the code the "
    "student has provided in this conversation. If asked something "
    "unrelated to that code or to this lab exercise, politely decline "
    "and redirect the student back to their code. Only describe what "
    "code actually does -- never claim a function, sort, or check "
    "exists unless it is genuinely present in the code you just wrote "
    "or were shown; if you are not certain something is correct, say "
    "so explicitly rather than stating it as fact. When suggesting a "
    "fix, provide the complete corrected program in a single fenced "
    "code block tagged with its language, and keep your own "
    "explanation concise -- this hardware generates slowly, so prefer "
    "a short, precise answer over a long one where both would be "
    "equally correct. Code you write runs in a real sandbox that "
    "supports interactive input (Python's input(), C's scanf, Bash's "
    "read and the like) -- if a script you wrote is waiting for "
    "input, you will be shown exactly what it has printed so far and "
    "asked what to provide; reply with ONLY a fenced ```stdin block "
    "containing exactly the one line to send. Never reply with a "
    "```stdin block on its own unless you have just been told a "
    "running script is waiting for input; if asked to run code with "
    "particular input, give the complete program in a fenced code "
    "block and say which input to use. This can happen a few times "
    "per script, not unlimited, so keep prompts short and avoid "
    "scripts that would need a long back-and-forth. This code sandbox "
    "runs Python, Bash, JavaScript (Node), C, C++ and Go, one-shot, "
    "with no network access and only each language's standard "
    "library. Separately, the page's own Terminal panel gives a real "
    "persistent Linux shell with genuine internet access (pip "
    "install, curl, cloning a repo) that is otherwise fully isolated, "
    "plus ports __PREVIEW_PORTS_LIST__ reachable from the browser for "
    "previewing a web app run there -- if asked about installing "
    "packages, running something long-lived, or viewing a web app's "
    "own output, say to use the Terminal (whose own panel lists the "
    "exact ports), not this code sandbox."
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
    "filesystem, cron, log inspection) -- the student may be a complete"
    " beginner or already comfortable at the command line, so don't "
    "assume either. Only describe what a command actually does -- never"
    " claim a flag or behavior exists unless you are genuinely sure of "
    "it; if you are not certain something is correct, say so explicitly"
    " rather than stating it as fact. When you suggest a command, put "
    "it in its own fenced ```bash code block so the student can run it "
    "with one click -- a command you suggest is NEVER run without the "
    "student clicking 'Run in Terminal' themselves, but once they do, "
    "the real result IS automatically checked: if it fails, you will be"
    " shown the real terminal transcript and exit code and asked to "
    "diagnose it and suggest a fix, same as this turn. This hardware "
    "generates slowly, so keep answers short and precise rather than "
    "long where both would be equally correct. The student's own "
    "Terminal panel is a real, minimal Ubuntu 22.04 shell with genuine "
    "internet access, but: apt can install anything from the whole "
    "Ubuntu 22.04 archive (main, restricted, universe and multiverse), "
    "it has no persistent storage across sessions, and it cannot reach "
    "anything on the local network except the real internet. Ports "
    "__PREVIEW_PORTS_LIST__ are reachable from the student's browser "
    "for previewing anything they serve there. This is a genuinely "
    "minimal image -- ordinary tools you might expect (e.g. fdisk) are "
    "often not preinstalled. If a command you suggest might need one, "
    "give ONLY ONE fenced ```bash block for it, combining the install "
    "check and the real command with `||` in that single block -- for "
    "example exactly `command -v fdisk >/dev/null || sudo apt-get "
    "install -y fdisk; fdisk -l /dev/vda` (adjust the tool/package name"
    " and real command). Chain steps that must all happen with && (for "
    "example `sudo apt-get update && sudo apt-get install -y PACKAGE`);"
    " || means 'only if the first part failed', so use it only for that"
    " install-if-missing check. Do NOT also show a plain, naive version"
    " of the command on its own first -- that copy would just fail with"
    " 'command not found' if the tool is missing, defeating the whole "
    "point. The student should only ever need to click 'Run in "
    "Terminal' once, on the one block you give them. Only runnable "
    "shell commands go in ```bash blocks: file contents, config lines "
    "(sshd_config, PAM, fstab, systemd units) and example output go in "
    "```text blocks, because every ```bash block gets a 'Run in "
    "Terminal' button. Only suggest packages that really exist in "
    "Ubuntu 22.04 under that exact name (a command name is not always "
    "its package name). For any change to authentication or remote "
    "access (PAM, SSH, sudo, firewall), warn about lockout: keep an "
    "existing session open while testing from a second one, and avoid "
    "settings that lock out users who have not been set up yet. "
    "Politely decline anything clearly unrelated to Linux or this lab "
    "and redirect back to that."
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
    # Stage 12 -- editor modes for the other Run/Ask languages.
    "codemirror-mode-javascript.min.js": "text/javascript",
    "codemirror-mode-clike.min.js": "text/javascript",
    "codemirror-mode-go.min.js": "text/javascript",
    "codemirror-mode-shell.min.js": "text/javascript",
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

# Any fence tag, not just python/py (Stage 12) -- extract_code() decides
# which tags name a runnable language; anything else (```stdin, ```text,
# ```output) is skipped exactly as a non-python tag always was.
CODE_BLOCK_RE = re.compile(r"```([A-Za-z0-9_+#.-]*)[ \t]*\n(.*?)```", re.DOTALL)
_PY_HINTS = ("def ", "import ", "print(", "class ", "for ", "if __name__")

# Stage 12 -- every language Run/Ask can execute. Python keeps its own
# coordinator-side unshare runner (run_sandboxed*); every other language
# runs in a fresh, network-less Firecracker microVM per Run
# (run_in_microvm). `compile` runs first, with its own timeout, and a
# non-zero exit there is reported as a compile failure, not a run.
LANGUAGES = {
    "python": {"label": "Python", "fence": "python", "aliases": ("python", "py", "python3")},
    "bash": {"label": "Bash", "fence": "bash", "aliases": ("bash", "sh", "shell"),
             "file": "main.sh", "compile": None, "run": "bash main.sh"},
    "javascript": {"label": "JavaScript (Node)", "fence": "javascript",
                   "aliases": ("javascript", "js", "node", "nodejs"),
                   "file": "main.js", "compile": None, "run": "node main.js"},
    "c": {"label": "C", "fence": "c", "aliases": ("c",),
          "file": "main.c", "compile": "gcc -O0 -Wall -o main main.c -lm", "run": "./main"},
    "cpp": {"label": "C++", "fence": "cpp", "aliases": ("cpp", "c++", "cxx", "cc"),
            "file": "main.cpp", "compile": "g++ -O0 -Wall -std=c++17 -o main main.cpp", "run": "./main"},
    "go": {"label": "Go", "fence": "go", "aliases": ("go", "golang"),
           "file": "main.go", "compile": "go build -o main main.go", "run": "./main"},
}
_LANG_BY_ALIAS = {alias: key for key, spec in LANGUAGES.items() for alias in spec["aliases"]}

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

# Stage 11 -- exact detection, tried before the quiet-period heuristic
# above. verify-proxy runs as root, so it can read /proc/<pid>/syscall
# for every task in the sandboxed process tree: a task blocked in
# read(2) on fd 0, where fd 0 is the stdin pipe we hold, is waiting for
# input with certainty -- no need to wait out INTERACTIVE_QUIET_S. A
# task that is running, or sleeping in nanosleep, is definitely NOT
# waiting, however long it stays silent. Anything else (epoll/futex
# waits, e.g. an event-loop runtime reading stdin) is genuinely
# ambiguous from the outside and falls back to the heuristic. x86_64
# syscall numbers; the coordinator is always x86_64 (llama.cpp and
# Firecracker artifacts are both pinned to it).
_SYS_READ = 0
_SYS_POLL = 7
_SYS_SELECT = 23
_SYS_NANOSLEEP = 35
_SYS_WAIT4 = 61
_SYS_CLOCK_NANOSLEEP = 230
_SYS_WAITID = 247
_SYS_PSELECT6 = 270
_SYS_PPOLL = 271

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

# Configurable module, direct follow-up to the above: "we might even
# need to send the messages to something like rabbitmq... so that we
# don't try to overload the llm" -- discussed and deliberately NOT
# RabbitMQ (this coordinator has exactly one consumer, one model, so a
# broker's own durability/multi-consumer value doesn't apply here; the
# real ask is a visible position instead of an outright reject).
# Default OFF: with this False, every code path below that checks it
# is skipped and _handle_ask behaves exactly as it did before this
# existed. `_ask_queue` is an IP-keyed FIFO (an OrderedDict, not
# queue.Queue -- there's no producer/consumer thread split here, just
# an ordered membership test under a lock already held for other
# reasons) guarded ENTIRELY by _llm_busy_lock above, not a new lock:
# queue order and _llm_busy are one invariant, and two locks here
# would only add a real ordering-bug risk for no benefit.
ASK_QUEUE_ENABLED = os.environ.get("ASK_QUEUE_ENABLED", "false").lower() == "true"
_ASK_QUEUE_TTL_S = 60  # a few multiples of the frontend's own 4s ask-status poll
_ask_queue: "collections.OrderedDict[str, dict]" = collections.OrderedDict()


def _prune_stale_queue_entries_locked() -> None:
    """Caller must already hold _llm_busy_lock. No background sweep
    thread -- an abandoned ticket (closed tab, or rate-limited on
    retry before ever reaching the busy-gate below) simply ages out
    the next time ANYONE touches the queue (their own ask-status poll,
    every 4s, or a fresh ask attempt), since a live student's own
    ticket is refreshed by that exact same traffic. Self-healing, no
    new thread or scheduler needed."""
    now = time.time()
    for stale_ip in [ip for ip, t in _ask_queue.items() if now - t["last_seen"] > _ASK_QUEUE_TTL_S]:
        del _ask_queue[stale_ip]


def _queue_position_locked(ip: str) -> int | None:
    """Caller must already hold _llm_busy_lock. 1-based position, or
    None if `ip` isn't currently queued. list(dict.keys()).index() is
    fine at these sizes (a handful of students at once, never a real
    hot loop) -- no secondary index worth the complexity."""
    ips = list(_ask_queue.keys())
    return ips.index(ip) + 1 if ip in ips else None


def extract_code(text: str, default_language: str = "python") -> tuple[str, str] | None:
    """(language, code) for the first fenced block tagged with a
    supported language. An untagged block counts as `default_language`
    -- the student's selected language -- except that for Python it
    still has to look like Python (the original cheap keyword check),
    so a stray untagged snippet of prose isn't executed. Returns None if
    nothing worth running was found."""
    for tag, code in CODE_BLOCK_RE.findall(text):
        lang = _LANG_BY_ALIAS.get(tag.lower())
        if lang:
            return lang, code
        if not tag:
            if default_language != "python":
                return default_language, code
            if any(h in code for h in _PY_HINTS):
                return "python", code
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


def _descendant_pids(root_pid: int) -> list[int]:
    """root_pid plus every process below it, from one scan of /proc.
    The sandboxed interpreter sits one or two levels below the Popen
    PID (unshare -> its forked child in the new PID namespace)."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        # comm (field 2) may contain spaces or parens; everything after
        # the LAST ')' is fixed-format: state, ppid, ...
        fields = stat[stat.rfind(")") + 2:].split()
        children.setdefault(int(fields[1]), []).append(int(entry))
    found, stack = [], [root_pid]
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, []))
    return found


def _pipe_unread(fd: int) -> int:
    """Bytes written to a pipe and not yet read by the other end.
    FIONREAD works on either end of a Linux pipe."""
    buf = array.array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, buf, True)
    return buf[0]


def _stdin_wait_state(root_pid: int, stdin_pipe_ino: int) -> str:
    """"waiting" (a task is blocked reading our stdin pipe), "busy" (a
    task is running or sleeping on a timer -- never hand it input), or
    "unknown" (only ambiguous waits seen; use the heuristic). Processes
    that vanish mid-scan are skipped, not treated as errors."""
    pipe_target = f"pipe:[{stdin_pipe_ino}]"
    busy = False
    for pid in _descendant_pids(root_pid):
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            base = f"/proc/{pid}/task/{tid}"
            try:
                with open(f"{base}/stat") as f:
                    stat = f.read()
                with open(f"{base}/syscall") as f:
                    syscall = f.read().split()
            except OSError:
                continue
            state = stat[stat.rfind(")") + 2:].split()[0]
            if state == "R" or not syscall or syscall[0] == "running":
                busy = True
                continue
            try:
                nr = int(syscall[0])
            except ValueError:
                continue
            if nr == _SYS_READ and len(syscall) > 1 and int(syscall[1], 16) == 0:
                try:
                    if os.readlink(f"/proc/{pid}/fd/0") == pipe_target:
                        return "waiting"
                except OSError:
                    pass
            elif nr in (_SYS_NANOSLEEP, _SYS_CLOCK_NANOSLEEP):
                busy = True
            # select/poll with no fds at all is a pure timer sleep --
            # Python 3.10's time.sleep() is exactly pselect6(0, ...),
            # found live on the coordinator (jammy's python3).
            elif nr in (_SYS_SELECT, _SYS_PSELECT6) and len(syscall) > 1 and int(syscall[1], 16) == 0:
                busy = True
            elif nr in (_SYS_POLL, _SYS_PPOLL) and len(syscall) > 2 and int(syscall[2], 16) == 0:
                busy = True
            # wait4/waitid: the unshare wrapper waiting on its child --
            # says nothing about the program itself, so it's ignored.
    return "busy" if busy else "unknown"


def run_sandboxed_interactive(code: str, provide_input, interrupt=None) -> dict:
    """Like run_sandboxed(), same isolation exactly (same unshare +
    unprivileged user + resource.setrlimit CPU/memory/proc/fsize
    limits -- CPU-time based, not wall-clock, so still correctly
    bounds a longer-lived session), but the process's stdin stays open
    and live instead of DEVNULL.

    Whenever the process is waiting for input -- detected exactly via
    _stdin_wait_state() where possible, otherwise by going quiet (no new
    stdout/stderr for INTERACTIVE_QUIET_S seconds) while neither
    running nor sleeping on a timer -- `provide_input(transcript_so_far)`
    is called and expected to
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
    "timed_out", "interrupted", "exchanges", "detections"}.
    `detections` lists, per exchange, whether it was triggered "exact"
    or "heuristic". `transcript` interleaves
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
    detections: list[str] = []
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
        stdin_pipe_ino = os.fstat(proc.stdin.fileno()).st_ino
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

            wait_state = _stdin_wait_state(proc.pid, stdin_pipe_ino)
            # A task still shows as blocked in read(0) for a moment after
            # we write, until the scheduler runs it -- on a contended
            # coordinator that window can span a whole poll. Unread bytes
            # in the pipe mean our last input hasn't been consumed yet,
            # so it can't be waiting for the next one.
            if wait_state == "waiting" and _pipe_unread(proc.stdin.fileno()) > 0:
                wait_state = "busy"
            if wait_state == "waiting":
                detection = "exact"
            elif (wait_state == "unknown"
                  and time.monotonic() - last_output_at >= INTERACTIVE_QUIET_S):
                detection = "heuristic"
            else:
                detection = None
            if detection is not None:
                if exchanges >= INTERACTIVE_MAX_EXCHANGES:
                    session_timed_out = True
                    break
                value = provide_input("".join(transcript))
                if value is None:
                    break
                exchanges += 1
                detections.append(detection)
                transcript.append(f"\n>>> INPUT PROVIDED ({detection}): {value!r}\n")
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
            "detections": detections,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- Stage 12: non-Python execution in a per-run Firecracker microVM ------
#
# Each Run/Ask verification in Bash, Node, C, C++ or Go boots its own
# jailed microVM (microvm.py -- same launcher as the Terminal), on a
# separate bridge with NO route anywhere: fcrun0 has no NAT and its
# FORWARD/INPUT rules drop everything except return traffic for the
# coordinator's own SSH into the guest (see coordinator cloud-init). So
# model-generated code keeps the same network-less guarantee the Python
# runner's unshare --net gives it.
#
# The coordinator is small and llama-server already holds most of its
# RAM, so booting is gated twice: a FIFO queue capped at
# RUN_VM_MAX_CONCURRENT (default 1), and a MemAvailable floor checked just
# before boot -- an honest "under memory pressure" beats an OOM kill of
# llama-server.
RUN_BRIDGE = "fcrun0"
RUN_SUBNET_CIDR = os.environ.get("RUN_SUBNET_CIDR", "10.201.0.0/24")
RUN_VM_MAX_CONCURRENT = int(os.environ.get("RUN_VM_MAX_CONCURRENT", "1"))
RUN_VM_MEM_MIB = int(os.environ.get("RUN_VM_MEM_MIB", "256"))
# Go's compiler is the heaviest thing any of these languages does.
RUN_VM_MEM_MIB_GO = int(os.environ.get("RUN_VM_MEM_MIB_GO", "512"))
RUN_VM_MIN_HOST_MEM_MB = int(os.environ.get("RUN_VM_MIN_HOST_MEM_MB", "400"))
RUN_COMPILE_TIMEOUT_S = int(os.environ.get("RUN_COMPILE_TIMEOUT_SECONDS", "60"))
RUN_QUEUE_WAIT_S = int(os.environ.get("RUN_QUEUE_WAIT_SECONDS", "120"))
RUN_BOOT_TIMEOUT_S = 20
# Per-run VMs have no network at all, so the Terminal's boot-time apt
# index refresh would only burn the single vCPU retrying for nothing.
_RUN_VM_BOOT_ARGS = "systemd.mask=refresh-apt-index.service"
_RUN_DIR = "/home/student/run"
_RUN_PIDFILE = f"{_RUN_DIR}/.pid"
_RUN_OUTPUT_HARD_CAP = MAX_OUTPUT_CHARS * 4


class _RunQueue:
    """FIFO admission for per-run microVMs. `position()` lets the browser
    show "waiting for a free sandbox (position N)" while its own Run is
    queued, keyed by a ticket the browser generated itself."""

    def __init__(self, capacity: int):
        self.capacity = max(1, capacity)
        self.cond = threading.Condition()
        self.waiting: list[str] = []
        self.running: set[str] = set()

    def acquire(self, ticket: str, timeout: float, interrupt=None) -> bool:
        deadline = time.monotonic() + timeout
        with self.cond:
            self.waiting.append(ticket)
            try:
                while not (self.waiting[0] == ticket and len(self.running) < self.capacity):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or (interrupt is not None and interrupt.is_set()):
                        return False
                    self.cond.wait(min(remaining, 0.5))
                self.running.add(ticket)
                return True
            finally:
                self.waiting.remove(ticket)
                self.cond.notify_all()

    def release(self, ticket: str) -> None:
        with self.cond:
            self.running.discard(ticket)
            self.cond.notify_all()

    def position(self, ticket: str) -> dict:
        with self.cond:
            if ticket in self.running:
                return {"state": "running"}
            if ticket in self.waiting:
                return {"state": "queued", "position": self.waiting.index(ticket) + 1}
            return {"state": "unknown"}


_run_queue = _RunQueue(RUN_VM_MAX_CONCURRENT)
_run_pool = None
_run_pool_lock = threading.Lock()


def _get_run_pool():
    """microvm.py (and paramiko under it) is imported lazily, on the first
    non-Python run, so the Python-only path keeps working exactly as
    before even where those aren't installed."""
    global _run_pool
    from microvm import IpPool
    with _run_pool_lock:
        if _run_pool is None:
            _run_pool = IpPool(RUN_SUBNET_CIDR)
        return _run_pool


# llm-chat-lab-sandbox L4: every Linux Help answer is tried for real, step
# by step, in a disposable microVM on the sandbox bridge (internet-only
# egress, like the Terminal; never the student's own session). One run at a
# time, in answer order; the page polls /sandbox/lab-run for progress.
ADVICE_RUN_ENABLED = os.environ.get("ADVICE_RUN_ENABLED", "true").lower() == "true"
SANDBOX_SUBNET_CIDR = os.environ.get("SANDBOX_SUBNET_CIDR", "10.200.0.0/24")
SANDBOX_BRIDGE = os.environ.get("SANDBOX_BRIDGE", "fcbr0")
_ADVICE_KEEP = 50
_advice_runs: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_advice_waiting: list[str] = []
_advice_cv = threading.Condition()
_advice_pool = None
_advice_worker_started = False

# Kept lab machines: after a run the target VM stays up so the person who
# asked can open it in the Terminal panel and look around (direct request:
# "provide the instructions on how to login in to the lab machine ... and
# ... offer a destroy the answers lab button to save resources"). Each has a
# deadline; at most ADVICE_KEEP_MAX exist at once, and a new run evicts the
# oldest first, so kept machines never starve new runs of memory.
ADVICE_KEEP_MINUTES = int(os.environ.get("ADVICE_KEEP_MINUTES", "20"))
ADVICE_KEEP_MAX = int(os.environ.get("ADVICE_KEEP_MAX", "2"))
_kept_labs: "collections.OrderedDict[str, dict]" = collections.OrderedDict()  # run id -> {vm, expires, token}
_kept_lock = threading.Lock()


def _release_kept(run_id: str, reason: str) -> bool:
    with _kept_lock:
        entry = _kept_labs.pop(run_id, None)
    if entry is None:
        return False
    try:
        entry["vm"].teardown()
    except Exception as e:  # noqa: BLE001 -- nothing to recover; say so
        print(f"verify-proxy: tearing down kept lab {run_id} failed: {e!r}", flush=True)
    with _advice_cv:
        data = _advice_runs.get(run_id)
        if data is not None and data.get("kept"):
            data["kept"] = dict(data["kept"], destroyed=True, reason=reason)
    print(f"verify-proxy: kept lab {run_id} destroyed ({reason})", flush=True)
    return True


def _kept_reaper() -> None:
    while True:
        time.sleep(30)
        now = time.time()
        with _kept_lock:
            due = [rid for rid, e in _kept_labs.items() if e["expires"] <= now]
        for rid in due:
            _release_kept(rid, "expired")


def _advice_sizing():
    from microvm import VmSizing
    return VmSizing(
        mem_target_mib=int(os.environ.get("ADVICE_MEM_TARGET_MIB", "2048")),
        mem_floor_mib=int(os.environ.get("ADVICE_MEM_FLOOR_MIB", "1024")),
        vcpu_target=int(os.environ.get("ADVICE_VCPU_TARGET", "2")),
        scratch_target_mib=int(os.environ.get("ADVICE_SCRATCH_MIB", "16384")),
        scratch_floor_mib=int(os.environ.get("ADVICE_SCRATCH_FLOOR_MIB", "4096")),
    )


def _advice_store(run_id: str, data: dict) -> None:
    with _advice_cv:
        _advice_runs[run_id] = data
        _advice_runs.move_to_end(run_id)
        while len(_advice_runs) > _ADVICE_KEEP:
            _advice_runs.popitem(last=False)


_MODEL_FIX_SYSTEM = (
    "A step from a Linux how-to answer failed in a fresh Ubuntu 22.04 server (a disposable lab machine; "
    "you are the student's helper, commands run as a normal user with passwordless sudo). Reply with ONE shell "
    "command, on one line, that fixes the cause so the same step will succeed when run again -- for example "
    "installing a missing package, creating a missing directory, or fixing a permission. No explanation, no "
    "code fences. If nothing sensible would fix it, reply exactly NONE.")


# L16/L17: before an answer is tried, the model says what the question takes
# for granted on the machine and which tokens are placeholders. The lab acts
# on this only through a fixed menu (advice_runner.plan_setup), so a wrong
# reading can at worst create a harmless user, file or package.
_PRESUME_SYSTEM = (
    "You read a Linux how-to question and the answer given to it, before the answer is tried on a FRESH "
    "Ubuntu 22.04 server that has nothing extra installed and only the user 'student'. Reply with ONLY a JSON "
    "object, no prose, no code fences:\n"
    '{"setup": [...], "placeholders": [...], "checks": [...]}\n'
    "\"setup\" lists what the QUESTION treats as ALREADY existing before the answer starts, which a fresh "
    "server lacks. Each item is one of:\n"
    '  {"action":"user","name":"..."}\n'
    '  {"action":"group","name":"...","members":["..."]}\n'
    '  {"action":"package","name":"<apt package>","running":true|false}\n'
    '  {"action":"service","name":"...","running":true|false}\n'
    '  {"action":"dir","path":"/...","owner":"..."}\n'
    '  {"action":"file","path":"/...","owner":"..."}\n'
    "A file or directory the answer uses by a relative name (file1.txt, ./run.sh) is in the student's "
    "home: give it as /home/student/<name>.\n"
    "NEVER list what the question asks to create, install or configure -- that is the answer's job. Only "
    "list what must be there for the answer to make sense (an existing user it modifies, a server it "
    "configures but does not install, a file or directory it reads).\n"
    "\"placeholders\" lists tokens in the ANSWER's commands that stand for a value the reader must fill "
    "in, each as {\"token\": \"<exact text as written>\", \"kind\": K} with K one of: user, group, "
    "this_machine_ip, other_machine_ip, uuid, pid, service, package, path, domain. Only invented "
    "stand-ins, never real names like nginx, eth0, /etc/hosts or root.\n"
    "Example. Question: \"How do I let user maria edit files in the web root?\" Answer uses "
    "`sudo setfacl -m u:maria:rwX /var/www/html` and `ssh maria@your_server_ip`. Reply: "
    '{"setup":[{"action":"user","name":"maria"},{"action":"dir","path":"/var/www/html"}],'
    '"placeholders":[{"token":"your_server_ip","kind":"this_machine_ip"}]}\n'
    "Second example. Question: \"How do I let the web server read files in /srv/app?\" Answer runs "
    "`sudo chgrp -R www-data /srv/app` and `sudo systemctl reload nginx`, never installing nginx. Reply: "
    '{"setup":[{"action":"package","name":"nginx","running":true},{"action":"dir","path":"/srv/app"}],'
    '"placeholders":[]}\n'
    "Software the question names or the answer relies on (a web server, a database, Docker, Samba ...) "
    "is presumed installed unless the answer installs it.\n"
    "\"checks\" (L21) says how to confirm, on this machine after the answer has run, that what the "
    "QUESTION asked for was achieved. Use names and paths from the answer. Only for questions that change "
    "the machine; for questions that only look at something, or explain something, give []. Each is one of:\n"
    '  {"kind":"user_exists","user":"..."}  {"kind":"user_in_group","user":"...","group":"..."}\n'
    '  {"kind":"user_shell","user":"...","shell":"/bin/..."}  {"kind":"path_exists","path":"/..."}\n'
    '  {"kind":"path_owner","path":"/...","owner":"user[:group]"}  {"kind":"path_mode","path":"/...","mode":"755|sticky|setgid"}\n'
    '  {"kind":"file_contains","path":"/...","text":"<a line or part of one>"}\n'
    '  {"kind":"service_active","unit":"..."}  {"kind":"service_enabled","unit":"..."}  {"kind":"service_disabled","unit":"..."}\n'
    '  {"kind":"port_listening","port":N}  {"kind":"port_open_from_other","port":N}  {"kind":"port_closed_from_other","port":N}\n'
    '  {"kind":"default_target","target":"....target"}  {"kind":"sysctl","key":"...","value":"..."}\n'
    '  {"kind":"command_output","command":"<one read-only command, no pipes>","contains":"..."}\n'
    "Behaviour checks (L23) -- PREFER these: they test what a person would see, not what a file says:\n"
    '  {"kind":"login_env","user":"...","var":"NAME","contains":"..."}  (a fresh login session)\n'
    '  {"kind":"user_can"|"user_cannot","user":"...","action":"read|write|execute|list","path":"/..."}\n'
    '  {"kind":"sudo_allowed"|"sudo_denied","user":"...","command":"<the exact command>"}\n'
    '  {"kind":"http_from_other","port":N,"path":"/...","host":"<Host header, optional>",'
    '"status":"200|301|404|3xx...","contains":"<body text, optional>","location":"<redirect target, optional>"}\n'
    '  {"kind":"resolves","name":"...","address":"..."}  {"kind":"unit_runs_ok","unit":"<service or timer>"}\n'
    '  {"kind":"sshd_effective","key":"<sshd option>","value":"..."}  (what sshd really uses)\n'
    "Add \"after_reboot\":true to a check when the question asks for something permanent, at boot, or to "
    "survive a reboot.\n"
    "Prefer login_env over file_contains for variables, user_can/user_cannot over path_mode for permissions, "
    "sudo_allowed/sudo_denied for sudo rights, http_from_other for web servers, sshd_effective for SSH "
    "settings. Never check only that a file contains a line the answer writes.\n"
    "Prefer the most direct check of the goal (for \"let maria edit /var/www/html\": user_in_group or path_owner, "
    "not file_contains; for \"let another machine reach X\": port_open_from_other). Check the change the "
    "question asks for, not only that the software is installed or running. At most 4 checks.\n"
    'If nothing applies, reply {"setup":[],"placeholders":[],"checks":[]}.')


def _answer_code(answer: str, limit: int = 3000) -> str:
    """The answer's fenced blocks, each with the line before it (which names
    a file's path): what the lab will run. The 14B model on CPU spends most
    of a call reading its input, and the prose adds nothing here."""
    lines, out, i = answer.splitlines(), [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("```"):
                j += 1
            if i > 0 and lines[i - 1].strip():
                out.append(lines[i - 1].strip())
            out.extend(lines[i:j + 1])
            i = j + 1
        else:
            i += 1
    return ("\n".join(out) or answer)[:limit]


# C1 (llm-chat-placement-Phased-Implementation.md): students first. Every
# model call the lab makes -- readings, repairs, another way -- waits while a
# student's question is being answered on this model, so students get the
# whole 14B. The lab's work is unchanged, only later. Waiting is bounded so
# the lab never stalls for good.
STUDENT_PRIORITY_WAIT_S = int(os.environ.get("STUDENT_PRIORITY_WAIT_S", "1800"))
_students_active = 0
_students_cv = threading.Condition()


class _StudentRequest:
    """`with _StudentRequest():` around a student's request."""

    def __enter__(self):
        global _students_active
        with _students_cv:
            _students_active += 1

    def __exit__(self, *exc):
        global _students_active
        with _students_cv:
            _students_active -= 1
            _students_cv.notify_all()


def _students_first(what: str) -> None:
    """Hold a lab model call while students are being answered."""
    with _students_cv:
        if _students_active == 0:
            return
        t0 = time.monotonic()
        print(f"verify-proxy: {what} waits for {_students_active} student request(s)", flush=True)
        _students_cv.wait_for(lambda: _students_active == 0, timeout=STUDENT_PRIORITY_WAIT_S)
        print(f"verify-proxy: {what} went ahead after {time.monotonic() - t0:.0f}s", flush=True)


# L25: readers -- small models on other instances that do the lab's readings,
# so they don't queue behind students' answers on this coordinator's model.
LAB_READER_URLS = [u.strip().rstrip("/") for u in os.environ.get("LAB_READER_URLS", "").split(",") if u.strip()]


_floor_ok: dict[str, tuple[float, bool]] = {}


def _meets_floor(url: str) -> bool:
    """C2: a lab endpoint is used only if it serves our own model -- never a
    smaller one (direct request: "I don't want quality compromised").
    Asked via llama-server's /props, remembered for 5 minutes."""
    now = time.monotonic()
    hit = _floor_ok.get(url)
    if hit and now - hit[0] < 300:
        return hit[1]
    own = os.environ.get("EXAMPLES_MODEL_FILENAME", "")
    ok = False
    try:
        with urllib.request.urlopen(url + "/props", timeout=3) as resp:
            props = json.loads(resp.read())
        served = os.path.basename(str(props.get("model_path") or props.get("default_generation_settings", {}).get("model", "")))
        ok = bool(own) and served == own
        if not ok:
            print(f"verify-proxy: lab endpoint {url} serves {served!r}, not {own!r}: not used (quality floor)", flush=True)
    except Exception:  # noqa: BLE001 -- unreachable: not used
        ok = False
    _floor_ok[url] = (now, ok)
    return ok


def _idle_reader() -> str:
    """The first lab endpoint serving our own model with a free slot right
    now, or "" (use our own model, students first). Asked per call: lab
    endpoints come and go, and get busy."""
    for url in LAB_READER_URLS:
        if not _meets_floor(url):
            continue
        try:
            with urllib.request.urlopen(url + "/slots", timeout=3) as resp:
                slots = json.loads(resp.read())
        except Exception:  # noqa: BLE001 -- down or unreachable: try the next
            continue
        if isinstance(slots, list) and any(not s.get("is_processing") for s in slots if isinstance(s, dict)):
            return url
    return ""


def _lab_chat(payload: dict, what: str, timeout: int) -> str:
    """One chat completion for the lab: on an idle lab endpoint (C2/C5), else
    on our own model once no student is waiting (C1). Raises on failure."""
    lab = _idle_reader()
    if not lab:
        _students_first(what)
    for base in ([lab] if lab else []) + [f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}"]:
        try:
            req = urllib.request.Request(f"{base}/v1/chat/completions", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            if base == lab:
                print(f"verify-proxy: {what} on {lab} failed ({e!r}); using our own model", flush=True)
                _students_first(what)
                continue
            raise
    raise RuntimeError("no model answered")


def _model_presumptions(question: str, answer: str) -> dict:
    """L16/L17: the model's reading of what the question presumes and which
    tokens are placeholders. {} on any failure: the run goes on without it."""
    payload = {"messages": [
        {"role": "system", "content": _PRESUME_SYSTEM},
        {"role": "user", "content": f"Question: {question}\n\nAnswer (its commands and files):\n{_answer_code(answer)}"}],
        "max_tokens": 700, "stream": False, "temperature": 0.0}
    reader = _idle_reader()  # recorded with the run: which model read it
    try:
        text = _lab_chat(payload, "a lab reading", 600)
    except Exception as e:  # noqa: BLE001 -- an aid, never a hard dependency
        print(f"verify-proxy: presumptions request failed: {e!r}", flush=True)
        return {}
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if start >= 0 < end else {}
    except ValueError:
        print(f"verify-proxy: presumptions reply wasn't JSON: {text[:200]!r}", flush=True)
        return {}
    if isinstance(data, dict):
        data["_reader"] = reader or "coordinator"
        return data
    return {}


def _model_fix(question: str, step: str, output: str) -> str:
    """L10b: the model's one-line fix for one failed step (the lab's own
    repair strategies having failed). "" on any failure."""
    payload = {"messages": [
        {"role": "system", "content": _MODEL_FIX_SYSTEM},
        {"role": "user", "content": f"The question was: {question}\n\nThe step:\n{step}\n\n"
                                    f"What it printed (last part):\n{output}"}],
        "max_tokens": 80, "stream": False, "temperature": 0.2}
    try:
        text = _lab_chat(payload, "a lab repair", 600).strip()
    except Exception as e:  # noqa: BLE001 -- a repair aid, never a hard dependency
        print(f"verify-proxy: model fix request failed: {e!r}", flush=True)
        return ""
    line = next((ln.strip().strip("`") for ln in text.splitlines() if ln.strip() and not ln.startswith("```")), "")
    return line[:300]


# L11: try another way. When an answer still fails in the lab after the
# lab's own repairs, the model is shown what actually failed and asked for
# a different method, which a fresh lab machine then tries -- up to
# ADVICE_RETRIES times, stopping at the first that works. Direct request:
# "If something doesn't work on a lab vm ... it can be noted as failed,
# create the vm and try another way/method."
ADVICE_RETRIES = int(os.environ.get("ADVICE_RETRIES", "2"))
# F4: "full" proves answers on throwaway full VMs from CloudCore's lab-VM
# broker (its guest-facing address and its own token); anything else keeps
# the microVM lab.
LAB_BACKEND = os.environ.get("LAB_BACKEND", "microvm")
LABVM_BROKER_URL = os.environ.get("LABVM_BROKER_URL", "")
LABVM_BROKER_TOKEN = os.environ.get("LABVM_BROKER_TOKEN", "")
_RETRY_INSTRUCTION = (
    "Your previous answer was tried step by step, exactly as written, on a fresh Ubuntu 22.04 server (a lab "
    "machine), and it did not work. What happened is below. Answer the question again with a DIFFERENT method "
    "that avoids what failed -- not the same steps with small changes. Use the same format as before: a short "
    "explanation, then every command in a fenced bash block, and any file's full contents in a fenced block "
    "with the file's path named just before it. Write it as a complete answer for someone who never saw the "
    "previous one: don't mention it, the lab, or what failed (found in L11: a verified alternative reused for "
    "another student began \"It seems that the nmcli command failed\").")


def _failure_report(result) -> str:
    """What failed in a run, for the model: failing steps with their real
    output, and failed checks. Lab limits are left out: they're not the
    answer's fault."""
    lines = [f"Result: {result.summary}"]
    for s in result.steps:
        if s.get("cls") in ("ok", "skipped", "repaired", "lab_limit", "", None):
            continue
        first = (s.get("source") or "").strip().splitlines()[:1]
        lines.append(f"- step {s['n']} `{first[0][:160] if first else s.get('kind')}`: {(s.get('detail') or '')[:240]}"
                     + (f"\n  its output ended with: {(s.get('output') or '').strip()[-400:]}" if s.get("output") else ""))
    for c in result.checks:
        if not c.get("ok") and c.get("decisive", True):
            lines.append(f"- check failed: {c['subject']}" + (f" ({(c.get('detail') or '')[:200]})" if c.get("detail") else ""))
    if getattr(result, "diagnosis", ""):
        lines = lines[:16] + ["What the machine itself reported afterwards:", result.diagnosis]
        return "\n".join(lines)
    return "\n".join(lines[:16])


_PREVIOUS_RE = re.compile(r"\b(?:previous|earlier|last|prior|first)\b[^.!?\n]*\b(?:attempt|method|answer|setup|approach|"
                          r"step|try|configuration|run)|\b(?:failed|did not work|didn't work|encountered)\b|"
                          r"\b(?:different|another|alternative|new)\s+(?:approach|method|way)\b|\bagain\b|"
                          r"\b(?:the|this)\s+(?:issue|problem|error)\b",
                          re.IGNORECASE)


def _standalone(alt: str) -> str:
    """Drop opening-paragraph sentences about the previous attempt: the
    model doesn't reliably follow "don't mention it" (found in L11:
    "This method avoids the issue encountered in the previous attempt"),
    and the answer may be reused for someone who never saw it."""
    head, sep, rest = alt.partition("\n\n")
    if "```" in head:
        return alt
    kept = [s for s in re.split(r"(?<=[.!?])\s+", head.strip()) if s and not _PREVIOUS_RE.search(s)]
    return (" ".join(kept) + sep + rest) if kept else rest.lstrip()


def _commands_of(answer: str) -> set:
    """The answer's command lines, normalised, to tell methods apart."""
    out = set()
    for block in re.findall(r"```[^\n]*\n(.*?)```", answer, re.DOTALL):
        for ln in block.splitlines():
            ln = re.sub(r"\s+", " ", ln.strip().removeprefix("sudo "))
            if ln and not ln.startswith("#"):
                out.add(ln)
    return out


def _same_method(a: str, b: str) -> bool:
    ca, cb = _commands_of(a), _commands_of(b)
    return bool(ca and cb) and len(ca & cb) / len(ca | cb) >= 0.8


def _model_alternative(question: str, answer: str, report: str, search_terms: str,
                       temperature: float = 0.4, nudge: str = "") -> str:
    """A different answer to `question`, given what failed. "" on failure."""
    payload = {"messages": [
        {"role": "system", "content": LINUX_SYSTEM_MESSAGE},
        {"role": "user", "content": _lab_facts(search_terms) + question},
        {"role": "assistant", "content": answer},
        {"role": "user", "content": _RETRY_INSTRUCTION + (" " + nudge if nudge else "") + "\n\n" + report}],
        "max_tokens": 1200, "stream": False, "temperature": temperature}
    try:
        return _standalone(_lab_chat(payload, "a lab 'another way' answer", 3600).strip())
    except Exception as e:  # noqa: BLE001 -- another way is a bonus; the original result stands
        print(f"verify-proxy: alternative-answer request failed: {e!r}", flush=True)
        return ""


def _set_attempts(run_id: str, attempts: list, retrying: bool) -> None:
    with _advice_cv:
        data = _advice_runs.get(run_id)
        if data is not None:
            data["attempts"] = [dict(a) for a in attempts]
            data["retrying"] = retrying


def _final_verdict(result) -> str:
    return (result.repaired or {}).get("verdict") or result.verdict


def _try_other_ways(run_id: str, question: str, answer: str, search_terms: str, result, run_one) -> None:
    attempts: list = []
    prev_answer, prev = answer, result
    for k in range(1, ADVICE_RETRIES + 1):
        if _final_verdict(prev) != "failed":
            break
        att = {"n": k, "status": "writing", "id": "", "answer": "", "verdict": "", "summary": "", "lines": []}
        attempts.append(att)
        _set_attempts(run_id, attempts, True)
        report = _failure_report(prev)
        alt = _model_alternative(question, prev_answer, report, search_terms)
        tried = [answer] + [a["answer"] for a in attempts if a.get("answer")]
        if alt and any(_same_method(alt, x) for x in tried):
            # Found in L11: WireGuard's second attempt was word for word the
            # first. Ask once more, harder; never spend a lab run re-proving it.
            alt = _model_alternative(question, prev_answer, report, search_terms, temperature=0.9,
                                     nudge="Your last reply repeated a method that has already failed in the lab; "
                                           "use a genuinely different tool or approach.")
            if alt and any(_same_method(alt, x) for x in tried):
                att.update(status="done", verdict="", answer=alt,
                           summary="The model offered a method that had already failed in the lab, so it wasn't run "
                                   "again.")
                break
        if not alt:
            att.update(status="done", summary="The model didn't produce another answer.")
            break
        child = uuid.uuid4().hex[:12]
        att.update(status="running", id=child, answer=alt)
        _set_attempts(run_id, attempts, True)

        def progress(r, _att=att):
            import advice_runner
            _att["lines"] = [advice_runner.plain_step_line(s) for s in r.to_dict()["steps"]]
            _set_attempts(run_id, attempts, True)

        res = run_one(alt, child, progress)
        import advice_runner
        att.update(status="done", verdict=res.verdict, summary=res.summary,
                   repaired=(res.repaired or {}).get("verdict", ""),
                   lines=[advice_runner.plain_step_line(s) for s in res.to_dict()["steps"]])
        _set_attempts(run_id, attempts, k < ADVICE_RETRIES and _final_verdict(res) == "failed")
        entry = {"id": res.id, "source": "retry", "question": question, "answer": alt,
                 "search_terms": search_terms, "verdict": res.verdict, "summary": res.summary, "error": res.error,
                 "vm": res.vm, "steps": res.steps, "checks": res.checks, "repaired": res.repaired,
                 "attempt_of": run_id, "attempt": k, "diagnosis": res.diagnosis}
        print("ADVICE_RUN " + json.dumps({k2: v for k2, v in entry.items() if k2 != "answer"}), flush=True)
        if SENTINEL_HOST:
            # The alternative is an answer in its own right: logged like an
            # asked one first, so a goal-verified run can promote it for reuse.
            _push_grounding_to_sentinel({"endpoint": "/sandbox/linux-ask", "question": question, "answer": alt,
                                         "search_terms": search_terms, "references": [], "grounded": False,
                                         "grounding_source": "lab-retry"})
            _push_advice_run_to_sentinel(entry)
        prev_answer, prev = alt, res
    _set_attempts(run_id, attempts, False)


def _advice_worker() -> None:
    global _advice_pool
    import advice_runner
    from microvm import PAIR_SUBNET, IpPool, MicroVM, VmSizing, create_pair_bridge, delete_pair_bridge
    _advice_pool = IpPool(SANDBOX_SUBNET_CIDR, part="advice")
    sizing = _advice_sizing()
    # L8: the prober only needs to log in, connect and fetch -- the smallest
    # VM that boots the image. Its only NIC is the run's private pair bridge.
    prober_sizing = VmSizing(mem_target_mib=512, mem_floor_mib=384, vcpu_target=1,
                             scratch_target_mib=2048, scratch_floor_mib=1024)

    def make_target(**kw):
        # L13: a real ext4 root (device-mapper snapshot) and two blank 1GB
        # disks (/dev/sdb, /dev/sdc) for disk advice.
        return MicroVM("advc", _advice_pool, SANDBOX_BRIDGE, sizing=sizing, boot_timeout_s=90,
                       root="snapshot", spare_disks_mib=(1024, 1024), **kw)


    def make_prober(bridge):
        return MicroVM("advp", IpPool(PAIR_SUBNET), bridge, sizing=prober_sizing, isolate=False,
                       boot_timeout_s=90, extra_boot_args=_RUN_VM_BOOT_ARGS)
    # F4: proofs on throwaway full VMs from CloudCore's lab-VM broker, when
    # configured (LAB_BACKEND=full plus the broker's address and token).
    if LAB_BACKEND == "full" and LABVM_BROKER_URL and LABVM_BROKER_TOKEN:
        from fullvm import FullVMLabs
        _labs = FullVMLabs(LABVM_BROKER_URL, LABVM_BROKER_TOKEN)
        make_target, make_prober = _labs.make_target, _labs.make_prober
        create_pair_bridge, delete_pair_bridge = _labs.new_run, _labs.end_run
        print("verify-proxy: lab runs use full VMs via " + LABVM_BROKER_URL, flush=True)
    threading.Thread(target=_kept_reaper, daemon=True).start()
    while True:
        with _advice_cv:
            while not _advice_waiting:
                _advice_cv.wait()
            run_id = _advice_waiting.pop(0)
            job = _advice_runs.get(run_id, {})
        answer, question = job.get("_answer", ""), job.get("question", "")
        search_terms = job.get("_search_terms", "")
        token = job.get("_token", "")
        # Room for this run's machine: never more than ADVICE_KEEP_MAX kept
        # counting the one this run may keep.
        with _kept_lock:
            surplus = list(_kept_labs)[:max(0, len(_kept_labs) - (ADVICE_KEEP_MAX - 1))]
        for rid in surplus:
            _release_kept(rid, "made room for a newer run")

        def keep(vm, info, _rid=run_id, _tok=token):
            if ADVICE_KEEP_MINUTES <= 0 or ADVICE_KEEP_MAX <= 0 or not getattr(vm, "keepable", True):
                return False
            expires = time.time() + ADVICE_KEEP_MINUTES * 60
            with _kept_lock:
                _kept_labs[_rid] = {"vm": vm, "expires": expires, "token": _tok}
            with _advice_cv:
                data = _advice_runs.get(_rid)
                if data is not None:
                    data["_kept_info"] = info
            return True

        def publish(result, _q=question, _tok=token):
            data = result.to_dict()
            data["question"] = _q
            data["_token"] = _tok
            data["lines"] = [advice_runner.plain_step_line(s) for s in data["steps"]]
            with _advice_cv:
                old = _advice_runs.get(result.id) or {}
                if "_kept_info" in old:
                    data["_kept_info"] = old["_kept_info"]
            _advice_store(result.id, data)

        try:
            result = advice_runner.run_advice(
                answer, make_target, progress=publish, run_id=run_id, question=question,
                make_prober=make_prober, pair_bridges=(create_pair_bridge, delete_pair_bridge),
                keep_vm=keep, model_fix=_model_fix, presume=_model_presumptions)
        except Exception as e:  # noqa: BLE001 -- one broken run must never stop the queue
            # Found in the held-out run: a parser bug on one answer killed this
            # thread, and every later run sat "waiting to start" for 11 hours.
            traceback.print_exc()
            with _advice_cv:
                data = _advice_runs.get(run_id)
                if data is not None:
                    data.update(status="error", verdict="", error=f"{type(e).__name__}: {e}"[:300],
                                summary="The lab couldn't run this answer (an internal error, not the answer's fault).")
            continue
        with _kept_lock:
            kept = _kept_labs.get(run_id)
        if kept is not None:
            with _advice_cv:
                data = _advice_runs.get(run_id)
                if data is not None:
                    data["kept"] = {"expires_at": kept["expires"]}
        entry = {"id": result.id, "source": "auto", "question": question, "answer": answer,
                 "diagnosis": result.diagnosis,
                 "search_terms": search_terms, "verdict": result.verdict,
                 "summary": result.summary, "error": result.error, "vm": result.vm,
                 "steps": result.steps, "checks": result.checks, "repaired": result.repaired,
                 "setup": result.setup}
        print("ADVICE_RUN " + json.dumps({k: v for k, v in entry.items() if k != "answer"}), flush=True)
        if SENTINEL_HOST:
            _push_advice_run_to_sentinel(entry)

        def run_one(alt, child, progress):
            return advice_runner.run_advice(
                alt, make_target, progress=progress, run_id=child, question=question, make_prober=make_prober,
                pair_bridges=(create_pair_bridge, delete_pair_bridge), model_fix=_model_fix,
                presume=_model_presumptions)
        if ADVICE_RETRIES > 0 and _final_verdict(result) == "failed":
            _try_other_ways(run_id, question, answer, search_terms, result, run_one)


def _start_advice_run(answer: str, question: str, search_terms: str = "") -> tuple[str, str] | None:
    """Queue a run of `answer`; returns (id, token), or None when disabled.
    The token goes only to the person who asked: it unlocks the kept lab
    machine (login details, Terminal, Destroy)."""
    global _advice_worker_started
    if not ADVICE_RUN_ENABLED:
        return None
    run_id = uuid.uuid4().hex[:12]
    with _advice_cv:
        if not _advice_worker_started:
            threading.Thread(target=_advice_worker, daemon=True).start()
            _advice_worker_started = True
        token = secrets.token_hex(16)
        _advice_runs[run_id] = {"id": run_id, "status": "queued", "question": question,
                                "_answer": answer, "_search_terms": search_terms, "_token": token}
        _advice_runs.move_to_end(run_id)
        _advice_waiting.append(run_id)
        _advice_cv.notify()
    return run_id, token


def _token_ok(data: dict, token: str) -> bool:
    return bool(token) and hmac.compare_digest(str(data.get("_token", "")), token)


def _advice_status(run_id: str, token: str = "") -> dict:
    with _advice_cv:
        data = _advice_runs.get(run_id)
        if data is None:
            return {"id": run_id, "status": "unknown"}
        out = {k: v for k, v in data.items() if not k.startswith("_")}
        # Only the asker sees how to get into the kept machine.
        if out.get("kept") and _token_ok(data, token):
            out["kept"] = dict(out["kept"], **data.get("_kept_info", {}))
        elif out.get("kept"):
            out["kept"] = {k: v for k, v in out["kept"].items() if k in ("expires_at", "destroyed")}
        if out.get("status") == "queued":
            out["queue_position"] = _advice_waiting.index(run_id) + 1 if run_id in _advice_waiting else 0
    return out


def _mem_available_mb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def _empty_run_result(language: str) -> dict:
    return {"transcript": "", "stdout": "", "stderr": "", "exit_code": None,
            "timed_out": False, "interrupted": False, "exchanges": 0, "detections": [],
            "phase": "run", "language": language, "timings": {}}


def _ssh_exec(client, cmd: str, timeout: float) -> tuple[int, str, str, bool]:
    """Runs `cmd` in the guest with stdin closed; returns (exit_code,
    stdout, stderr, timed_out). Used for the compile step and the
    stdin-wait probe; never for the student program itself."""
    chan = client.get_transport().open_session()
    chan.exec_command(cmd)
    chan.shutdown_write()
    out, err = [], []
    deadline = time.monotonic() + timeout
    while True:
        while chan.recv_ready():
            out.append(chan.recv(65536))
        while chan.recv_stderr_ready():
            err.append(chan.recv_stderr(65536))
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if time.monotonic() > deadline:
            chan.close()
            return (-1, b"".join(out).decode(errors="replace"),
                    b"".join(err).decode(errors="replace"), True)
        time.sleep(0.05)
    rc = chan.recv_exit_status()
    chan.close()
    return rc, b"".join(out).decode(errors="replace"), b"".join(err).decode(errors="replace"), False


def run_in_microvm(language: str, code: str, provide_input=None, interrupt=None,
                   ticket: str | None = None) -> dict:
    """Stage 12's counterpart to run_sandboxed()/run_sandboxed_interactive()
    for every non-Python language: boot a fresh jailed microVM, copy the
    source in, compile if the language needs it, run, tear the whole VM
    down. Same result shape as run_sandboxed_interactive() (so the
    formatters and fix loop don't care which ran it), plus `phase`
    ("compile" or "run"), `language` and `timings`.

    With `provide_input` None this is a plain Run: stdin is closed and
    the wall-clock limit is VERIFY_TIMEOUT_S, same as run_sandboxed().
    Otherwise it's interactive exactly like run_sandboxed_interactive():
    the guest-side /usr/local/bin/stdin-wait-check (the same rules as
    _stdin_wait_state()) decides when the program is waiting, falling
    back to the INTERACTIVE_QUIET_S heuristic when it can't tell."""
    from microvm import BootError, MicroVM

    spec = LANGUAGES[language]
    result = _empty_run_result(language)
    ticket = ticket or uuid.uuid4().hex
    t_queue = time.monotonic()
    if not _run_queue.acquire(ticket, RUN_QUEUE_WAIT_S, interrupt):
        result["interrupted"] = interrupt is not None and interrupt.is_set()
        result["stderr"] = ("[Stopped while waiting for a free sandbox]" if result["interrupted"] else
                            f"[Every sandbox stayed busy for {RUN_QUEUE_WAIT_S}s -- try again shortly]")
        return result
    result["timings"]["queued_s"] = round(time.monotonic() - t_queue, 2)

    mem_mib = RUN_VM_MEM_MIB_GO if language == "go" else RUN_VM_MEM_MIB
    vm = client = None
    try:
        avail = _mem_available_mb()
        if avail < RUN_VM_MIN_HOST_MEM_MB + mem_mib:
            result["stderr"] = (f"[Not started: the coordinator is under memory pressure "
                                f"({avail}MB available, {RUN_VM_MIN_HOST_MEM_MB + mem_mib}MB needed). "
                                f"Try again once the current answer finishes.]")
            return result

        vm = MicroVM("run", _get_run_pool(), RUN_BRIDGE, vcpu_count=1,
                     mem_size_mib=mem_mib, scratch_mib=1024,
                     boot_timeout_s=RUN_BOOT_TIMEOUT_S, extra_boot_args=_RUN_VM_BOOT_ARGS)
        t0 = time.monotonic()
        try:
            vm.boot()
            client = vm.ssh_client()
        except (BootError, OSError) as e:
            result["stderr"] = f"[Sandbox failed to start: {e}]"
            return result
        result["timings"]["boot_s"] = round(time.monotonic() - t0, 2)

        client.exec_command(f"mkdir -p {_RUN_DIR}")[1].channel.recv_exit_status()
        sftp = client.open_sftp()
        with sftp.file(f"{_RUN_DIR}/{spec['file']}", "w") as f:
            f.write(code)
        sftp.close()

        if spec["compile"]:
            t0 = time.monotonic()
            rc, out, err, timed_out = _ssh_exec(
                client, f"cd {_RUN_DIR} && {spec['compile']}", RUN_COMPILE_TIMEOUT_S)
            result["timings"]["compile_s"] = round(time.monotonic() - t0, 2)
            if rc != 0:
                result["phase"] = "compile"
                result["exit_code"] = rc
                result["timed_out"] = timed_out
                text = (out + err).strip()
                if timed_out:
                    text += f"\n[Compilation killed: exceeded {RUN_COMPILE_TIMEOUT_S}s]"
                result["stderr"] = text[:MAX_OUTPUT_CHARS]
                result["transcript"] = result["stderr"]
                return result

        # ulimit -t: the same CPU-seconds cap run_sandboxed() applies with
        # RLIMIT_CPU. -u keeps a fork bomb from making the guest too busy
        # to answer our own SSH. The program runs as a CHILD of this shell,
        # not exec'd over it: sshd reports a signal death as a signal,
        # which paramiko surfaces only as exit status -1 (found live: a C
        # segfault showed "exit code -1" and nothing else), so the shell
        # names the signal itself. .pid holds the shell's PID;
        # stdin-wait-check walks its descendants, so it still finds the
        # program.
        run_cmd = (
            # Hard CPU limit 1s above the soft one: at the soft limit the
            # kernel sends SIGXCPU (named below as a CPU-time overrun); with
            # both equal, as plain `ulimit -t` sets them, it goes straight
            # to an anonymous SIGKILL. Soft first: lowering the hard limit
            # below a still-unlimited soft one fails with EINVAL.
            # `|| exit 125`: if ANY limit fails to apply the program must not
            # run at all (found locally: a failed ulimit chain followed by
            # `;` ran the program with no CPU limit).
            f"cd {_RUN_DIR} && ulimit -S -t {VERIFY_TIMEOUT_S} && ulimit -H -t {VERIFY_TIMEOUT_S + 1} && "
            f"ulimit -u 128 -f 10240 && echo $$ > {_RUN_PIDFILE} || "
            f"{{ echo '[sandbox limits could not be applied -- not run]' >&2; exit 125; }}; "
            f"{spec['run']}; rc=$?; "
            f"if [ $rc -gt 128 ]; then sig=$(kill -l $((rc-128))); "
            f"msg=\"[program killed by signal SIG$sig\"; "
            f"[ \"$sig\" = XCPU ] && msg=\"$msg: exceeded the {VERIFY_TIMEOUT_S}s CPU-time limit\"; "
            f"echo \"$msg]\" >&2; fi; exit $rc")
        chan = client.get_transport().open_session()
        chan.exec_command(run_cmd)
        if provide_input is None:
            chan.shutdown_write()

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        transcript: list[str] = []
        total = 0
        t0 = time.monotonic()
        last_output_at = t0
        last_probe = 0.0
        wall_limit = (VERIFY_TIMEOUT_S + 5) if provide_input is None else INTERACTIVE_MAX_WALL_S

        while True:
            if interrupt is not None and interrupt.is_set():
                result["interrupted"] = True
                break
            if time.monotonic() - t0 > wall_limit:
                result["timed_out"] = True
                break
            got = False
            while chan.recv_ready():
                text = chan.recv(65536).decode(errors="replace")
                stdout_parts.append(text); transcript.append(text); total += len(text); got = True
            while chan.recv_stderr_ready():
                text = chan.recv_stderr(65536).decode(errors="replace")
                stderr_parts.append(text); transcript.append(text); total += len(text); got = True
            if total > _RUN_OUTPUT_HARD_CAP:
                transcript.append("\n[Output limit reached -- program stopped]\n")
                break
            if got:
                last_output_at = time.monotonic()
                continue
            if chan.exit_status_ready():
                break
            if provide_input is not None and time.monotonic() - last_probe >= 0.5:
                last_probe = time.monotonic()
                _, probe, _, _ = _ssh_exec(
                    client, f"sudo -n /usr/local/bin/stdin-wait-check {_RUN_PIDFILE}", 5)
                state = probe.strip()
                if state == "waiting":
                    detection = "exact"
                elif state == "unknown" and time.monotonic() - last_output_at >= INTERACTIVE_QUIET_S:
                    detection = "heuristic"
                else:
                    detection = None
                if detection is not None:
                    if result["exchanges"] >= INTERACTIVE_MAX_EXCHANGES:
                        result["timed_out"] = True
                        break
                    value = provide_input("".join(transcript))
                    if value is None:
                        break
                    result["exchanges"] += 1
                    result["detections"].append(detection)
                    transcript.append(f"\n>>> INPUT PROVIDED ({detection}): {value!r}\n")
                    try:
                        chan.sendall((value + "\n").encode())
                    except OSError:
                        break
                    last_output_at = time.monotonic()
                continue
            time.sleep(0.05)

        result["timings"]["run_s"] = round(time.monotonic() - t0, 2)
        if chan.exit_status_ready():
            # Final drain: a last chunk can arrive together with the exit.
            while chan.recv_ready():
                text = chan.recv(65536).decode(errors="replace")
                stdout_parts.append(text); transcript.append(text)
            while chan.recv_stderr_ready():
                text = chan.recv_stderr(65536).decode(errors="replace")
                stderr_parts.append(text); transcript.append(text)
            # -1: killed by a signal (ulimit -t sends SIGXCPU/SIGKILL).
            result["exit_code"] = chan.recv_exit_status()
        if result["interrupted"]:
            transcript.append("\n[Stopped at your request]\n")
        elif result["timed_out"]:
            msg = (f"\n[Execution killed: exceeded {VERIFY_TIMEOUT_S}s]\n" if provide_input is None else
                   f"\n[Interactive session ended: exceeded its {INTERACTIVE_MAX_EXCHANGES}-exchange / "
                   f"{INTERACTIVE_MAX_WALL_S}s budget]\n")
            transcript.append(msg)
            stderr_parts.append(msg)
        result["stdout"] = "".join(stdout_parts)[:MAX_OUTPUT_CHARS]
        result["stderr"] = "".join(stderr_parts)[:MAX_OUTPUT_CHARS]
        result["transcript"] = "".join(transcript)[:MAX_OUTPUT_CHARS]
        return result
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if vm is not None:
            vm.teardown()
        _run_queue.release(ticket)


def execute(language: str, code: str, provide_input=None, interrupt=None,
            ticket: str | None = None) -> dict:
    """The one entry point Run/Ask use: Python on the coordinator's own
    unshare runner (unchanged), everything else in a per-run microVM."""
    if language == "python":
        if provide_input is None:
            r = run_sandboxed(code)
            r.setdefault("transcript", (r["stdout"] + r["stderr"]))
        else:
            r = run_sandboxed_interactive(code, provide_input, interrupt=interrupt)
        r.setdefault("phase", "run")
        r["language"] = "python"
        return r
    return run_in_microvm(language, code, provide_input, interrupt, ticket)


def _lang_suffix(result: dict) -> str:
    lang = result.get("language")
    return f" as {LANGUAGES[lang]['label']}" if lang and lang != "python" else ""


def _compile_failure_block(result: dict, final: bool) -> str | None:
    """Stage 12: a compile failure is its own, first-class result -- the
    program never ran, so there is no stdout/exit code of a run to show,
    only the compiler's own verbatim output."""
    if result.get("phase") != "compile":
        return None
    lines = [f"\n\n---\n### ACTUALLY COMPILED{_lang_suffix(result)} -- compilation failed (not model output)\n",
             "**compiler output:**\n```\n" + (result["stderr"].rstrip() or "(none)") + "\n```\n",
             f"**compiler exit code:** {result['exit_code']}\n"]
    if final:
        lines.append("\n_This code did not compile. You can ask for another attempt in your next message._\n")
    return "".join(lines)


def format_verification_block(result: dict, final: bool = True) -> str:
    """`final` controls only the trailing "ask again" invite -- an
    intermediate round in the Phase 2 fix loop is followed automatically
    by another attempt, so inviting the student to ask again there
    would be misleading; only the actual last block in a chain passes
    final=True."""
    compile_block = _compile_failure_block(result, final)
    if compile_block:
        return compile_block
    passed = (not result["timed_out"]) and result["exit_code"] == 0
    lines = [f"\n\n---\n### ACTUALLY EXECUTED{_lang_suffix(result)} (not model output)\n"]
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
    compile_block = _compile_failure_block(result, final)
    if compile_block:
        return compile_block
    passed = (not result["timed_out"]) and result["exit_code"] == 0
    lines = [f"\n\n---\n### ACTUALLY EXECUTED, interactively{_lang_suffix(result)} (not model output)\n"]
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
    interactive counterpart to extract_code(). Returns None
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
                          max_tokens: int | None = None, interrupt=None,
                          language: str = "python",
                          initial_inputs: list[str] | None = None) -> tuple[str, dict]:
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
    messages.append({"role": "assistant",
                     "content": f"```{LANGUAGES[language]['fence']}\n{code}\n```"})

    provider = _make_input_provider(messages, heartbeat, max_tokens, interrupt)
    if initial_inputs:
        # Values already known before the run (F-164) are handed over
        # first; the model is only consulted if the program asks for more.
        queued = list(initial_inputs)
        model_provider = provider

        def provider(transcript_so_far, _q=queued, _m=model_provider):
            return _q.pop(0) if _q else _m(transcript_so_far)
    result = execute(language, code, provider, interrupt=interrupt)
    capture = {
        "language": language,
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
        what = ("failed to compile with the following real compiler output"
                if result.get("phase") == "compile" else
                "was executed and failed with the following real output")
        fix_prompt = (
            f"This code {what}:\n\n```\n{(result['stderr'] or result['transcript'] or result['stdout']).strip()}\n```\n\n"
            "Explain exactly what is wrong, quoting the failing line, then "
            f"provide a corrected version of the complete program in a single "
            f"fenced ```{LANGUAGES[language]['fence']} block."
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

        extracted = extract_code(fix_text, language)
        new_code = extracted[1] if extracted else None
        if not new_code:
            blocks.append(
                "\n\n_No runnable code block found in this fix attempt._\n"
                + ("\n_This code did not run successfully. You can ask for "
                   "another attempt in your next message._\n" if is_last_round else "")
            )
            break

        language = extracted[0]
        result = execute(language, new_code, _make_input_provider(messages, heartbeat, max_tokens, interrupt),
                         interrupt=interrupt)
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
<script src="/vendor/codemirror-mode-javascript.min.js"></script>
<script src="/vendor/codemirror-mode-clike.min.js"></script>
<script src="/vendor/codemirror-mode-go.min.js"></script>
<script src="/vendor/codemirror-mode-shell.min.js"></script>
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
.sources { margin-top: 0.6rem; padding-top: 0.4rem; border-top: 1px solid #ddd; white-space: normal; font-size: 0.8rem; }
.sources-label { color: #555; margin-bottom: 0.2rem; }
.sources a { display: block; color: #1a5fb4; text-decoration: none; margin: 0.1rem 0; }
.sources a:hover { text-decoration: underline; }
.notice { margin-top: 0.5rem; padding: 0.35rem 0.55rem; border-radius: 4px; white-space: normal; font-size: 0.8rem; }
.notice.info { background: #eef1f5; border-left: 3px solid #8a94a6; color: #333; }
.notice.warn { background: #fff4e0; border-left: 3px solid #d98e04; color: #4a3300; }
.labrun { margin-top: 0.5rem; padding: 0.4rem 0.6rem; border-radius: 4px; white-space: normal; font-size: 0.8rem; border-left: 3px solid #8a94a6; background: #f3f5f8; }
.labrun.ok { border-left-color: #2e7d32; background: #edf7ee; }
.labrun.bad { border-left-color: #c62828; background: #fdecec; }
.labrun-head { font-weight: 600; margin-bottom: 0.2rem; }
.labrun-summary { margin-bottom: 0.3rem; }
.labrun-step { font-family: ui-monospace, monospace; font-size: 0.76rem; margin: 0.1rem 0; word-break: break-word; }
.labrun pre.labrun-live { max-height: 260px; overflow: auto; background: #0d0d0d; color: #d6dae3; padding: 0.45rem 0.55rem; font-size: 0.72rem; line-height: 1.35; white-space: pre-wrap; word-break: break-word; border-radius: 4px; margin: 0.35rem 0; }
.labrun-repaired { margin-top: 0.45rem; padding: 0.35rem 0.55rem; border-radius: 4px; border-left: 3px solid #d98e04; background: #fff8ea; }
.labrun-repaired.ok { border-left-color: #2e7d32; background: #eef8ef; }
.labrun-repaired pre { max-height: 260px; overflow: auto; background: #111; color: #ddd; padding: 0.4rem; font-size: 0.72rem; white-space: pre-wrap; }
.labrun-kept { margin-top: 0.45rem; padding: 0.4rem 0.55rem; border: 1px dashed #8a94a6; border-radius: 4px; background: #fbfcfe; }
.labrun-kept div { margin: 0.12rem 0; }
.labrun-kept-head { font-weight: 600; }
.labrun-kept-buttons { margin-top: 0.35rem; display: flex; gap: 0.4rem; flex-wrap: wrap; }
.labrun details pre { max-height: 220px; overflow: auto; background: #111; color: #ddd; padding: 0.4rem; font-size: 0.72rem; white-space: pre-wrap; }
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
<p class="sub">Write real code (Python, Bash, JavaScript, C, C++ or Go), run it for real, and ask the model about it -- every response you get back is grounded in an actual execution, not just the model's own word for it.</p>

<div class="panel">
  <h2>Your code</h2>
  <div id="codeHost"></div>
  <div class="row">
    <select id="langSel" onchange="setLanguage(this.value)" title="Language used by Run and by Ask the model">
      <option value="python">Python</option>
      <option value="bash">Bash</option>
      <option value="javascript">JavaScript (Node)</option>
      <option value="c">C</option>
      <option value="cpp">C++</option>
      <option value="go">Go</option>
    </select>
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
  <p class="sub" style="margin-bottom:0.75rem">Ask any Linux question -- from everyday commands to real system administration -- kept separate from the coding Ask panel above. Every answer is tried automatically, step by step, in a fresh Ubuntu 22.04 sandbox, and a second machine then checks what it was meant to achieve (logins, open ports, web servers) &mdash; the result appears under the answer. Use "Run in Terminal" to try a command yourself in the Terminal panel.</p>
  <div id="linuxTranscript"></div>
  <div class="row" style="align-items:flex-end">
    <textarea id="linuxQuestion" rows="4" placeholder="e.g. how do I check disk usage? -- or: how do I add a new user? (Shift+Enter for a new line)" onkeydown="if(event.key==='Enter' && !event.shiftKey){event.preventDefault();linuxAsk.askModel();}"></textarea>
    <button id="linuxAskBtn" class="primary" onclick="linuxAsk.askModel()">Ask</button>
    <button id="linuxStopBtn" onclick="linuxAsk.stopAsk()" disabled>Stop</button>
    <button id="linuxRegenBtn" onclick="linuxAsk.regenerateAsk()" disabled title="Ask again with no changes">Regenerate</button>
    <button onclick="clearLinuxHelp()">Clear session</button>
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

// Stage 12 -- the selected language drives the editor's highlighting,
// what Run executes, and what Ask tells the model to write. Remembered
// per browser only; a blocked localStorage just means Python each time.
const LANG_KEY = 'sandboxLang';
const LANG_MODES = {python: 'python', bash: 'shell', javascript: 'javascript',
                    c: 'text/x-csrc', cpp: 'text/x-c++src', go: 'go'};
function currentLang() { return document.getElementById('langSel').value; }
function setLanguage(lang) {
  if (!LANG_MODES[lang]) lang = 'python';
  document.getElementById('langSel').value = lang;
  cm.setOption('mode', LANG_MODES[lang]);
  cm.setOption('indentUnit', lang === 'go' ? 8 : 4);
  try { localStorage.setItem(LANG_KEY, lang); } catch (e) {}
}
(function () {
  let saved = null;
  try { saved = localStorage.getItem(LANG_KEY); } catch (e) {}
  setLanguage(saved || 'python');
})();

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
  //
  // cfg.runnableTags (F-174): when set, only fences with one of those
  // language tags get the panel's action; any other block (a config
  // file, PAM lines, example output) gets Copy instead -- otherwise
  // "Run in Terminal" on an sshd_config line types it into bash, it
  // fails, and the automatic diagnose turn chases a non-command.
  function renderAssistantContent(el, text) {
    el.innerHTML = '';
    // split() with two groups yields [text, tag, code, text, tag, code, ..., text].
    const parts = text.split(/```([a-zA-Z0-9_+-]*)\\n?([\\s\\S]*?)```/);
    for (let i = 0; i < parts.length; i += 3) {
      if (parts[i]) el.appendChild(document.createTextNode(parts[i]));
      if (i + 2 >= parts.length) break;
      const tag = parts[i + 1].toLowerCase();
      const code = parts[i + 2];
      const actionable = !cfg.runnableTags
        || (cfg.runnableTags.includes(tag) && !looksLikeConfigLine(code));
      const wrap = document.createElement('div');
      wrap.className = 'code-block';
      const pre = document.createElement('pre');
      pre.textContent = code;
      const btn = document.createElement('button');
      btn.className = 'use-code-btn';
      btn.textContent = actionable ? cfg.codeBlockLabel : 'Copy';
      btn.onclick = actionable ? () => cfg.onCodeBlock(code, statusEl)
                               : () => copyCodeBlock(code, btn);
      wrap.appendChild(pre);
      wrap.appendChild(btn);
      el.appendChild(wrap);
    }
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

  // K4 -- the kiwix pages behind an answer, as real links built with DOM
  // calls (never innerHTML). Only paths inside the /kiwix/ read-only
  // passthrough are accepted, so a malformed or hostile value can never
  // become a javascript: or off-site link.
  // F-175: cautions verify-proxy attaches to Linux Help answers, sent as
  // structured data (never model text) and set with textContent.
  function renderNotices(parentEl, notices) {
    for (const n of (notices || [])) {
      if (!n || typeof n.text !== 'string') continue;
      const box = document.createElement('div');
      box.className = 'notice ' + (n.level === 'warn' ? 'warn' : 'info');
      box.textContent = n.text;
      parentEl.appendChild(box);
    }
  }

  function renderSources(parentEl, sources) {
    const safe = (sources || []).filter(s => typeof s.link === 'string' && s.link.startsWith('/kiwix/content/'));
    if (!safe.length) return;
    const box = document.createElement('div');
    box.className = 'sources';
    const label = document.createElement('div');
    label.className = 'sources-label';
    label.textContent = 'Sources checked:';
    box.appendChild(label);
    for (const s of safe) {
      const a = document.createElement('a');
      a.href = s.link;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = (s.title || 'Reference') + ' (' + (s.source || 'kiwix') + ')';
      box.appendChild(a);
    }
    parentEl.appendChild(box);
  }

  // L4: the lab run of a Linux Help answer, rendered from the compact copy
  // kept in history (DOM calls and textContent only, like everything the
  // model or a run produces).
  const LAB_VERDICTS = {
    goal_verified: ['ok', 'Verified: every step worked in a fresh Ubuntu 22.04 sandbox, and the result was checked'],
    ran_clean: ['info', 'Every step worked, but nothing checked the result (not reused until reviewed)'],
    not_testable: ['info', "Can't be tested in this lab (it needs real hardware, a bootloader or kernel modules); not judged wrong"],
    lab_verified: ['ok', 'Verified: every step worked in a fresh Ubuntu 22.04 sandbox'],
    failed: ['bad', 'Tried in a fresh Ubuntu 22.04 sandbox: problems found'],
    partial: ['info', 'Tried in a fresh Ubuntu 22.04 sandbox'],
    not_runnable: ['info', 'Nothing in this answer could be run as a step'],
  };

  // L11: another way, after the answer failed even with the lab's repairs.
  function labAttemptBlock(a) {
    const div = document.createElement('div');
    const v = a.repaired || a.verdict;
    const good = ['goal_verified', 'lab_verified'].includes(v);
    div.className = 'labrun-repaired ' + (good ? 'ok' : (a.status !== 'done' || v === 'ran_clean' ? 'info' : 'bad'));
    const head = document.createElement('div');
    head.className = 'labrun-head';
    head.textContent = `Another way (attempt ${a.n}): ` + (
      a.status === 'writing' ? 'the model is writing a different method, given what failed…'
      : a.status === 'running' ? `trying it in a fresh sandbox (${a.lines.length} steps so far)…`
      : good ? 'this worked, and the result was checked'
      : v === 'ran_clean' ? 'every step worked, but nothing checked the result'
      : v === 'not_testable' ? "can't be tested in this lab"
      : (a.summary || 'this did not work either'));
    div.appendChild(head);
    for (const l of a.lines) {
      const d = document.createElement('div');
      d.className = 'labrun-step';
      d.textContent = l;
      div.appendChild(d);
    }
    if (a.answer) {
      const det = document.createElement('details');
      if (good) det.open = true;
      const s = document.createElement('summary');
      s.textContent = 'The answer the lab tried';
      const pre = document.createElement('pre');
      pre.textContent = a.answer;
      det.appendChild(s);
      det.appendChild(pre);
      div.appendChild(det);
    }
    return div;
  }

  function fillLabRun(box, lab) {
    box.innerHTML = '';
    const head = document.createElement('div');
    head.className = 'labrun-head';
    let tone = 'info', title;
    if (!lab || lab.status === 'queued') {
      title = 'Lab run: waiting to start' + (lab && lab.queue_position > 1 ? ` (#${lab.queue_position} in line)` : '') + '…';
    } else if (lab.status === 'running') {
      const done = (lab.steps || []).filter(s => s.cls).length;
      title = `Lab run: trying the steps in a fresh sandbox (${done}/${(lab.steps || []).length})…`;
    } else if (lab.status === 'error' && !lab.verdict) {
      title = 'Lab run could not finish: ' + (lab.error || 'unknown error');
    } else if (lab.status === 'unknown') {
      title = 'Lab run result is no longer available';
    } else {
      [tone, title] = LAB_VERDICTS[lab.verdict] || ['info', 'Lab run finished'];
    }
    box.className = 'labrun ' + tone;
    head.textContent = title;
    box.appendChild(head);
    if (lab && lab.summary && lab.status !== 'running') {
      const sum = document.createElement('div');
      sum.className = 'labrun-summary';
      sum.textContent = lab.summary;
      box.appendChild(sum);
    }
    for (const line of (lab && lab.lines) || []) {
      const row = document.createElement('div');
      row.className = 'labrun-step';
      row.textContent = line;
      box.appendChild(row);
    }
    for (const c of (lab && lab.checks) || []) {
      const row = document.createElement('div');
      row.className = 'labrun-step';
      const mark = c.decisive === false ? 'ℹ ' : (c.ok ? '✓ ' : '✗ ');
      row.textContent = mark + c.kind + ': ' + c.subject + (c.detail && (!c.ok || c.kind === 'login' || c.kind === 'http') ? ' — ' + c.detail.slice(0, 200) : '');
      box.appendChild(row);
    }
    if (lab && lab.transcript) {
      const pre = document.createElement('pre');
      pre.className = 'labrun-live';
      pre.textContent = lab.transcript;
      if (['queued', 'running'].includes(lab.status)) {
        box.appendChild(pre);
      } else {
        const det = document.createElement('details');
        const s = document.createElement('summary');
        s.textContent = 'What ran on the lab machine (read-only log)';
        det.appendChild(s);
        det.appendChild(pre);
        box.appendChild(det);
      }
      requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; });
    }
    if (lab && lab.repaired) {
      // L10b: the answer failed as written, but the lab repaired its steps.
      const r = lab.repaired;
      const rb = document.createElement('div');
      const good = ['goal_verified', 'lab_verified'].includes(r.verdict);
      rb.className = 'labrun-repaired ' + (good ? 'ok' : (r.verdict === 'ran_clean' ? 'info' : 'bad'));
      const head = document.createElement('div');
      head.className = 'labrun-head';
      head.textContent = good
        ? 'After the lab repaired it: every step worked, and the result was checked'
        : r.verdict === 'ran_clean'
          ? 'After the lab repaired it: every step worked, but nothing checked the result'
          : 'After the lab repaired it: ' + (r.summary || 'still not working');
      rb.appendChild(head);
      for (const c of r.changes) {
        const d = document.createElement('div');
        d.className = 'labrun-step';
        d.textContent = '↻ ' + c;
        rb.appendChild(d);
      }
      if (r.risky) {
        const w = document.createElement('div');
        w.textContent = 'A repair added a third-party source or piped a download into a shell: review it before trusting it.';
        rb.appendChild(w);
      }
      if (r.procedure) {
        const det = document.createElement('details');
        const s = document.createElement('summary');
        s.textContent = 'The procedure that worked in the lab';
        const pre = document.createElement('pre');
        pre.textContent = r.procedure;
        det.appendChild(s);
        det.appendChild(pre);
        rb.appendChild(det);
      }
      box.appendChild(rb);
    }
    if (lab && lab.kept) box.appendChild(labKeptBlock(lab));
    for (const a of (lab && lab.attempts) || []) box.appendChild(labAttemptBlock(a));
    for (const f of (lab && lab.failures) || []) {
      const det = document.createElement('details');
      const s = document.createElement('summary');
      s.textContent = `Output of step ${f.n}`;
      const pre = document.createElement('pre');
      pre.textContent = f.output;
      det.appendChild(s);
      det.appendChild(pre);
      box.appendChild(det);
    }
  }

  // What's kept of a run in localStorage: verdict and plain lines, plus the
  // output tail of failed steps only.
  function compactLab(data) {
    return {
      id: data.id, status: data.status, verdict: data.verdict || '', summary: data.summary || '',
      error: data.error || '', queue_position: data.queue_position || 0, lines: data.lines || [],
      checks: (data.checks || []).map(c => ({kind: c.kind, subject: c.subject, ok: c.ok, decisive: c.decisive, detail: (c.detail || '').slice(0, 300)})),
      steps: (data.steps || []).map(s => ({cls: s.cls})),
      failures: (data.steps || []).filter(s => s.cls && !['ok', 'skipped'].includes(s.cls) && s.output)
        .map(s => ({n: s.n, output: s.output.slice(-1500)})),
      // Start and end of a long log: the start holds boot and the first steps.
      transcript: (data.transcript || '').length > 24000
        ? data.transcript.slice(0, 6000) + '\\n# ... (middle of the log not kept in this browser) ...\\n' + data.transcript.slice(-18000)
        : (data.transcript || ''),
      kept: data.kept || null,
      retrying: !!data.retrying,
      attempts: (data.attempts || []).map(a => ({n: a.n, status: a.status, verdict: a.verdict || '', repaired: a.repaired || '',
                                                 summary: a.summary || '', answer: a.answer || '', lines: a.lines || []})),
      repaired: data.repaired && data.repaired.verdict ? {
        verdict: data.repaired.verdict, summary: data.repaired.summary || '',
        changes: data.repaired.changes || [], risky: !!data.repaired.risky,
        procedure: (data.repaired.procedure || '').slice(0, 8000),
      } : null,
    };
  }

  // How to get into a kept lab machine, from what the run knows: a shell
  // as student through the Terminal panel, and the login the answer set up.
  function labKeptBlock(lab) {
    const k = lab.kept;
    const box = document.createElement('div');
    box.className = 'labrun-kept';
    const expired = !k.expires_at || k.expires_at * 1000 < Date.now();
    const line = (text, cls) => {
      const d = document.createElement('div');
      if (cls) d.className = cls;
      d.textContent = text;
      box.appendChild(d);
      return d;
    };
    if (k.destroyed || expired) {
      line(k.destroyed ? 'The lab machine has been destroyed.' : 'The lab machine has expired and been destroyed.');
      return box;
    }
    const until = new Date(k.expires_at * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    line(`The lab machine is kept, exactly as the run left it, until ${until}.`, 'labrun-kept-head');
    if (!lab.token) {
      line('Logging in and Destroy are only available in the browser tab that asked the question (it holds the key to this machine).');
      return box;
    }
    line('To look around: open it in the Terminal panel below. You are logged in as student; sudo works without a password.');
    const cmds = [];
    const svcCmd = {login: 'sudo login student', su: 'su - student', sudo: 'sudo -k && sudo -v'};
    for (const s of (k.pam_services || [])) if (svcCmd[s]) cmds.push(svcCmd[s]);
    if (k.sshd_changed) cmds.push('ssh student@localhost');
    if (cmds.length) {
      line('To try the login the answer set up, run: ' + cmds.join('   or   '));
      if (k.password) line(`Password for student (set by the lab for testing): ${k.password}`);
      if (k.totp_secret) line(`Verification code: run  oathtool --totp -b ${k.totp_secret}  (the secret the answer's google-authenticator created)`);
    }
    const buttons = document.createElement('div');
    buttons.className = 'labrun-kept-buttons';
    const open = document.createElement('button');
    open.textContent = 'Open it in the Terminal panel';
    open.onclick = () => startTerminal({id: lab.id, token: lab.token});
    const destroy = document.createElement('button');
    destroy.textContent = 'Destroy this lab machine';
    destroy.onclick = async () => {
      destroy.disabled = true;
      try {
        await fetch('/sandbox/lab-run/destroy', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify({id: lab.id, token: lab.token})});
      } catch (e) { /* the refresh below shows the real state either way */ }
      const h = getHistory();
      const entry = h.find(m => m.labRun && m.labRun.id === lab.id);
      if (entry && entry.labRun.kept) { entry.labRun.kept.destroyed = true; saveHistory(h); }
      const holder = transcriptEl.querySelector(`[data-labrun="${lab.id}"]`);
      if (holder && entry) fillLabRun(holder, entry.labRun);
    };
    buttons.appendChild(open);
    buttons.appendChild(destroy);
    box.appendChild(buttons);
    return box;
  }

  const labPolls = new Set();
  function pollLabRun(id) {
    if (labPolls.has(id)) return;
    labPolls.add(id);
    const tick = async () => {
      let data;
      try {
        const known = getHistory().find(m => m.labRun && m.labRun.id === id);
        const token = known && known.labRun.token ? known.labRun.token : '';
        const resp = await fetch('/sandbox/lab-run?id=' + encodeURIComponent(id) + '&token=' + encodeURIComponent(token),
                                 {cache: 'no-store'});
        data = await resp.json();
      } catch (e) { setTimeout(tick, 5000); return; }
      const lab = compactLab(data);
      const h = getHistory();
      const entry = h.find(m => m.labRun && m.labRun.id === id);
      if (entry) { lab.token = entry.labRun.token; entry.labRun = lab; saveHistory(h); }
      const box = transcriptEl.querySelector(`[data-labrun="${id}"]`);
      if (box) fillLabRun(box, lab);
      if (['done', 'error', 'unknown'].includes(data.status) && !data.retrying) { labPolls.delete(id); return; }
      setTimeout(tick, data.status === 'running' ? 1500 : 3000);
    };
    tick();
  }

  function renderLabRun(parentEl, lab) {
    if (!lab || !lab.id) return;
    const box = document.createElement('div');
    box.dataset.labrun = lab.id;
    fillLabRun(box, lab);
    parentEl.appendChild(box);
    if (!['done', 'error', 'unknown'].includes(lab.status) || lab.retrying) pollLabRun(lab.id);
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
        renderNotices(contentEl, h[i].notices);
        renderLabRun(contentEl, h[i].labRun);
        renderSources(contentEl, h[i].sources);
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
        } else if (data.queue_position) {
          // ASK_QUEUE_ENABLED only -- queue_position is always null
          // with the feature off, so this branch never fires then and
          // the message below is unchanged from before this existed.
          statusEl.textContent = `You're #${data.queue_position} in the queue`
            + (data.queue_depth ? ` (${data.queue_depth} waiting)` : '') + '...';
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
    let answerSources = [];
    let answerNotices = [];
    let answerLabRun = null;
    try {
      const resp = await fetch(cfg.endpoint, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(Object.assign(cfg.buildBody(), {question, history: contextHistory})),
      });
      if (!resp.ok || !resp.body) {
        // 429 (rate limit or "already have a question in progress") is
        // plain text with the real, useful reason. 503 (someone ELSE
        // is currently asking -- see _send_busy()'s own comment) is
        // JSON -- {message, queue_position, queue_depth}, the latter
        // two null unless ASK_QUEUE_ENABLED -- so a real live position
        // can be appended when there is one. 503 specifically starts
        // polling for availability so the student is told the moment
        // it's actually worth retrying, rather than left to guess-
        // and-spam Ask themselves.
        bubbleContent.classList.remove('thinking');
        if (resp.status === 429) {
          bubbleContent.textContent = await resp.text();
        } else if (resp.status === 503) {
          const data = await resp.json();
          bubbleContent.textContent = data.message
            + (data.queue_position ? ` You're #${data.queue_position} in the queue.` : '');
        } else {
          bubbleContent.textContent = 'Request failed (' + resp.status + ')';
        }
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
              if (obj.sources) { answerSources = obj.sources; continue; }
              if (obj.notices) { answerNotices = obj.notices; continue; }
              if (obj.lab_run) { answerLabRun = {id: obj.lab_run.id, token: obj.lab_run.token, status: 'queued'}; continue; }
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
    h.push({role: 'assistant', content: assistantText || bubbleContent.textContent,
            sources: answerSources, notices: answerNotices, labRun: answerLabRun});
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
  buildBody: () => ({code: cm.getValue(), language: currentLang()}),
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
  // Untagged stays runnable: small models often omit the tag on real commands.
  runnableTags: ['', 'bash', 'sh', 'shell', 'console', 'zsh'],
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

function clearLinuxHelp() {
  // Mirrors clearAll() above, scoped to just this panel -- direct
  // request for a real "start fresh" button on Linux Help too, not
  // just the coding panel (the two conversations stay deliberately
  // independent, same reasoning as clearAll()'s own comment).
  if (!confirm('Clear your Linux Help conversation? This only affects this browser.')) return;
  linuxAsk.clearHistory();
}

async function runCode() {
  const btn = document.getElementById('runBtn');
  const status = document.getElementById('runStatus');
  const out = document.getElementById('runResult');
  if (!cm.getValue().trim()) return;
  btn.disabled = true;
  const lang = currentLang();
  status.textContent = lang === 'python' ? 'Running...' : 'Starting a fresh sandbox...';
  out.innerHTML = '';
  // Non-Python Runs boot their own microVM and may queue behind someone
  // else's -- poll our own ticket's position while the request blocks.
  const ticket = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
  let poll = null;
  if (lang !== 'python') {
    poll = setInterval(async () => {
      try {
        const s = await (await fetch('/sandbox/run-status?ticket=' + encodeURIComponent(ticket))).json();
        if (s.state === 'queued') status.textContent = `Waiting for a free sandbox (position ${s.position})...`;
        else if (s.state === 'running') status.textContent = 'Booting a sandbox and running...';
      } catch (e) {}
    }, 1000);
  }
  try {
    const resp = await fetch('/sandbox/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: cm.getValue(), language: lang, ticket}),
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
    if (data.phase === 'compile') {
      out.innerHTML = `<div class="result fail">
        <h4>compiler output</h4><pre>${esc(data.stderr || '(none)')}</pre>
        <p class="exitline">compilation failed (exit code ${data.exit_code}${data.timed_out ? ', timed out' : ''}) -- the program did not run</p>
      </div>`;
      return;
    }
    out.innerHTML = `<div class="result ${passed ? 'pass' : 'fail'}">
      ${data.stdout.trim() ? `<h4>stdout</h4><pre>${esc(data.stdout)}</pre>` : ''}
      ${data.stderr.trim() ? `<h4>stderr</h4><pre>${esc(data.stderr)}</pre>` : ''}
      <p class="exitline">exit code: ${data.exit_code}${data.timed_out ? ' (timed out)' : ''}</p>
    </div>`;
  } catch (e) {
    status.textContent = 'Request failed: ' + e.message;
  } finally {
    if (poll) clearInterval(poll);
    btn.disabled = false;
  }
}

// ── Terminal panel ──────────────────────────────────────────────────
// Same xterm.js + WS<->PTY wiring shape as the Dashboard's own admin
// Terminal feature (ui/src/js/11-terminal.js) -- one fixed panel here
// rather than that page's multi-window instance picker, since there's
// only ever one sandbox terminal per student session.
let termState = null;  // { ws, term, fitAddon }

// With `lab` ({id, token}), opens the kept lab machine an advice run left
// behind instead of booting a fresh sandbox.
function startTerminal(lab) {
  if (termState && lab) stopTerminal();
  if (termState) return;
  const startBtn = document.getElementById('termStartBtn');
  const stopBtn = document.getElementById('termStopBtn');
  const status = document.getElementById('termStatus');
  const host = document.getElementById('termHost');

  startBtn.disabled = true;
  host.classList.add('open');
  status.textContent = lab ? 'Opening the lab machine...' : 'Booting a fresh sandboxed shell...';
  host.scrollIntoView({behavior: 'smooth', block: 'center'});

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
  const labQuery = lab ? `?lab=${encodeURIComponent(lab.id)}&token=${encodeURIComponent(lab.token)}` : '';
  const ws = new WebSocket(`${wsProto}//${location.host}/terminal${labQuery}`);
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
// The prompt asks for config in ```text fences, but the model still
// tags PAM and sshd_config lines as bash often enough (F-174), so the
// first real line is checked too: a PAM rule, or a capitalised
// "Directive value" (shell commands are lowercase), is not a command.
function looksLikeConfigLine(code) {
  const first = code.split('\\n').map(l => l.trim()).find(l => l && !l.startsWith('#')) || '';
  return /^(auth|account|password|session|@include)\\s+\\S/.test(first)
      || /^[A-Z][A-Za-z0-9]+\\s+\\S/.test(first)
      // an fstab line (device, mount point, type): never a command
      || /^(?:\\/dev\\/\\S+|UUID=\\S+|LABEL=\\S+|PARTUUID=\\S+|[\\w.-]+:\\/\\S*|tmpfs|proc)\\s+(?:\\/\\S*|none|swap)\\s+[\\w.,-]+(?:\\s|$)/.test(first);
}

// The page is usually served over plain HTTP through the LB, where
// navigator.clipboard doesn't exist (secure contexts only), hence the
// execCommand fallback.
function copyCodeBlock(text, btn) {
  const shown = (label) => {
    btn.textContent = label;
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  };
  const legacy = () => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    ta.remove();
    shown(ok ? 'Copied' : 'Select and copy manually');
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => shown('Copied'), legacy);
  } else {
    legacy();
  }
}

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
        elif path == KIWIX_URL_ROOT or path.startswith(KIWIX_URL_ROOT + "/"):
            self._proxy_kiwix()
        elif path == "/sandbox/ask-status":
            self._handle_ask_status()
        elif path == "/sandbox/run-status":
            self._handle_run_status()
        elif path == "/sandbox/lab-run":
            self._handle_lab_run_status()
        elif path == "/sandbox/lab-run/attach":
            self._handle_lab_run_attach()
        elif path.startswith("/vendor/"):
            self._serve_vendor_file(path[len("/vendor/"):])
        else:
            self._not_found()

    def do_HEAD(self):
        path = self._clean_path()
        if path == "/health":
            self._proxy_passthrough()
        elif path == KIWIX_URL_ROOT or path.startswith(KIWIX_URL_ROOT + "/"):
            self._proxy_kiwix()
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
            with _StudentRequest():
                self._handle_sandbox_ask()
        elif path == "/sandbox/linux-ask":
            with _StudentRequest():
                self._handle_linux_ask()
        elif path == "/sandbox/interrupt":
            self._handle_sandbox_interrupt()
        elif path == "/sandbox/reverify":
            self._handle_sandbox_reverify()
        elif path == "/sandbox/lab-run/destroy":
            self._handle_lab_run_destroy()
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

    def _send_busy(self, message: str, queue_position: int | None = None,
                    queue_depth: int | None = None):
        # A distinct status (503, not 429's rate-limit/per-IP-already-
        # active meaning) so the browser's own JS can tell "someone ELSE
        # is asking" apart from those other two 429 cases, and knows to
        # start polling /sandbox/ask-status rather than just showing a
        # static message. Always a small JSON body (not plain text) --
        # queue_position/queue_depth ride the same envelope ask-status
        # already uses, rather than inventing a second, plain-text-only
        # encoding for the same concept. With ASK_QUEUE_ENABLED off,
        # both are always None here (see _handle_ask's own call site),
        # so this degrades to {"message": ..., "queue_position": null,
        # "queue_depth": null} -- same information as before, just
        # JSON-shaped; no threading/business-logic change either way.
        out = json.dumps({
            "message": message, "queue_position": queue_position, "queue_depth": queue_depth,
        }).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_ask_status(self):
        """GET /sandbox/ask-status -- a trivial, instant read of the
        global busy flag (see _llm_busy's own comment) plus, when
        ASK_QUEUE_ENABLED, this caller's own live queue position -- no
        LLM call involved either way. Polled by the browser every 4s
        once it's been told "busy" once, to know the moment it's worth
        telling the student to retry rather than leaving them to
        guess-and-spam Ask. This poll is also what keeps a genuinely-
        still-waiting student's own queue ticket alive (see
        _prune_stale_queue_entries_locked's own comment) -- every call
        here refreshes `last_seen` for the calling IP if it's queued."""
        ip = self._client_ip()
        with _llm_busy_lock:
            if ASK_QUEUE_ENABLED:
                _prune_stale_queue_entries_locked()
                if ip in _ask_queue:
                    _ask_queue[ip]["last_seen"] = time.time()
            busy = _llm_busy
            pos = _queue_position_locked(ip) if ASK_QUEUE_ENABLED else None
            depth = len(_ask_queue) if ASK_QUEUE_ENABLED else None
        out = json.dumps({"busy": busy, "queue_position": pos, "queue_depth": depth}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_sandbox_run(self):
        """Plain Run -- executes the student's own current buffer as-is,
        synchronously: Python through the exact same run_sandboxed()
        Phases 1-2 already proved live, any other language in a per-run
        microVM (Stage 12). Bounded to a few seconds by
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
        language = req_json.get("language") or "python"
        # The browser's own ticket, so it can poll /sandbox/run-status for
        # its queue position while this request blocks (Stage 12).
        ticket = str(req_json.get("ticket") or "")[:64] or None
        if language not in LANGUAGES:
            out = json.dumps({"error": f"unsupported language: {language}"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if not code.strip():
            out = json.dumps({"error": "code is required"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        result = execute(language, code, ticket=ticket)
        out = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_sandbox_reverify(self):
        """POST /sandbox/reverify -- Stage 13. The CloudCore API asks this
        coordinator to re-run code a local-capture client submitted, so the
        corpus records OUR execution result, never the client's. Plain,
        non-interactive execute(): stdin is closed, exactly like a Run.

        This port is reachable by students through the LB, so it requires
        the same shared token this coordinator already uses to POST
        captures to the API (EXAMPLES_API_TOKEN). Without one configured
        the route stays disabled rather than open."""
        def reply(status: int, obj: dict):
            out = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        presented = self.headers.get("Authorization", "")
        if not EXAMPLES_API_TOKEN or not hmac.compare_digest(
                presented.encode(), f"Bearer {EXAMPLES_API_TOKEN}".encode()):
            reply(401, {"error": "unauthorized"})
            return
        req_json = self._read_json_body()
        language = req_json.get("language") or ""
        code = req_json.get("code") or ""
        if language not in LANGUAGES:
            reply(400, {"error": f"unsupported language: {language}"})
            return
        if not code.strip():
            reply(400, {"error": "code is required"})
            return
        reply(200, execute(language, code))

    def _handle_lab_run_status(self):
        """GET /sandbox/lab-run?id=<run> -- progress and results of one
        Linux Help advice run (L4). Instant, no side effects; polled by the
        page under the answer until status is done or error."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        run_id = re.sub(r"[^0-9a-f]", "", (query.get("id") or [""])[0])[:12]
        token = (query.get("token") or [""])[0][:64]
        out = json.dumps(_advice_status(run_id, token) if run_id else {"status": "unknown"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _send_small_json(self, code: int, obj: dict) -> None:
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_lab_run_destroy(self):
        """POST /sandbox/lab-run/destroy {id, token} -- the page's "Destroy
        this lab machine" button: frees the kept VM now instead of at its
        deadline. Only with the run's token."""
        body = self._read_json_body()
        run_id = re.sub(r"[^0-9a-f]", "", str(body.get("id", "")))[:12]
        with _advice_cv:
            data = _advice_runs.get(run_id) or {}
        if not _token_ok(data, str(body.get("token", ""))[:64]):
            self._send_small_json(403, {"error": "not your lab machine"})
            return
        gone = _release_kept(run_id, "destroyed from the page")
        self._send_small_json(200, {"destroyed": gone})

    def _handle_lab_run_attach(self):
        """GET /sandbox/lab-run/attach?id=&token= -- for sandbox_terminal.py
        on this same host only (never through the LB): which VM a Terminal
        session for this kept lab machine should open a shell on."""
        if self.client_address[0] != "127.0.0.1" or self.headers.get("X-Forwarded-For"):
            self._send_small_json(403, {"error": "local only"})
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        run_id = re.sub(r"[^0-9a-f]", "", (query.get("id") or [""])[0])[:12]
        with _advice_cv:
            data = _advice_runs.get(run_id) or {}
        if not _token_ok(data, (query.get("token") or [""])[0][:64]):
            self._send_small_json(403, {"error": "not your lab machine"})
            return
        with _kept_lock:
            entry = _kept_labs.get(run_id)
        if entry is None:
            self._send_small_json(404, {"error": "that lab machine has been destroyed"})
            return
        vm = entry["vm"]
        self._send_small_json(200, {"session_id": vm.session_id, "ip": vm.ip, "expires_at": entry["expires"]})

    def _handle_run_status(self):
        """GET /sandbox/run-status?ticket=<id> -- where the caller's own
        in-flight non-Python Run is in the per-run microVM queue. Instant,
        no side effects; polled by the Run button while it waits."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        ticket = (query.get("ticket") or [""])[0][:64]
        out = json.dumps(_run_queue.position(ticket) if ticket else {"state": "unknown"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
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
        question outright by default -- thrown away, never even sent
        upstream. With ASK_QUEUE_ENABLED on (see its own comment),
        that reject instead registers a FIFO ticket and reports this
        IP's live position, still never auto-resubmitted -- the
        student sees a real "#N in the queue" and retries themselves
        once told it's free, same manual-retry shape as before, just
        with an honest position instead of a bare "busy" message.
        Checked after the per-IP gate so a student re-clicking their
        OWN in-flight question still gets the friendlier "you already
        have one" message instead of being told someone else is busy."""
        ip = self._client_ip()
        request_id = str(uuid.uuid4())[:8]
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
            if ASK_QUEUE_ENABLED:
                _prune_stale_queue_entries_locked()
            # ASK_QUEUE_ENABLED's own real fix, not just "is the model
            # busy": the model can go idle while this IP still isn't at
            # the front of _ask_queue (the front-of-queue student just
            # hasn't polled/retried yet) -- letting a brand-new IP
            # through in that window would let it silently jump a real,
            # already-displayed queue position. front_ip is None (and
            # this whole check a no-op) whenever the queue is empty,
            # which is every request when the feature is off.
            front_ip = next(iter(_ask_queue), None)
            must_wait = _llm_busy or (ASK_QUEUE_ENABLED and front_ip is not None and front_ip != ip)
            if must_wait:
                pos = depth = None
                is_new = False
                if ASK_QUEUE_ENABLED:
                    is_new = ip not in _ask_queue
                    _ask_queue.setdefault(ip, {"enqueued_at": time.time()})["last_seen"] = time.time()
                    pos, depth = _queue_position_locked(ip), len(_ask_queue)
                self._send_busy(
                    "The assistant is currently answering another student's question -- "
                    "your question was not sent. This page will let you know the moment "
                    "it's free so you can ask again.",
                    queue_position=pos, queue_depth=depth)
                if ASK_QUEUE_ENABLED and is_new:
                    _log_ask_queue_event(request_id, endpoint_label, "queued",
                                          queue_position=pos, queue_depth=depth)
                return
            ticket = _ask_queue.pop(ip, None) if ASK_QUEUE_ENABLED else None
            _llm_busy = True
        if ASK_QUEUE_ENABLED:
            wait_seconds = (time.time() - ticket["enqueued_at"]) if ticket else 0.0
            _log_ask_queue_event(request_id, endpoint_label, "started", wait_seconds=wait_seconds)

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
            if ASK_QUEUE_ENABLED:
                _log_ask_queue_event(request_id, endpoint_label, "finished")

    def _do_handle_ask(self, interrupt_event, system_message: str, capture_source: str,
                        verify: bool, include_code: bool, endpoint_label: str):
        ask_started_at = time.time()
        req_json = self._read_json_body()
        code = (req_json.get("code") or "").strip() if include_code else ""
        question = (req_json.get("question") or "").strip()
        history = req_json.get("history") or []
        max_tokens = req_json.get("max_tokens")
        # Stage 12: the student's selected editor language. Anything
        # unrecognised falls back to Python, the original behaviour.
        language = req_json.get("language") if include_code else None
        if language not in LANGUAGES:
            language = "python"
        if include_code:
            spec = LANGUAGES[language]
            system_message = (
                f"{system_message}\n\nThe student's selected language is {spec['label']}. "
                f"Write any code in {spec['label']} unless they explicitly ask for another "
                f"language, as one complete program in a fenced ```{spec['fence']} block"
                + (" with a main function" if language in ("c", "cpp") else "")
                + (" in package main" if language == "go" else "")
                + (" for Node.js, with no browser-only APIs" if language == "javascript" else "")
                + ".")

        if not question:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"question is required")
            return

        user_turn = (f"Here is my current code:\n```{LANGUAGES[language]['fence']}\n{code}\n```\n\n{question}"
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
        # Direct report: a long pasted error, not the model itself, was
        # the actual cause of a wrong answer -- extraction timed out on
        # it, silently producing no grounding at all. Told to the
        # student directly (see the note appended near _log_grounding's
        # own call below) rather than left as a silent quality drop,
        # since they have no way to otherwise know grounding quietly
        # didn't help this time.
        question_truncated_for_search = len(question) > _SEARCH_TERMS_MAX_QUESTION_CHARS
        search_terms = _extract_search_terms(question)
        # Check-corpus-first, direct follow-up to F-136: a human-approved
        # past answer (see _local_corpus_search's own comment) is tried
        # before this platform's own source code, which is in turn
        # tried before the general-purpose kiwix corpus -- each tier is
        # the fallback for whatever the one before it doesn't cover,
        # not a second opinion run alongside it.
        grounding, references = _local_corpus_search(search_terms)
        grounding_source = "local_corpus" if references else "none"
        if not references and _is_platform_question(question):
            grounding, references = _codebase_search(search_terms)
            if references:
                grounding_source = "codebase"
        if not references:
            grounding, references = _kiwix_search(search_terms, "coding" if include_code else "linux")
            if references:
                grounding_source = "kiwix"
        if endpoint_label == "/sandbox/linux-ask":
            grounding = _lab_facts(search_terms) + grounding
        messages = ([{"role": "system", "content": system_message}]
                    + [{"role": m["role"], "content": str(m.get("content") or "")}
                       for m in history if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
                    + [{"role": "user", "content": grounding + user_turn}])

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
                                       started_at=ask_started_at, question=question, language=language,
                                       student_code=code,
                                       search_terms=search_terms, references=references,
                                       grounding_source=grounding_source,
                                       question_truncated_for_search=question_truncated_for_search)
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
                                  grounding_source: str = "none",
                                  question_truncated_for_search: bool = False,
                                  language: str | None = None, student_code: str = ""):
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

        # Defaulted here, not just inside the block below -- outcome/
        # duration_s now also feed _log_grounding()'s own call further
        # down (see its own comment on why), which must never NameError
        # on whatever rare path leaves started_at unset.
        outcome = None
        duration_s = None
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
            duration_s = time.time() - started_at
            _log_ask_outcome(endpoint_label, outcome, duration_s,
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
            extracted = extract_code(full_text, language or "python") if ENABLE_VERIFICATION else None
            if extracted:
                code_lang, code = extracted
                extra, capture = verify_and_maybe_fix(request_messages, code, heartbeat=self._sse_heartbeat,
                                                        max_tokens=request_max_tokens, interrupt=interrupt,
                                                        language=code_lang)
                self._write_sse_delta(extra, last_chunk_meta)
                capture_example(request_messages, capture, source=capture_source)
            elif ENABLE_VERIFICATION and student_code and extract_stdin_value(full_text) is not None:
                # F-164: asked to run the editor's code with some input, the
                # model sometimes replies with ONLY a ```stdin block and no
                # code, so nothing ran and the student saw a bare block. What
                # they asked for is their own code run with that input, so do
                # exactly that. Not captured to the corpus: it's the
                # student's code, not a model claim (same as a plain Run).
                extra, capture = verify_and_maybe_fix(
                    request_messages, student_code, heartbeat=self._sse_heartbeat,
                    max_tokens=request_max_tokens, interrupt=interrupt,
                    language=language or "python",
                    initial_inputs=[extract_stdin_value(full_text)])
                self._write_sse_delta(
                    "\n\n_Ran the code in your editor with the input above._" + extra, last_chunk_meta)

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
        # Direct report: a long pasted error caused search-term
        # extraction to time out, silently producing no grounding --
        # the student had no way to know that had happened, or why the
        # answer might be less reliable as a result. Told plainly,
        # regardless of whether a reference was still found afterward
        # (see question_truncated_for_search's own comment in
        # _do_handle_ask) -- a truncated search query can retrieve
        # something for the wrong facet of a long question just as
        # easily as it can retrieve nothing.
        if question_truncated_for_search:
            self._write_sse_delta(
                "\n\n---\n_Your question was quite long, so it was shortened before searching "
                "for reference material -- this can make the answer above less reliable. For "
                "best results, paste just the most relevant part of a long error or log rather "
                "than the whole output._\n", last_chunk_meta)

        _log_grounding(endpoint_label, question, full_text, search_terms, references or [],
                        code_verified, grounding_source, outcome=outcome, duration_s=duration_s,
                        tokens=(last_timings or {}).get("predicted_n"),
                        tokens_per_second=(last_timings or {}).get("predicted_per_second"),
                        continuation_rounds=continuation_rounds)
        if SENTINEL_HOST:
            snapshot = _llm_stats_snapshot()
            snapshot["deployment_name"] = DEPLOYMENT_NAME
            threading.Thread(target=_push_llm_stats_to_sentinel, args=(snapshot,), daemon=True).start()

        # K4: the kiwix pages behind this answer, as a structured event the
        # page renders as real links (never as model text).
        sources = [{"title": r["title"], "source": r["source"], "link": r["link"]}
                   for r in (references or []) if r.get("link")]
        try:
            if sources:
                self.wfile.write(b"data: " + json.dumps({"sources": sources}).encode() + b"\n\n")
            if endpoint_label == "/sandbox/linux-ask":
                notices = _answer_notices(full_text)
                self.wfile.write(b"data: " + json.dumps({"notices": notices}).encode() + b"\n\n")
                started = (_start_advice_run(full_text, question, search_terms)
                           if outcome == "success" else None)
                if started:
                    run_id, token = started
                    self.wfile.write(b"data: " + json.dumps({"lab_run": {"id": run_id, "token": token}}).encode()
                                     + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _proxy_kiwix(self):
        """GET /kiwix/... -- read-only passthrough to kiwix-serve (which
        runs with --urlRootLocation=/kiwix, so paths map 1:1): the pages
        answers cite, plus kiwix's own library browser and search. GET and
        HEAD only; nothing here can change kiwix's state."""
        if not KIWIX_HOST:
            self._not_found()
            return
        conn = http.client.HTTPConnection(KIWIX_HOST, KIWIX_PORT, timeout=15)
        try:
            conn.request(self.command, self.path,
                         headers={"Accept-Encoding": self.headers.get("Accept-Encoding", "")})
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("content-type", "content-length", "content-encoding", "cache-control",
                                 "etag", "last-modified", "location", "content-range", "accept-ranges"):
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, http.client.HTTPException) as e:
            print(f"verify-proxy: kiwix passthrough failed: {e!r}", flush=True)
            try:
                self.send_error(502, "reference library unavailable")
            except OSError:
                pass
        finally:
            conn.close()

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
                f'<div class="meta">{esc(LANGUAGES.get(it.get("language") or "python", {}).get("label", it.get("language") or "python"))} &middot; '
                f'{esc(it.get("model_filename",""))} &middot; {esc(it.get("created_at",""))} &middot; {passed_badge}</div>',
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


class _QuietDisconnectServer(http.server.ThreadingHTTPServer):
    """F-163: HAProxy's health check (and any browser that navigates away)
    closes the connection before reading the whole response, and the
    handler's next write then raises ConnectionResetError/BrokenPipeError.
    socketserver's default handle_error prints a full traceback for that,
    which filled the journal (and so Loki, which Sentinel reads) with
    noise. A client that hung up needs no answer; every other error still
    gets the full default traceback."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    # Fire-and-forget -- a slow/unreachable CloudCore API must never
    # delay this service actually binding and serving real traffic.
    threading.Thread(target=register_llm_deployment, daemon=True).start()
    threading.Thread(target=_load_terminal_packages, daemon=True).start()
    # Stage 12: per-run microVMs live outside this service's cgroup
    # (jailer moves them), so a restart mid-Run leaves them running.
    # Nothing of ours can be live yet, so everything tagged "run" is an
    # orphan. Skipped where microvm.py's dependencies aren't installed --
    # the Python-only path never needs them.
    try:
        from microvm import sweep_orphans
        swept = sweep_orphans("run") + sweep_orphans("advc") + sweep_orphans("advp")
        if swept:
            print(f"verify-proxy: removed {swept} per-run/advice microVM(s) orphaned by a previous run",
                  flush=True)
    except ImportError:
        pass
    server = _QuietDisconnectServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
