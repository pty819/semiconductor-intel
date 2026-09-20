"""NOOA adapter layer (spec 06 / 14 / 15 / 16; Task 11).

Everything in this package is the boundary between the intel application
and the pinned NOOA framework:

- :mod:`intel.nooa_adapter.agents` — the seven Agent classes (doc 06 §4)
  with tiered route resolution (L1/L2/L3 registry aliases);
- :mod:`intel.nooa_adapter.factory` — composition helpers: route registry
  loading, investigation strategy (sandboxed CodeAct / CodeActV2
  candidate), TokenBudgetSummarizer wiring;
- :mod:`intel.nooa_adapter.middleware` — the three D16 guardrails
  (agent_call / llm_call / execute_python);
- :mod:`intel.nooa_adapter.gateway` — scoped-token tool gateway for the
  investigation agent's five tools;
- :mod:`intel.nooa_adapter.tracing` — per-job tracing sessions and
  generation_runs provenance helpers.
"""
