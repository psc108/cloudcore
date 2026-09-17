"""Reads real metadata directly out of a GGUF file's own binary header —
no model-specific lookup table to keep in hand-sync. Built because every
model swap this session (Mistral-7B -> Qwen2.5-Coder-7B, and now -14B)
required manually re-deriving and hand-editing a "transformer layer count"
comment in three separate files (examples/llm-chat/variables.tf,
ansible/examples/14-llm-chat.yml, and their distributed-llm equivalents)
to keep rpc_offload_layers tuned — real, previously-undiscovered risk of
silent drift if anyone forgot. The layer count (GGUF calls it
"<architecture>.block_count") is genuinely present in every GGUF file's
own header; reading it directly removes the whole class of bug rather
than documenting it more carefully.

GGUF binary format (llama.cpp's own spec, version 3, confirmed live by
parsing the real Mistral-7B and Qwen2.5-Coder-7B files already cached in
package-repo/jammy/artifacts/ and checking the result against each
model's own published config.json num_hidden_layers — 32 and 28
respectively, both matched exactly):

    magic       4 bytes  b"GGUF"
    version     uint32
    tensor_count   uint64
    kv_count       uint64
    then kv_count key-value pairs:
        key    : gguf-string (uint64 length + utf8 bytes)
        vtype  : uint32 (see _SCALAR_SIZES / ARRAY=9 / STRING=8)
        value  : type-dependent

Only reads the file's header (a few KB at most for any real model,
regardless of the multi-GB tensor data that follows) — never loads
tensor data.
"""
from __future__ import annotations

import struct
from pathlib import Path

_STRING = 8
_ARRAY = 9
_SCALAR_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def _read_str(f) -> str:
    (n,) = struct.unpack("<Q", f.read(8))
    return f.read(n).decode("utf-8", errors="replace")


def _skip_value(f, vtype: int) -> None:
    if vtype == _STRING:
        _read_str(f)
    elif vtype == _ARRAY:
        (elem_type,) = struct.unpack("<I", f.read(4))
        (count,) = struct.unpack("<Q", f.read(8))
        for _ in range(count):
            _skip_value(f, elem_type)
    elif vtype in _SCALAR_SIZES:
        f.read(_SCALAR_SIZES[vtype])
    else:
        raise ValueError(f"unknown GGUF value type {vtype}")


def _read_scalar(f, vtype: int):
    if vtype == _STRING:
        return _read_str(f)
    if vtype in (4, 10):  # UINT32, UINT64
        fmt, size = ("<I", 4) if vtype == 4 else ("<Q", 8)
        return struct.unpack(fmt, f.read(size))[0]
    if vtype in (5, 11):  # INT32, INT64
        fmt, size = ("<i", 4) if vtype == 5 else ("<q", 8)
        return struct.unpack(fmt, f.read(size))[0]
    if vtype in (2, 3):  # UINT16, INT16
        fmt = "<H" if vtype == 2 else "<h"
        return struct.unpack(fmt, f.read(2))[0]
    _skip_value(f, vtype)
    return None


def block_count(path: Path | str) -> int | None:
    """The model's own real transformer layer count (GGUF's
    "<architecture>.block_count" key), or None if the file isn't a
    readable GGUF or that key is genuinely absent. Reads only the
    header — safe to call on a multi-GB file without loading it."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            (_version,) = struct.unpack("<I", f.read(4))
            (_tensor_count,) = struct.unpack("<Q", f.read(8))
            (kv_count,) = struct.unpack("<Q", f.read(8))
            result = None
            for _ in range(kv_count):
                key = _read_str(f)
                (vtype,) = struct.unpack("<I", f.read(4))
                if key.endswith(".block_count"):
                    result = _read_scalar(f, vtype)
                    return result
                _skip_value(f, vtype)
            return result
    except (OSError, struct.error, UnicodeDecodeError):
        return None
