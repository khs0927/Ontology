# Restore acceptance hardening — 2026-10-04

Isolated branch: codex/drive-handoff-20261004. Base: 7b138139.

Changes: require successful pg_restore exit, compare full index definitions and readiness/validity, compare constraints and extensions, reject existing scratch DB names, and scope temporary container dump names by process. Failed restore stderr remains available for review.

Validation: six PowerShell integration tests pass. Tests replace Docker with an in-process fake and do not touch running containers/databases. They cover silent restore failure despite equal row counts, changed index definition, constraint mismatch, invalid indexes even when both sides match, scratch collision, and successful matching restore.

Runtime verification is PENDING. Another agent currently has a live pg_dump process (container PID 16752 observed); do not run a second backup/drill or restart Docker until its migration completes.

Full Drive snapshot and handoff publication is still PENDING; this branch does not claim those features complete.
