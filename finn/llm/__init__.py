"""LLM access. New code calls `finn.llm.router.call(...)`.

`legacy` is the Step 1-8 gateway over the old SQLite store. It stays only until each old module is ported
onto the Postgres router (Phases 2-4); it is re-exported so `llm_mod.llm(...)` keeps working meanwhile.
"""
from .legacy import LLMUnavailable, available, cache_key, llm, model_for, pool  # noqa: F401
