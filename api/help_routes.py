"""Help article API routes — CRUD + FTS5 search."""
from __future__ import annotations

from flask import Blueprint, request, jsonify

import help_store
from models import HelpArticle, slugify

help_bp = Blueprint("help", __name__)


def _problem(status, title, detail):
    return jsonify({"status": status, "title": title, "detail": detail}), status


@help_bp.get("/v1/help/articles")
def list_help_articles():
    q = request.args.get("q", "").strip()
    items = help_store.search(q) if q else help_store.list_all()
    return jsonify({"items": [a.to_dict() for a in items]})


@help_bp.get("/v1/help/articles/<article_id>")
def get_help_article(article_id):
    a = help_store.get(article_id)
    if not a:
        return _problem(404, "Not Found", f"Help article '{article_id}' not found")
    return jsonify(a.to_dict())


@help_bp.post("/v1/help/articles")
def create_help_article():
    body = request.get_json(force=True) or {}
    title = body.get("title", "").strip()
    if not title:
        return _problem(400, "Bad Request", "title is required")
    slug = (body.get("slug") or "").strip() or slugify(title)
    if help_store.find_by_slug(slug):
        return _problem(409, "Conflict", f"Slug '{slug}' already exists")
    article = HelpArticle(
        slug=slug, title=title,
        category=body.get("category") or "General",
        content=body.get("content", ""),
    )
    try:
        help_store.put(article)
    except ValueError as e:
        return _problem(409, "Conflict", str(e))
    return jsonify(article.to_dict()), 201


@help_bp.put("/v1/help/articles/<article_id>")
def update_help_article(article_id):
    a = help_store.get(article_id)
    if not a:
        return _problem(404, "Not Found", f"Help article '{article_id}' not found")
    body = request.get_json(force=True) or {}
    if "title" in body:
        title = (body["title"] or "").strip()
        if not title:
            return _problem(400, "Bad Request", "title cannot be empty")
        a.title = title
    if "slug" in body:
        new_slug = (body["slug"] or "").strip() or slugify(a.title)
        existing = help_store.find_by_slug(new_slug)
        if existing and existing.id != a.id:
            return _problem(409, "Conflict", f"Slug '{new_slug}' already exists")
        a.slug = new_slug
    if "category" in body:
        a.category = body["category"] or "General"
    if "content" in body:
        a.content = body["content"]
    try:
        help_store.put(a)
    except ValueError as e:
        return _problem(409, "Conflict", str(e))
    return jsonify(a.to_dict())


@help_bp.delete("/v1/help/articles/<article_id>")
def delete_help_article(article_id):
    a = help_store.get(article_id)
    if not a:
        return _problem(404, "Not Found", f"Help article '{article_id}' not found")
    help_store.delete(article_id)
    return "", 204
