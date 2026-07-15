-- Durable, exact idempotency receipts for Commands issued by Agent Tools.

CREATE TABLE IF NOT EXISTS agent_tool_command_results (
    command_name    TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    action_hash     TEXT NOT NULL,
    actor_id        TEXT NOT NULL,
    aggregate_id    TEXT NOT NULL,
    result_json     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    PRIMARY KEY(command_name, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_tool_command_results_actor
    ON agent_tool_command_results(actor_id, created_at);
