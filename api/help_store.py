"""SQLite CRUD + FTS5 search for help articles."""
from __future__ import annotations

from typing import Optional

import db
from models import HelpArticle, HelpArticleStatus, now_iso


def _from_row(row) -> HelpArticle:
    return HelpArticle(
        id=row["id"], slug=row["slug"], title=row["title"],
        category=row["category"], content=row["content"],
        status=HelpArticleStatus(row["status"]),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def put(a: HelpArticle) -> None:
    a.updated_at = now_iso()
    db.get_db().execute("""INSERT INTO help_articles
        (id,slug,title,category,content,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            slug=excluded.slug, title=excluded.title, category=excluded.category,
            content=excluded.content, status=excluded.status, updated_at=excluded.updated_at""",
        (a.id, a.slug, a.title, a.category, a.content, a.status.value,
         a.created_at, a.updated_at))
    db.get_db().commit()


def get(article_id: str) -> Optional[HelpArticle]:
    row = db.get_db().execute(
        "SELECT * FROM help_articles WHERE id=? AND status != 'deleted'", (article_id,)).fetchone()
    return _from_row(row) if row else None


def find_by_slug(slug: str) -> Optional[HelpArticle]:
    row = db.get_db().execute(
        "SELECT * FROM help_articles WHERE slug=? AND status != 'deleted'", (slug,)).fetchone()
    return _from_row(row) if row else None


def list_all() -> list[HelpArticle]:
    rows = db.get_db().execute(
        "SELECT * FROM help_articles WHERE status != 'deleted' ORDER BY category, title").fetchall()
    return [_from_row(r) for r in rows]


def search(query: str) -> list[HelpArticle]:
    q = query.strip()
    if not q:
        return list_all()
    match = " ".join(f'"{t}"*' for t in q.split())
    rows = db.get_db().execute("""
        SELECT h.* FROM help_articles_fts
        JOIN help_articles h ON h.rowid = help_articles_fts.rowid
        WHERE help_articles_fts MATCH ? AND h.status != 'deleted'
        ORDER BY bm25(help_articles_fts)""", (match,)).fetchall()
    return [_from_row(r) for r in rows]


def delete(article_id: str) -> bool:
    c = db.get_db()
    r = c.execute(
        "UPDATE help_articles SET status='deleted' WHERE id=? AND status != 'deleted'", (article_id,))
    c.commit()
    return r.rowcount > 0
