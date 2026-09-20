"""Background workers: lease helpers, scheduler tick, job runner (spec 07).

The composition root (``intel.workers.composition.build_runtime``) registers
ingest/route/extract/event_build/archive_answer/investigate/report_build/
apply_review handlers on a :class:`~intel.workers.runner.JobRunner`.
"""
