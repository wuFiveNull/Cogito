"""
cogito.database.schema — 完整数据库 Schema (DDL)

对应设计文档第 7 节。
"""

# ── 连接初始 PRAGMA ──────────────────────────────────────────────

CONFIG_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA journal_size_limit = 67108864;
"""

# ── 建表 SQL ─────────────────────────────────────────────────────

CREATE_TRACE_EVENTS = """
CREATE TABLE IF NOT EXISTS trace_events (
    id                      TEXT PRIMARY KEY,
    trace_id                TEXT NOT NULL,
    parent_span_id          TEXT REFERENCES trace_events(id),

    user_id                 TEXT NOT NULL,
    session_id              TEXT,

    step_type               TEXT NOT NULL,
    step_name               TEXT NOT NULL,

    status                  TEXT NOT NULL DEFAULT 'running'
                            CHECK (
                                status IN (
                                    'running',
                                    'success',
                                    'failed',
                                    'timeout',
                                    'cancelled'
                                )
                            ),

    input_event_ids_json    TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(input_event_ids_json)
                                AND json_type(input_event_ids_json) = 'array'
                            ),

    input_memory_ids_json   TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(input_memory_ids_json)
                                AND json_type(input_memory_ids_json) = 'array'
                            ),

    output_event_ids_json   TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(output_event_ids_json)
                                AND json_type(output_event_ids_json) = 'array'
                            ),

    output_memory_ids_json  TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(output_memory_ids_json)
                                AND json_type(output_memory_ids_json) = 'array'
                            ),

    model_name              TEXT,
    prompt_version          TEXT,

    tool_name               TEXT,
    tool_call_id            TEXT,
    attempt_no              INTEGER NOT NULL DEFAULT 1
                            CHECK (attempt_no >= 1),

    decision                TEXT,
    decision_reason         TEXT,

    metadata_json           TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(metadata_json)),

    input_hash              TEXT,
    output_hash             TEXT,

    started_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    ended_at                TEXT,
    latency_ms              INTEGER CHECK (
                                latency_ms IS NULL OR latency_ms >= 0
                            ),

    error_code              TEXT,
    error_message           TEXT,

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            )
) STRICT;
"""

CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    id                      TEXT PRIMARY KEY,

    user_id                 TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    seq_no                  INTEGER NOT NULL CHECK (seq_no >= 1),

    role                    TEXT NOT NULL
                            CHECK (
                                role IN (
                                    'user',
                                    'assistant',
                                    'system',
                                    'tool'
                                )
                            ),

    event_type              TEXT NOT NULL,

    content                 TEXT NOT NULL DEFAULT '',
    content_json            TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(content_json)),

    trace_id                TEXT,
    created_by_span_id      TEXT REFERENCES trace_events(id),

    extraction_status       TEXT NOT NULL DEFAULT 'pending'
                            CHECK (
                                extraction_status IN (
                                    'pending',
                                    'processing',
                                    'done',
                                    'failed',
                                    'ignored'
                                )
                            ),

    extraction_group_id     TEXT,
    extraction_attempts     INTEGER NOT NULL DEFAULT 0
                            CHECK (extraction_attempts >= 0),
    extraction_error        TEXT,
    extracted_at            TEXT,

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),

    UNIQUE(user_id, session_id, seq_no)
) STRICT;
"""

