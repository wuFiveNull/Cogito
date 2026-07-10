-- 1004: Multimodal perception layer (PLAN-12 image MVP).
-- Additive migration. Payload bytes remain owned by payload_objects/PayloadStore.

ALTER TABLE content_parts ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS multimodal_assets (
    asset_id                 TEXT PRIMARY KEY,
    payload_ref              TEXT NOT NULL REFERENCES payload_objects(payload_ref),
    sha256                   TEXT NOT NULL UNIQUE,
    perceptual_hash          TEXT NOT NULL DEFAULT '',
    media_kind               TEXT NOT NULL CHECK(media_kind IN ('image','document','audio','video','other')),
    mime_type                TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes               INTEGER NOT NULL DEFAULT 0 CHECK(size_bytes >= 0),
    created_by_principal_id  TEXT NOT NULL DEFAULT '',
    status                   TEXT NOT NULL DEFAULT 'available'
                             CHECK(status IN ('available','quarantined','deleted')),
    retention_class          TEXT NOT NULL DEFAULT 'hot',
    version                  INTEGER NOT NULL DEFAULT 1,
    created_at               INTEGER NOT NULL,
    deleted_at               INTEGER
);

CREATE TABLE IF NOT EXISTS message_asset_links (
    message_id        TEXT NOT NULL REFERENCES messages(message_id),
    part_id           TEXT NOT NULL REFERENCES content_parts(part_id),
    asset_id          TEXT NOT NULL REFERENCES multimodal_assets(asset_id),
    ordinal           INTEGER NOT NULL DEFAULT 0,
    original_filename TEXT NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL,
    PRIMARY KEY (message_id, part_id),
    UNIQUE(part_id)
);

CREATE TABLE IF NOT EXISTS asset_derivatives (
    derivative_id    TEXT PRIMARY KEY,
    asset_id         TEXT NOT NULL REFERENCES multimodal_assets(asset_id),
    kind             TEXT NOT NULL,
    page_no          INTEGER,
    payload_ref      TEXT NOT NULL REFERENCES payload_objects(payload_ref),
    sha256           TEXT NOT NULL DEFAULT '',
    perceptual_hash  TEXT NOT NULL DEFAULT '',
    metadata_json    TEXT NOT NULL DEFAULT '{}',
    created_at       INTEGER NOT NULL,
    UNIQUE(asset_id, kind, page_no)
);

CREATE TABLE IF NOT EXISTS vision_analyses (
    analysis_id           TEXT PRIMARY KEY,
    asset_id              TEXT NOT NULL REFERENCES multimodal_assets(asset_id),
    analysis_kind         TEXT NOT NULL DEFAULT 'describe',
    model_id              TEXT NOT NULL DEFAULT '',
    prompt_version        TEXT NOT NULL DEFAULT '1',
    result_schema_version TEXT NOT NULL DEFAULT '1',
    options_hash          TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'queued'
                          CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
    short_description     TEXT NOT NULL DEFAULT '',
    detailed_description  TEXT NOT NULL DEFAULT '',
    extracted_text        TEXT NOT NULL DEFAULT '',
    objects_json          TEXT NOT NULL DEFAULT '[]',
    document_type         TEXT NOT NULL DEFAULT '',
    metadata_json         TEXT NOT NULL DEFAULT '{}',
    result_payload_ref    TEXT REFERENCES payload_objects(payload_ref),
    error_category        TEXT NOT NULL DEFAULT '',
    retryable             INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0,1)),
    created_at            INTEGER NOT NULL,
    started_at            INTEGER,
    completed_at          INTEGER,
    UNIQUE(asset_id, analysis_kind, model_id, prompt_version,
           result_schema_version, options_hash)
);

CREATE INDEX IF NOT EXISTS idx_multimodal_assets_phash
    ON multimodal_assets(perceptual_hash) WHERE perceptual_hash <> '';
CREATE INDEX IF NOT EXISTS idx_message_asset_links_asset
    ON message_asset_links(asset_id);
CREATE INDEX IF NOT EXISTS idx_vision_analyses_status
    ON vision_analyses(status, created_at);
CREATE INDEX IF NOT EXISTS idx_vision_analyses_asset
    ON vision_analyses(asset_id, completed_at DESC);

