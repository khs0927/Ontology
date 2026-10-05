# AEC drawing collection progress

Updated: 2026-10-05 KST. **The full collection and acceleration goal is incomplete.**

This new file is the proposed Windows counterpart of the original plan's
`/workspace/gh-work/PROGRESS.md`. No existing Windows mapping was found. The intended deployment
copy is `C:\CODE\Ontology\PROGRESS.md`; this review copy is in `C:\CODE\Ontology-audit-repairs`.

## Evidence paths

- Original instructions: user attachment `C:\Users\khs09\.codex\attachments\c90c31a0-334d-4ae6-a259-db9a70b4b042\붙여넣은 텍스트.txt`.
- Requirement-by-requirement evidence: `D:\AECData\dev\completion-matrix.md`.
- Timestamped operations: `D:\AECData\dev\speedup-log.md`.
- Independent audit: `D:\AECData\waves\FINDINGS.md` (Wave5 corrections supersede early claims).
- Repair report and backup receipts: `D:\AECData\dev\audit-repairs-result.md`,
  `checkpoint-repair-receipt.json`, `checkpoint-repair-provenance.txt`.
- Proposed observational entry point: `scripts\ops\observe-bulk.ps1 -Once`.
  Default output: `D:\AECData\bulk\logs\stability.jsonl`. Registration and execution are separate
  operations; adding this script does not start monitoring or change any scheduled task.

## Current verified boundary

The repair log records a worker claim at 13:07 KST, followed by source-not-found errors and a DB
connection timeout at 13:20. At 13:24, direct inspection found 0.56 GiB available RAM, no stop marker,
one configured worker, and disabled Reembed/GraphRAG/FinalCheck/FinalWrap tasks. These are timestamped
observations, not current live guarantees. The 13:08 queue count of 21,840 is a historical snapshot.

## Gates remaining

- Maintain at least 2 GB available RAM; the 2048 MB launch guard alone does not prove this.
- Re-establish an uncontaminated baseline and demonstrate improved throughput with stated denominators.
- Complete the 20-document priority comparison with zero failures before increasing workers.
- Record continuous 24-hour DB stability after recovery; any outage starts a new observation window.
- Complete the whole ingestion scope, resolve source failures, and reconcile queue/document counts.
- Restore the required scheduled work after collection; finish reembedding and one GraphRAG refresh.
- Verify isolated backup recovery and independent accuracy; cloud checksum verification is only
  `BACKUP_VERIFIED`, not `RECOVERY_VERIFIED`.

The observer appends host boot time, available RAM, DB container health/start time, queue states,
and last-hour committed ingestion events. Failed/time-limited Docker or SQL observations retain
null counts and explicit errors. Each Docker CLI call is limited to 20 seconds by default, and each
SQL statement to five seconds. Only the observer's own timed-out CLI is terminated. It never changes
containers, tasks, or DB contents. Samples support review but gaps between samples cannot prove
that no outage happened. `ingestion_completed` events can include retries before job finalization;
they are not a historical success/failure ledger.
