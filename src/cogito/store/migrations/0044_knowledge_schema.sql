-- 0044: Knowledge 聚合 schema (PLAN-13 P13-07 M4)
-- Applied at version 44
--
-- Resource → Document → Segment 三层内容记忆聚合。
-- KnowledgeService 是唯一写入者；不污染 MemoryItem。

CREATE TABLE IF NOT EXISTS knowledge_resources (
    resource_id        TEXT PRIMARY KEY,
    principal_id       TEXT NOT NULL DEFAULT '',
    connector_id       TEXT NOT NULL DEFAULT '',
    source_uri_hash    TEXT NOT NULL DEFAULT '',
    source_kind        TEXT NOT NULL DEFAULT 'explicit_local_file'
                        CHECK(source_kind IN (
                            'connector','explicit_local_file','pdf_office',
                            'multimodal_text','url_fetch'
                        )),
    media_type         TEXT NOT NULL DEFAULT 'text/markdown',
    payload_ref        TEXT NOT NULL DEFAULT '',
    content_hash       TEXT NOT NULL DEFAULT '',
    trust_label        TEXT NOT NULL DEFAULT 'unverified',
    scope_type         TEXT NOT NULL DEFAULT 'global',
    scope_id           TEXT NOT NULL DEFAULT '',
    source_version     TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'discovered'
                        CHECK(status IN (
                            'discovered','queued','processing',
                            'active','failed','stale','deleted'
                        )),
    retention_class    TEXT NOT NULL DEFAULT 'normal',
    created_at         TEXT NOT NULL,
    updated_at         TEXT,
    deleted_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_kr_status ON knowledge_resources(status, principal_id);
CREATE INDEX IF NOT EXISTS idx_kr_uri ON knowledge_resources(source_uri_hash);
CREATE INDEX IF NOT EXISTS idx_kr_connector ON knowledge_resources(connector_id);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    document_id        TEXT PRIMARY KEY,
    resource_id        NOT NULL REFERENCES knowledge_resources(resource_id),
    title              TEXT NOT NULL DEFAULT '',
    normalized_text_ref TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    language           TEXT NOT NULL DEFAULT 'zh',
    parser_id          TEXT NOT NULL DEFAULT 'markdown',
    parser_version     TEXT NOT NULL DEFAULT '1',
    content_version    TEXT NOT NULL DEFAULT '1',
    status             TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','stale','failed','deleted')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_kd_resource ON knowledge_documents(resource_id, status);

CREATE TABLE IF NOT EXISTS knowledge_segments (
    segment_id         TEXT PRIMARY KEY,
    document_id        TEXT NOT NULL REFERENCES knowledge_documents(document_id),
    ordinal            INTEGER NOT NULL DEFAULT 0,
    segment_kind       TEXT NOT NULL DEFAULT 'paragraph'
                        CHECK(segment_kind IN (
                            'paragraph','heading','code','list_item','table'
                        )),
    text_ref_or_inline TEXT NOT NULL DEFAULT '',
    content_hash       TEXT NOT NULL DEFAULT '',
    token_count        INTEGER NOT NULL DEFAULT 0,
    heading_path       TEXT NOT NULL DEFAULT '',
    start_offset       INTEGER NOT NULL DEFAULT 0,
    end_offset         INTEGER NOT NULL DEFAULT 0,
    embedding_status   TEXT NOT NULL DEFAULT 'pending'
                        CHECK(embedding_status IN ('pending','ready','failed','stale')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT,
    deleted_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_ks_doc ON knowledge_segments(document_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_ks_hash ON knowledge_segments(content_hash);

CREATE TABLE IF NOT EXISTS knowledge_embeddings (
    segment_id         TEXT NOT NULL REFERENCES knowledge_segments(segment_id),
    embedding_model    TEXT NOT NULL DEFAULT '',
    embedding_version  TEXT NOT NULL DEFAULT '',
    vector             BLOB,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (segment_id, embedding_model, embedding_version)
);
