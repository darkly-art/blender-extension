"""Exception-contained step running for runtime teardown - stdlib only, no `bpy`.

Stopping the stream is a sequence of independent releases (timer, handlers,
helper subprocess, GPU resources). If one raises and the rest are skipped, the
process is left in the worst state of all: partially alive, with the port still
held and no UI path to recover. `run_guarded` makes the fix structural rather
than a per-call-site discipline: every step runs no matter what the earlier
ones did.
"""


def run_guarded(steps, log):
    """Run `(name, fn)` steps in order; EVERY step runs even when earlier ones
    raise. Each failure is logged via `log.exception` and collected; returns a
    list of `(name, exception)` pairs, empty when all steps succeeded."""
    failures = []
    for name, fn in steps:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - containment is the point
            log.exception("step %r failed", name)
            failures.append((name, exc))
    return failures
