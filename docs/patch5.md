# Gotenx v1.5 — Transactional persistence and strict state validation

This patch closes the remaining production-safety gaps tracked by issues #8 and #12.

## Persistence guarantees

- Run ids are reserved atomically under `.gotenx/run-reservations/`.
- Reservations record the owning process; another process cannot publish into them.
- A run is written to a hidden temporary directory and atomically renamed into
  `.gotenx/runs/<run_id>/` only after every artifact is complete.
- Readers ignore reservations and hidden temporary directories.
- Unpublished reservations owned by a normally exiting process are released at exit.
  Reservations and temporary directories older than 24 hours are removed during
  the next run-id allocation after abrupt termination.
- Usage-ledger read/modify/write operations are serialized with `flock` on Linux
  and macOS.
- Policy, baseline, migration, checkpoint, ledger, and other JSON files use
  fsync-plus-replace atomic writes.

## Validation guarantees

- Persisted JSON is size-bounded and parsed with non-finite values disabled.
- Objects, lists, strings, booleans, integers, finite numbers, timestamps, costs,
  and collection sizes are checked explicitly at their boundaries.
- Policy, baseline, adapters, usage ledger, proposals, benchmark manifests and
  checkpoints, replay cases, faithfulness fixtures, model items, and run artifacts
  are validated before use or publication.
- Boolean values are never accepted as numeric values.
- Corrupt state produces a stable JSON error containing `code`, `field`, `path`,
  `message`, and a repair hint. CLI commands return non-zero without a traceback.
- Invalid accounting entries are rejected as a whole; valid unknown fields are
  preserved when the ledger is rewritten.

## Compatibility

- Existing policy v2 migration and policy v3 files remain supported.
- Existing benchmark result files without grader usage remain reportable; missing
  historical grader cost is treated as zero, matching the prior schema.
- Existing `save_run()` callers that supply their own run id remain supported.
