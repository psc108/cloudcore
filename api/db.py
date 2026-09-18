"""Central SQLite database for CloudCore.

All modules import `get_db()` to get a connection.  The DB is opened once at
startup (WAL mode, foreign-keys on) and shared via a module-level handle.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

_DB_FILE = Path(__file__).parent / "cloudcore.db"
_active_db_file: Path = _DB_FILE  # set for real by init(); see its own comment
_conn: sqlite3.Connection | None = None
_local = threading.local()
_lock = threading.Lock()


class _SerializedConnection(sqlite3.Connection):
    """Connection that serializes writers through the module-level ``_lock``,
    shared across every thread's own connection.

    SQLite (WAL mode included) only ever allows one writer at a time.
    Background work (instance launch/destroy, SG apply/remove — see
    server.py's ``threading.Thread(target=..., daemon=True)`` call sites)
    runs concurrently with the main request thread, each with its own
    connection via get_db()'s thread-local pattern, and with no
    coordination a write-heavy burst (e.g. several instances launching
    at once) contends for SQLite's single write lock. Contended callers
    then fall back to SQLite's own slow busy-wait retry for up to the
    full 30s `timeout` each, which compounds across many threads into
    multi-minute stalls or outright "database is locked" errors.

    A DML statement opens an implicit SQLite transaction (and takes the
    real file-level write lock) on its first execute() and holds it until
    the *separate*, later commit()/rollback() call — call sites in this
    codebase always do those as two distinct calls, often with other work
    in between. Acquiring and releasing ``_lock`` around each individual
    execute() call is therefore not enough: another thread's connection
    can acquire the (by-then-free) Python lock and immediately hit the
    first thread's still-open transaction, hitting the exact same
    OperationalError this is meant to prevent. Instead, once a call opens
    a transaction (``in_transaction`` becomes True), the lock is held
    across subsequent calls on this connection until commit()/rollback()
    closes it; a plain read (no open transaction) releases immediately.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._holding_lock = False

    def _acquire(self):
        if not self._holding_lock:
            _lock.acquire()
            self._holding_lock = True

    def _release_if_idle(self):
        if self._holding_lock and not self.in_transaction:
            self._holding_lock = False
            _lock.release()

    def _release_unconditionally(self):
        if self._holding_lock:
            self._holding_lock = False
            _lock.release()

    def execute(self, *args, **kwargs):
        self._acquire()
        try:
            result = super().execute(*args, **kwargs)
        except Exception:
            # A failed statement (e.g. a UNIQUE-constraint IntegrityError)
            # leaves in_transaction True with no automatic rollback — call
            # sites across this codebase generally don't catch sqlite
            # errors and roll back explicitly. Releasing unconditionally
            # here trades a (rare, already-buggy) fallback to SQLite's own
            # native busy-wait for that lingering transaction, instead of
            # this thread holding the process-wide lock forever and
            # blocking every other write with no timeout at all.
            self._release_unconditionally()
            raise
        self._release_if_idle()
        return result

    def executemany(self, *args, **kwargs):
        self._acquire()
        try:
            result = super().executemany(*args, **kwargs)
        except Exception:
            self._release_unconditionally()
            raise
        self._release_if_idle()
        return result

    def executescript(self, *args, **kwargs):
        self._acquire()
        try:
            result = super().executescript(*args, **kwargs)
        except Exception:
            self._release_unconditionally()
            raise
        self._release_if_idle()
        return result

    def commit(self):
        try:
            return super().commit()
        finally:
            self._release_unconditionally()

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._release_unconditionally()


def _new_conn(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(
        str(path), check_same_thread=True, timeout=30, factory=_SerializedConnection
    )
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS security_groups (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    vpc_id          TEXT NOT NULL DEFAULT '',
    ingress_rules   TEXT NOT NULL DEFAULT '[]',
    egress_rules    TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS vpcs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    cidr_block  TEXT NOT NULL,
    dns_support INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS instances (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    image_id            TEXT NOT NULL,
    flavor              TEXT NOT NULL,
    vpc_id              TEXT NOT NULL,
    subnet_id           TEXT NOT NULL,
    security_group_ids  TEXT NOT NULL DEFAULT '[]',
    usb_device_ids      TEXT NOT NULL DEFAULT '[]',
    user_data           TEXT,
    private_ip          TEXT NOT NULL DEFAULT '',
    public_ip           TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'pending',
    error_message       TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    tags                TEXT NOT NULL DEFAULT '{}',
    domain_name         TEXT NOT NULL DEFAULT '',
    ssh_host_port       INTEGER NOT NULL DEFAULT 0,
    ssh_user            TEXT NOT NULL DEFAULT 'ubuntu',
    users               TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS load_balancers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'application',
    vpc_id      TEXT NOT NULL DEFAULT '',
    subnet_ids  TEXT NOT NULL DEFAULT '[]',
    internal    INTEGER NOT NULL DEFAULT 0,
    dns_name    TEXT NOT NULL DEFAULT '',
    listen_port INTEGER NOT NULL DEFAULT 0,
    backends    TEXT NOT NULL DEFAULT '[]',
    listeners   TEXT NOT NULL DEFAULT '[]',
    health_check TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS subnets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    vpc_id      TEXT NOT NULL,
    cidr_block  TEXT NOT NULL,
    public      INTEGER NOT NULL DEFAULT 0,
    zone        TEXT NOT NULL DEFAULT 'a',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS internet_gateways (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    vpc_id      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS route_tables (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    vpc_id      TEXT NOT NULL,
    subnet_ids  TEXT NOT NULL DEFAULT '[]',
    routes      TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dns_zones (
    name        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dns_records (
    id              TEXT PRIMARY KEY,
    zone_name       TEXT NOT NULL REFERENCES dns_zones(name) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    fqdn            TEXT NOT NULL,
    type            TEXT NOT NULL,
    value           TEXT NOT NULL,
    ttl             INTEGER NOT NULL DEFAULT 300,
    resource_type   TEXT NOT NULL DEFAULT 'manual',
    resource_id     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    UNIQUE(zone_name, name, type)
);

CREATE TABLE IF NOT EXISTS nfs_servers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    vpc_id        TEXT NOT NULL,
    flavor        TEXT NOT NULL DEFAULT 'standard.medium',
    disk_gb       INTEGER NOT NULL DEFAULT 20,
    status        TEXT NOT NULL DEFAULT 'pending',
    private_ip    TEXT NOT NULL DEFAULT '',
    ssh_host_port INTEGER NOT NULL DEFAULT 0,
    domain_name   TEXT NOT NULL DEFAULT '',
    shares        TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tofu_builds (
    id              TEXT PRIMARY KEY,
    template        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    created_by      TEXT NOT NULL DEFAULT 'ui',
    var_overrides   TEXT NOT NULL DEFAULT '{}',
    log             TEXT NOT NULL DEFAULT '[]',
    exit_code       INTEGER,
    provisioned     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS builds (
    id              TEXT PRIMARY KEY,
    template        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    created_by      TEXT NOT NULL DEFAULT 'ui',
    var_overrides   TEXT NOT NULL DEFAULT '{}',
    log             TEXT NOT NULL DEFAULT '[]',
    exit_code       INTEGER,
    provisioned     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS help_articles (
    id          TEXT PRIMARY KEY,
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'General',
    content     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Slug uniqueness is scoped to non-deleted articles (not a plain column
-- UNIQUE) so a slug frees up for reuse once its article is soft-deleted,
-- matching help_store.find_by_slug()'s own "status != 'deleted'" check.
CREATE UNIQUE INDEX IF NOT EXISTS help_articles_slug_active_uq
    ON help_articles(slug) WHERE status != 'deleted';

-- Generic key/value settings store. Values are stored as JSON text so any
-- setting type (int, bool, string, null-to-unset) round-trips without a
-- schema change; api/settings_store.py owns interpreting each key.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Cross-host peering (see api/peers_routes.py). A "peer" row only exists
-- once pairing has actually started — direction/status track which side
-- of the handshake this row represents and how far it's got.
-- direction: 'outbound' (we initiated) | 'inbound' (they initiated).
-- status:    pending_outbound | approved | rejected | revoked.
-- local_token: minted by US, for THEM to present back to us.
-- remote_token: minted by THEM, for US to present back to them.
CREATE TABLE IF NOT EXISTS peers (
    id                TEXT PRIMARY KEY,
    hostname          TEXT NOT NULL,
    pubkey            TEXT NOT NULL,
    pubkey_fpr        TEXT NOT NULL,
    api_url           TEXT NOT NULL,
    direction         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending_outbound',
    local_token       TEXT,
    remote_token      TEXT,
    wg_pubkey         TEXT,
    wg_endpoint       TEXT,
    wg_bridge_subnet  TEXT,
    wg_transit_ip     TEXT,
    wg_tunnel_status  TEXT NOT NULL DEFAULT 'unknown',
    created_at        TEXT NOT NULL,
    approved_at       TEXT
);

-- An incoming pairing request awaiting this host's own human approval
-- (POST /v1/peers/pairing-requests, the one intentionally unauthenticated
-- bootstrap route — see api/peers_routes.py). Time-bounded (expires_at)
-- so an unapproved request doesn't linger indefinitely in the UI.
CREATE TABLE IF NOT EXISTS pairing_requests (
    id              TEXT PRIMARY KEY,
    hostname        TEXT NOT NULL,
    pubkey          TEXT NOT NULL,
    pubkey_fpr      TEXT NOT NULL,
    signature       TEXT NOT NULL,
    callback_token  TEXT NOT NULL,
    -- Built from the request's own actual TCP source address (never a
    -- self-reported hostname/IP in the signed payload) + the claimed
    -- peer-listener port — see api/peers_routes.py's bootstrap handler.
    callback_url    TEXT NOT NULL DEFAULT '',
    wg_pubkey       TEXT NOT NULL,
    wg_endpoint     TEXT NOT NULL,
    wg_bridge_subnet TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

-- Scheduler (api/scheduler.py). recurrence_type 'once' uses run_at (an
-- explicit ISO timestamp — a calendar date has no cron field); 'cron'
-- uses cron_expr, built server-side from the dashboard's simple
-- picker (api/croncalc.py's build_cron()) — no raw cron ever accepted
-- from a client. kind 'llm_ingest' is the only kind that uses
-- sentinel_checkpoint_event_id; ignored for kind 'build'.
CREATE TABLE IF NOT EXISTS schedules (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    kind              TEXT NOT NULL,
    engine            TEXT NOT NULL DEFAULT '',
    template          TEXT NOT NULL DEFAULT '',
    var_overrides     TEXT NOT NULL DEFAULT '{}',
    recurrence_type   TEXT NOT NULL,
    run_at            TEXT,
    cron_expr         TEXT NOT NULL DEFAULT '',
    enabled           INTEGER NOT NULL DEFAULT 1,
    next_run_at       TEXT,
    last_run_at       TEXT,
    last_status       TEXT NOT NULL DEFAULT '',
    sentinel_checkpoint_event_id INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL DEFAULT 'ui'
);

CREATE TABLE IF NOT EXISTS schedule_runs (
    id            TEXT PRIMARY KEY,
    schedule_id   TEXT NOT NULL REFERENCES schedules(id),
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    summary       TEXT NOT NULL DEFAULT '',
    log           TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS schedule_runs_schedule_idx ON schedule_runs(schedule_id);

-- One row per llm_ingest run, structured detail alongside that run's
-- own schedule_runs row (which just carries the generic status/log
-- every schedule kind gets). The *_seconds/token/stats columns back
-- the LLM Performance page (api/scheduler.py's own timing/usage
-- capture around the ephemeral build->health->inference cycle) —
-- deliberately generic column names (not "7b_" prefixed etc.) so a
-- future schedule pointed at a different template/model populates the
-- same columns without a schema change.
CREATE TABLE IF NOT EXISTS llm_ingestions (
    id                    TEXT PRIMARY KEY,
    schedule_id           TEXT NOT NULL,
    run_id                TEXT NOT NULL,
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    events_seen           INTEGER NOT NULL DEFAULT 0,
    findings_created      INTEGER NOT NULL DEFAULT 0,
    suggestions_created   INTEGER NOT NULL DEFAULT 0,
    peers_synced          TEXT NOT NULL DEFAULT '[]',
    summary_text          TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'running',
    cluster_build_seconds REAL,
    model_load_seconds    REAL,
    inference_seconds     REAL,
    prompt_tokens         INTEGER,
    completion_tokens     INTEGER,
    total_tokens          INTEGER,
    tokens_per_second     REAL,
    coordinator_stats     TEXT NOT NULL DEFAULT '{}',
    worker_stats          TEXT NOT NULL DEFAULT '[]'
);

-- A failed build's own log, queued for the next llm_ingest wakeup to
-- analyze (api/failure_queue.py) — per direct request: "keep failed
-- build logs, pass them through the llm, record the reasons and
-- whatever the llm thinks might be a resolution and then remove the
-- log(s)... it'll at the very least start collecting a good db of
-- issues as we go along." Rows are deleted once analyzed — the drafted
-- Finding in Sentinel's own KB becomes the permanent record, not this
-- table; this is working material only, not history.
CREATE TABLE IF NOT EXISTS failed_build_logs (
    id            TEXT PRIMARY KEY,
    engine        TEXT NOT NULL,
    build_id      TEXT NOT NULL,
    template      TEXT NOT NULL,
    var_overrides TEXT NOT NULL DEFAULT '{}',
    log           TEXT NOT NULL DEFAULT '[]',
    exit_code     INTEGER,
    created_at    TEXT NOT NULL
);

-- Every llm-chat grounded-verification transaction (Phase 1/2's own
-- real execution result, pass or fail, plus a fix round if one ran) —
-- captured unconditionally, nothing filtered at capture time, so this
-- table doubles as the exportable training corpus. `status` controls
-- only what the separate student review page shows (an instructor
-- curates by publishing good teaching examples), never what's
-- captured. No student/session identity is ever recorded — a shared
-- cohort resource, matching the chat itself having no login. `source`
-- defaults to this coordinator's own capture path but is
-- deliberately generic so a future capture client (e.g. a student
-- running a model locally) could feed the same table without a schema
-- change — not built now, just not designed away.
CREATE TABLE IF NOT EXISTS llm_verification_examples (
    id               TEXT PRIMARY KEY,
    source           TEXT NOT NULL DEFAULT 'llm-chat-coordinator',
    build_id         TEXT NOT NULL DEFAULT '',
    model_filename   TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    generated_code   TEXT NOT NULL,
    exec_stdout      TEXT NOT NULL DEFAULT '',
    exec_stderr      TEXT NOT NULL DEFAULT '',
    exec_exit_code   INTEGER,
    passed           INTEGER NOT NULL DEFAULT 0,
    fix_explanation  TEXT NOT NULL DEFAULT '',
    fixed_code       TEXT NOT NULL DEFAULT '',
    fix_exec_stdout  TEXT NOT NULL DEFAULT '',
    fix_exec_stderr  TEXT NOT NULL DEFAULT '',
    fix_passed       INTEGER,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS help_articles_fts USING fts5(
    title, category, content,
    content='help_articles', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS help_articles_ai AFTER INSERT ON help_articles BEGIN
  INSERT INTO help_articles_fts(rowid, title, category, content)
  VALUES (new.rowid, new.title, new.category, new.content);
END;

CREATE TRIGGER IF NOT EXISTS help_articles_ad AFTER DELETE ON help_articles BEGIN
  INSERT INTO help_articles_fts(help_articles_fts, rowid, title, category, content)
  VALUES ('delete', old.rowid, old.title, old.category, old.content);
END;

CREATE TRIGGER IF NOT EXISTS help_articles_au AFTER UPDATE ON help_articles BEGIN
  INSERT INTO help_articles_fts(help_articles_fts, rowid, title, category, content)
  VALUES ('delete', old.rowid, old.title, old.category, old.content);
  INSERT INTO help_articles_fts(rowid, title, category, content)
  VALUES (new.rowid, new.title, new.category, new.content);
END;
"""


def init(db_file: Path | None = None) -> None:
    """Open the database, apply schema, migrate from JSON if needed."""
    global _conn, _active_db_file
    path = db_file or _DB_FILE
    # get_db() (every other module's own entry point — store.py,
    # settings_store.py, etc.) opened its thread-local connections
    # against the hardcoded _DB_FILE constant regardless of what path
    # was actually passed here — invisible in production (init() is
    # always called with no argument there, so the two already
    # coincided), but it meant a test harness pointing init() at a
    # scratch file had every actual read/write silently land on the
    # real cloudcore.db instead, via any get_db() caller. Tracking the
    # path actually in use and having get_db() (and _migrate_json()'s
    # own JSON-sibling-file lookup, same bug) read it back closes that
    # gap for good, not just for this feature's own tests.
    _active_db_file = path
    _conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(_SCHEMA)
    _conn.commit()
    _migrate_columns()
    _migrate_help_articles_slug_uniqueness()
    _migrate_json()
    _seed_help_from_markdown()


def _migrate_columns() -> None:
    """Add columns introduced after initial schema release."""
    existing = {row[1] for row in _conn.execute("PRAGMA table_info(instances)").fetchall()}
    if "http_host_port" not in existing:
        _conn.execute("ALTER TABLE instances ADD COLUMN http_host_port INTEGER NOT NULL DEFAULT 0")
    if "usb_device_ids" not in existing:
        _conn.execute("ALTER TABLE instances ADD COLUMN usb_device_ids TEXT NOT NULL DEFAULT '[]'")
    if "error_message" not in existing:
        _conn.execute("ALTER TABLE instances ADD COLUMN error_message TEXT NOT NULL DEFAULT ''")
    if "host_id" not in existing:
        # NULL = local (created on this host, the default for every
        # instance before cross-host peering existed); non-NULL = the
        # peers.id this instance actually lives on — see api/peers_routes.py.
        _conn.execute("ALTER TABLE instances ADD COLUMN host_id TEXT")

    vpc_cols = {row[1] for row in _conn.execute("PRAGMA table_info(vpcs)").fetchall()}
    if "host_id" not in vpc_cols:
        # Same field, same meaning as instances.host_id — VPCs, subnets,
        # and security groups can now be peer-placed too, not just
        # instances (per direct request — "do we have to limit resource
        # placement to just instances?").
        _conn.execute("ALTER TABLE vpcs ADD COLUMN host_id TEXT")

    subnet_cols = {row[1] for row in _conn.execute("PRAGMA table_info(subnets)").fetchall()}
    if "host_id" not in subnet_cols:
        _conn.execute("ALTER TABLE subnets ADD COLUMN host_id TEXT")

    sg_cols = {row[1] for row in _conn.execute("PRAGMA table_info(security_groups)").fetchall()}
    if "host_id" not in sg_cols:
        _conn.execute("ALTER TABLE security_groups ADD COLUMN host_id TEXT")

    pr_cols = {row[1] for row in _conn.execute("PRAGMA table_info(pairing_requests)").fetchall()}
    if pr_cols and "callback_url" not in pr_cols:
        _conn.execute("ALTER TABLE pairing_requests ADD COLUMN callback_url TEXT NOT NULL DEFAULT ''")

    lb_cols = {row[1] for row in _conn.execute("PRAGMA table_info(load_balancers)").fetchall()}
    if "sticky_sessions" not in lb_cols:
        _conn.execute("ALTER TABLE load_balancers ADD COLUMN sticky_sessions INTEGER NOT NULL DEFAULT 0")
    if "cookie_name" not in lb_cols:
        _conn.execute("ALTER TABLE load_balancers ADD COLUMN cookie_name TEXT NOT NULL DEFAULT 'SERVERID'")
    if "target_groups" not in lb_cols:
        _conn.execute("ALTER TABLE load_balancers ADD COLUMN target_groups TEXT NOT NULL DEFAULT '[]'")
    if "deletion_protection" not in lb_cols:
        _conn.execute("ALTER TABLE load_balancers ADD COLUMN deletion_protection INTEGER NOT NULL DEFAULT 0")

    # llm_ingestions predates the LLM Performance page's own timing/
    # token/resource-usage capture (this table itself was added earlier
    # in the same overall feature) — an already-initialized install has
    # the table but not these columns.
    ing_cols = {row[1] for row in _conn.execute("PRAGMA table_info(llm_ingestions)").fetchall()}
    if ing_cols and "cluster_build_seconds" not in ing_cols:
        _conn.execute("ALTER TABLE llm_ingestions ADD COLUMN cluster_build_seconds REAL")
        _conn.execute("ALTER TABLE llm_ingestions ADD COLUMN model_load_seconds REAL")
        _conn.execute("ALTER TABLE llm_ingestions ADD COLUMN inference_seconds REAL")
        _conn.execute("ALTER TABLE llm_ingestions ADD COLUMN prompt_tokens INTEGER")
        _conn.execute("ALTER TABLE llm_ingestions ADD COLUMN completion_tokens INTEGER")
        _conn.execute("ALTER TABLE llm_ingestions ADD COLUMN total_tokens INTEGER")
        _conn.execute("ALTER TABLE llm_ingestions ADD COLUMN tokens_per_second REAL")
        _conn.execute("ALTER TABLE llm_ingestions ADD COLUMN coordinator_stats TEXT NOT NULL DEFAULT '{}'")
        _conn.execute("ALTER TABLE llm_ingestions ADD COLUMN worker_stats TEXT NOT NULL DEFAULT '[]'")
    _conn.commit()


def _migrate_help_articles_slug_uniqueness() -> None:
    """Replace the old column-level UNIQUE(slug) — enforced against every
    row including soft-deleted ones — with a partial unique index scoped
    to status != 'deleted'. help_store.find_by_slug() already only checks
    non-deleted rows for a conflict, so a stale soft-deleted row was
    silently invisible to that pre-check yet still blocked a fresh INSERT
    at the DB level, raising an uncaught IntegrityError. SQLite can't drop
    a column-level UNIQUE via ALTER TABLE, so this rebuilds the table when
    the old constraint is still present; a fresh database already gets the
    corrected schema directly from _SCHEMA and skips this entirely.
    """
    import re

    row = _conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='help_articles'"
    ).fetchone()
    if row is None or not re.search(r"slug\s+TEXT\s+NOT\s+NULL\s+UNIQUE", row[0], re.IGNORECASE):
        return
    _conn.executescript("""
        CREATE TABLE help_articles_new (
            id          TEXT PRIMARY KEY,
            slug        TEXT NOT NULL,
            title       TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'General',
            content     TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'active',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        INSERT INTO help_articles_new (id, slug, title, category, content, status, created_at, updated_at)
            SELECT id, slug, title, category, content, status, created_at, updated_at FROM help_articles;
        DROP TABLE help_articles;
        ALTER TABLE help_articles_new RENAME TO help_articles;
        CREATE UNIQUE INDEX IF NOT EXISTS help_articles_slug_active_uq
            ON help_articles(slug) WHERE status != 'deleted';
        CREATE TRIGGER IF NOT EXISTS help_articles_ai AFTER INSERT ON help_articles BEGIN
          INSERT INTO help_articles_fts(rowid, title, category, content)
          VALUES (new.rowid, new.title, new.category, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS help_articles_ad AFTER DELETE ON help_articles BEGIN
          INSERT INTO help_articles_fts(help_articles_fts, rowid, title, category, content)
          VALUES ('delete', old.rowid, old.title, old.category, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS help_articles_au AFTER UPDATE ON help_articles BEGIN
          INSERT INTO help_articles_fts(help_articles_fts, rowid, title, category, content)
          VALUES ('delete', old.rowid, old.title, old.category, old.content);
          INSERT INTO help_articles_fts(rowid, title, category, content)
          VALUES (new.rowid, new.title, new.category, new.content);
        END;
    """)
    # Table was dropped/rebuilt with new rowids — the FTS5 external-content
    # index no longer lines up with them until forced to rebuild.
    _conn.execute("INSERT INTO help_articles_fts(help_articles_fts) VALUES('rebuild')")
    _conn.commit()


_HELP_SEED_FILE = Path(__file__).parent / "help_seed.json"


def _seed_help_from_markdown() -> None:
    """One-time import of help_seed.json into help_articles. Gated on the
    TABLE being empty (never on file existence). help_seed.json is the sole
    source of default help content — the searchable help_articles table
    (managed via the Help Manager UI / /v1/help/articles API) is the only
    help system; there is no separate static-Markdown help view to keep in
    sync with it."""
    if _conn.execute("SELECT COUNT(*) FROM help_articles").fetchone()[0]:
        return
    if not _HELP_SEED_FILE.exists():
        return
    import json as _json
    from models import new_id, now_iso
    seeds = _json.loads(_HELP_SEED_FILE.read_text())
    ts = now_iso()
    for seed in seeds:
        _conn.execute(
            """INSERT OR IGNORE INTO help_articles
               (id,slug,title,category,content,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (new_id(), seed["slug"], seed["title"], seed["category"], seed["content"],
             "active", ts, ts))
    _conn.commit()


def get_db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.init() has not been called")
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = _new_conn(_active_db_file)
    return _local.conn


# ---------------------------------------------------------------------------
# JSON migration (runs once — renames files after import)
# ---------------------------------------------------------------------------

def _migrate_json() -> None:
    base = _active_db_file.parent

    state_file = base / "state.json"
    if state_file.exists():
        _import_state_json(state_file)
        state_file.rename(base / "state.json.migrated")

    dns_file = base / "dns.json"
    if dns_file.exists():
        _import_dns_json(dns_file)
        dns_file.rename(base / "dns.json.migrated")

    builds_file = base / "builds.json"
    if builds_file.exists():
        _import_builds_json(builds_file)
        builds_file.rename(base / "builds.json.migrated")


def _import_state_json(path: Path) -> None:
    data = json.loads(path.read_text())
    c = _conn
    for v in data.get("vpcs", {}).values():
        c.execute("""INSERT OR IGNORE INTO vpcs
            (id,name,cidr_block,dns_support,status,created_at,tags) VALUES
            (?,?,?,?,?,?,?)""",
            (v["id"], v["name"], v["cidr_block"], int(v.get("dns_support", True)),
             v.get("status", "active"), v["created_at"], json.dumps(v.get("tags", {}))))
    for i in data.get("instances", {}).values():
        c.execute("""INSERT OR IGNORE INTO instances
            (id,name,image_id,flavor,vpc_id,subnet_id,security_group_ids,
             user_data,private_ip,public_ip,status,created_at,tags,
             domain_name,ssh_host_port,ssh_user,users) VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (i["id"], i["name"], i["image_id"], i["flavor"], i["vpc_id"],
             i["subnet_id"], json.dumps(i.get("security_group_ids", [])),
             i.get("user_data"), i.get("private_ip", ""), i.get("public_ip", ""),
             i.get("status", "pending"), i["created_at"], json.dumps(i.get("tags", {})),
             i.get("domain_name", ""), i.get("ssh_port", 0),
             i.get("ssh_user", "ubuntu"), json.dumps(i.get("users", []))))
    for lb in data.get("load_balancers", {}).values():
        c.execute("""INSERT OR IGNORE INTO load_balancers
            (id,name,type,vpc_id,subnet_ids,internal,dns_name,listen_port,
             backends,status,created_at,tags) VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lb["id"], lb["name"], lb.get("type", "application"), lb.get("vpc_id", ""),
             json.dumps(lb.get("subnet_ids", [])), int(lb.get("internal", False)),
             lb.get("dns_name", ""), lb.get("listen_port", 0),
             json.dumps(lb.get("backends", [])), lb.get("status", "active"),
             lb["created_at"], json.dumps(lb.get("tags", {}))))
    c.commit()


def _import_dns_json(path: Path) -> None:
    from models import now_iso
    from dns import BUILTIN_ZONES
    data = json.loads(path.read_text())
    c = _conn
    for zone_name, zone in data.items():
        builtin = 1 if zone_name in BUILTIN_ZONES else 0
        c.execute("INSERT OR IGNORE INTO dns_zones (name,created_at,builtin) VALUES (?,?,?)",
                  (zone_name, zone.get("created_at", now_iso()), builtin))
        for rec in zone.get("records", {}).values():
            c.execute("""INSERT OR IGNORE INTO dns_records
                (id,zone_name,name,fqdn,type,value,ttl,resource_type,resource_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (str(__import__("uuid").uuid4()), zone_name, rec["name"], rec.get("fqdn", ""),
                 rec["type"], rec["value"], rec.get("ttl", 300),
                 rec.get("resource_type", "manual"), rec.get("resource_id", ""),
                 rec.get("created_at", now_iso())))
    c.commit()


def _import_builds_json(path: Path) -> None:
    data = json.loads(path.read_text())
    c = _conn
    for b in data.values():
        c.execute("""INSERT OR IGNORE INTO builds
            (id,template,status,created_at,started_at,finished_at,created_by,
             var_overrides,log,exit_code,provisioned) VALUES
            (?,?,?,?,?,?,?,?,?,?,?)""",
            (b["id"], b["template"], b["status"], b["created_at"],
             b.get("started_at"), b.get("finished_at"), b.get("created_by", "ui"),
             json.dumps(b.get("var_overrides", {})), json.dumps(b.get("log", [])),
             b.get("exit_code"), json.dumps(b.get("provisioned", []))))
    c.commit()
