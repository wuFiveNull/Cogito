ALTER TABLE proactive_candidates ADD COLUMN critical_override INTEGER NOT NULL DEFAULT 0
    CHECK(critical_override IN (0,1));
