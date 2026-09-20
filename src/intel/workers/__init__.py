"""Background workers: lease helpers, scheduler tick, job runner (spec 07).

Task 6 provides the durable-queue core. Handlers for the individual job
kinds (discover/fetch/parse/index/route/...) are Tasks 7+; the runner ships
with a no-op default handler so unregistered kinds complete observably.
"""
