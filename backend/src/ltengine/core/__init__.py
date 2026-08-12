"""ltengine.core — transport-free translation logic.

Importable without Litestar (or any server dependency) installed: prompt
building, language registry + detection, output-token budgeting, formatting
post-processing, and the LLM upstream error taxonomy. See
``tests/unit/test_core_isolation.py`` for the guard that keeps it that way.
"""
