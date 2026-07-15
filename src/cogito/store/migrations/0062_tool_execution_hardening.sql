-- 0062: bind Tool execution, Approval and output metadata to immutable payloads.

ALTER TABLE tool_calls ADD COLUMN arguments_ref TEXT NOT NULL DEFAULT '';
ALTER TABLE tool_calls ADD COLUMN result_ref TEXT NOT NULL DEFAULT '';
ALTER TABLE tool_calls ADD COLUMN result_summary TEXT NOT NULL DEFAULT '';
ALTER TABLE tool_calls ADD COLUMN result_trust_label TEXT NOT NULL DEFAULT 'unverified';
ALTER TABLE tool_calls ADD COLUMN result_size_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tool_calls ADD COLUMN constraints_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE approvals ADD COLUMN subject_type TEXT NOT NULL DEFAULT 'tool_call';
ALTER TABLE approvals ADD COLUMN subject_id TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN requester_attempt_id TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN capability_id TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN capability_version TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN arguments_snapshot_ref TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN action_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN requested_permissions TEXT NOT NULL DEFAULT '[]';
ALTER TABLE approvals ADD COLUMN risk_level TEXT NOT NULL DEFAULT 'low';
ALTER TABLE approvals ADD COLUMN policy_version TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN auto_mode_version TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN constraints_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE approvals ADD COLUMN allowed_responder_principal_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE approvals ADD COLUMN response_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE approvals ADD COLUMN responded_at TEXT;
ALTER TABLE approvals ADD COLUMN consumed_at TEXT;

ALTER TABLE tool_processes ADD COLUMN network_id TEXT NOT NULL DEFAULT '';
ALTER TABLE tool_processes ADD COLUMN proxy_id TEXT NOT NULL DEFAULT '';
ALTER TABLE tool_processes ADD COLUMN lease_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE tool_processes ADD COLUMN lease_expires_at TEXT;
ALTER TABLE tool_processes ADD COLUMN heartbeat_at TEXT;
ALTER TABLE tool_processes ADD COLUMN cleanup_error TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_approvals_subject
    ON approvals(subject_type, subject_id, status);
CREATE INDEX IF NOT EXISTS idx_tool_calls_unknown
    ON tool_calls(status, completed_at);
