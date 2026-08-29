"""File editor API — named roots mapped server-side, never raw paths from client."""
from __future__ import annotations

import os
from pathlib import Path
from flask import Blueprint, request, jsonify

editor_bp = Blueprint("editor", __name__)

_BASE = Path(__file__).parent.parent

# Named roots
ROOTS: dict[str, Path] = {
    "ansible": _BASE / "ansible" / "examples",
    "opentofu": _BASE / "examples",
}


def _problem(status, title, detail):
    return jsonify({"status": status, "title": title, "detail": detail}), status


def _resolve(root_name: str, rel: str | None = None) -> Path | None:
    """Resolve a named root + optional relative path, rejecting path traversal."""
    root = ROOTS.get(root_name)
    if root is None:
        return None
    if not rel:
        return root
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def _tree(path: Path, root: Path) -> list[dict]:
    entries = []
    try:
        items = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return entries
    for item in items:
        rel = str(item.relative_to(root))
        if item.is_dir():
            entries.append({"name": item.name, "path": rel, "type": "dir",
                            "children": _tree(item, root)})
        else:
            entries.append({"name": item.name, "path": rel, "type": "file"})
    return entries


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@editor_bp.get("/v1/editor/roots")
def list_roots():
    return jsonify({"roots": list(ROOTS.keys())})


@editor_bp.get("/v1/editor/tree")
def get_tree():
    root_name = request.args.get("root", "")
    root = ROOTS.get(root_name)
    if root is None:
        return _problem(404, "Not Found", f"Unknown root '{root_name}'")
    return jsonify({"root": root_name, "tree": _tree(root, root)})


@editor_bp.get("/v1/editor/file")
def read_file():
    root_name = request.args.get("root", "")
    rel       = request.args.get("path", "")
    target    = _resolve(root_name, rel)
    if target is None:
        return _problem(404, "Not Found", "Unknown root or invalid path")
    if not target.exists() or not target.is_file():
        return _problem(404, "Not Found", f"File not found: {rel}")
    return jsonify({"path": rel, "content": target.read_text(errors="replace")})


@editor_bp.put("/v1/editor/file")
def write_file():
    root_name = request.args.get("root", "")
    rel       = request.args.get("path", "")
    target    = _resolve(root_name, rel)
    if target is None:
        return _problem(400, "Bad Request", "Unknown root or invalid path")
    if not target.exists():
        return _problem(404, "Not Found", f"File not found: {rel}")
    body = request.get_json(force=True) or {}
    target.write_text(body.get("content", ""))
    return jsonify({"path": rel, "saved": True})


@editor_bp.post("/v1/editor/file")
def create_file():
    root_name = request.args.get("root", "")
    body      = request.get_json(force=True) or {}
    filename  = body.get("filename", "").strip()
    if not filename:
        return _problem(400, "Bad Request", "filename is required")
    # Reject any path separators — new files always go at root
    if os.sep in filename or "/" in filename:
        return _problem(400, "Bad Request", "filename must not contain path separators")
    target = _resolve(root_name, filename)
    if target is None:
        return _problem(400, "Bad Request", "Unknown root")
    if target.exists():
        return _problem(409, "Conflict", f"File '{filename}' already exists")
    target.write_text(body.get("content", ""))
    return jsonify({"path": filename, "created": True}), 201


@editor_bp.delete("/v1/editor/file")
def delete_file():
    root_name = request.args.get("root", "")
    rel       = request.args.get("path", "")
    target    = _resolve(root_name, rel)
    if target is None:
        return _problem(400, "Bad Request", "Unknown root or invalid path")
    if not target.exists() or not target.is_file():
        return _problem(404, "Not Found", f"File not found: {rel}")
    target.unlink()
    return "", 204
