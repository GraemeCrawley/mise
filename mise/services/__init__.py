"""LLM-backed services.

This is the ONLY directory in the package where the Anthropic / OpenAI
clients may be imported (enforced via ruff banned-api in pyproject.toml).
Per docs/llm-boundaries.md, the five legitimate call sites are:

  1. ingredient_parser.parse_line()
  2. canonical_resolver.resolve()
  3. recipe_importer.from_url()        (future)
  4. intent_parser.parse_*()           (Phase 3, MCP boundary)
  5. suggester (Phase 3, returns proposals, never writes)

Domain code MUST NOT import from here in a way that would block on a
network call. Services return data; domain operates on it.
"""
