# proactive-candidate-quality-stats

Read-only scan of DB proactive_candidates and the existing delivery pipeline quality.
Produces a candidate_draft that can be projected into ProactiveCandidate(origin=drift,
topic=candidate.quality) for down-stream evaluation. No model calls, no network,
no outbound side effects.

Steps:
0. read proactive_policies current (dry_run etc)
1. query proactive_candidates counts grouped by status/origin
2. query recent delivery pipeline quality (success/failure ratio) if any
3. summarize into 1 item with kind=candidate.quality and a candidate_draft