CREATE_MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    id                      TEXT PRIMARY KEY,

    user_id                 TEXT NOT NULL,

    memory_type             TEXT NOT NULL
                            CHECK (
                                memory_type IN (
                                    'fact',
                                    'preference',
                                    'rule',
                                    'event'
                                )
                            ),

    memory_key              TEXT NOT NULL,

    content                 TEXT NOT NULL,
    value_json              TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(value_json)),

    embedding               BLOB,
    embedding_dim           INTEGER CHECK (
                                embedding_dim IS NULL OR embedding_dim > 0
                            ),
    embedding_model         TEXT,
    embedding_format        TEXT DEFAULT 'float32-le',

    importance              REAL NOT NULL DEFAULT 0.5
                            CHECK (
                                importance >= 0.0
                                AND importance <= 1.0
                            ),

    confidence              REAL NOT NULL DEFAULT 0.8
                            CHECK (
                                confidence >= 0.0
                                AND confidence <= 1.0
                            ),

    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (
                                status IN (
                                    'active',
                                    'superseded',
                                    'deleted'
                                )
                            ),

    valid_from              TEXT,
    valid_until             TEXT,

    source_group_id         TEXT,

    source_event_ids_json   TEXT NOT NULL DEFAULT '[]'
                            CHECK (
                                json_valid(source_event_ids_json)
                                AND json_type(source_event_ids_json) = 'array'
                            ),

    supersedes_id           TEXT REFERENCES memories(id),

    created_by_span_id      TEXT REFERENCES trace_events(id),
    updated_by_span_id      TEXT REFERENCES trace_events(id),

    access_count            INTEGER NOT NULL DEFAULT 0
                            CHECK (access_count >= 0),
    last_accessed_at        TEXT,

    created_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            ),
    updated_at              TEXT NOT NULL DEFAULT (
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                            )
) STRICT;
"""

# ── 索引 SQL ─────────────────────────────────────────────────────

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_trace_events_trace "
    "ON trace_events(trace_id, started_at);",

    "CREATE INDEX IF NOT EXISTS idx_trace_events_parent "
    "ON trace_events(parent_span_id);",

    "CREATE INDEX IF NOT EXISTS idx_trace_events_user_session "
    "ON trace_events(user_id, session_id, started_at DESC);",

    "CREATE INDEX IF NOT EXISTS idx_trace_events_step_type "
    "ON trace_events(step_type, started_at DESC);",

    "CREATE INDEX IF NOT EXISTS idx_trace_events_tool "
    "ON trace_events(tool_name, started_at DESC) "
    "WHERE tool_name IS NOT NULL;",

    "CREATE INDEX IF NOT EXISTS idx_trace_events_tool_call "
    "ON trace_events(tool_call_id, attempt_no) "
    "WHERE tool_call_id IS NOT NULL;",

    "CREATE INDEX IF NOT EXISTS idx_events_session_seq "
    "ON events(user_id, session_id, seq_no);",

    "CREATE INDEX IF NOT EXISTS idx_events_trace "
    "ON events(trace_id, created_at);",

    "CREATE INDEX IF NOT EXISTS idx_events_pending_extraction "
    "ON events(user_id, session_id, extraction_status, seq_no) "
    "WHERE extraction_status IN ('pending', 'failed');",

    "CREATE INDEX IF NOT EXISTS idx_events_extraction_group "
    "ON events(extraction_group_id, seq_no) "
    "WHERE extraction_group_id IS NOT NULL;",

    "CREATE INDEX IF NOT EXISTS idx_events_type_time "
    "ON events(user_id, event_type, created_at DESC);",

    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_key "
    "ON memories(user_id, memory_key) "
    "WHERE status = 'active';",

    "CREATE INDEX IF NOT EXISTS idx_memories_user_status "
    "ON memories(user_id, status);",

    "CREATE INDEX IF NOT EXISTS idx_memories_user_type "
    "ON memories(user_id, memory_type, status);",

    "CREATE INDEX IF NOT EXISTS idx_memories_validity "
    "ON memories(user_id, valid_from, valid_until) "
    "WHERE status = 'active';",

    "CREATE INDEX IF NOT EXISTS idx_memories_source_group "
    "ON memories(source_group_id) "
    "WHERE source_group_id IS NOT NULL;",

    "CREATE INDEX IF NOT EXISTS idx_memories_created_span "
    "ON memories(created_by_span_id);",
]

# ── 触发器 SQL ───────────────────────────────────────────────────

CREATE_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS trg_events_touch_updated_at
    AFTER UPDATE ON events
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
    BEGIN
        UPDATE events
        SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = NEW.id;
    END;
    """,

    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_touch_updated_at
    AFTER UPDATE ON memories
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
    BEGIN
        UPDATE memories
        SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = NEW.id;
    END;
    """,
]

# ── FTS5 虚拟表 + 同步触发器 ──────────────────────────────────────

CREATE_MEMORIES_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    user_id UNINDEXED,
    memory_key,
    content,
    content = 'memories',
    content_rowid = 'rowid',
    tokenize = 'trigram'
);
"""

CREATE_FTS_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ai
    AFTER INSERT ON memories
    BEGIN
        INSERT INTO memories_fts(rowid, user_id, memory_key, content)
        VALUES (NEW.rowid, NEW.user_id, NEW.memory_key, NEW.content);
    END;
    """,

    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ad
    AFTER DELETE ON memories
    BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, user_id, memory_key, content)
        VALUES ('delete', OLD.rowid, OLD.user_id, OLD.memory_key, OLD.content);
    END;
    """,

    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_au
    AFTER UPDATE OF user_id, memory_key, content ON memories
    BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, user_id, memory_key, content)
        VALUES ('delete', OLD.rowid, OLD.user_id, OLD.memory_key, OLD.content);
        INSERT INTO memories_fts(rowid, user_id, memory_key, content)
        VALUES (NEW.rowid, NEW.user_id, NEW.memory_key, NEW.content);
    END;
    """,
]

# ── 版本信息 ─────────────────────────────────────────────────────

SCHEMA_VERSION = 4


def get_ddl_statements() -> list[str]:
    """返回完整的 DDL 语句列表（按依赖顺序）。"""
    stmts = []
    stmts.append(CONFIG_SQL)
    stmts.append(CREATE_TRACE_EVENTS)
    stmts.append(CREATE_EVENTS)
    stmts.append(CREATE_MEMORIES)
    stmts.extend(CREATE_INDEXES)
    stmts.extend(CREATE_TRIGGERS)
    stmts.append(CREATE_MEMORIES_FTS)
    stmts.extend(CREATE_FTS_TRIGGERS)
    return stmts
