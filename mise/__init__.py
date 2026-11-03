"""Mise — household meal-planning app.

The package is laid out per docs/architecture.md:
  adapters/  — HTTP (FastAPI routers), MCP (tool definitions)
  services/  — LLM-backed (parser, canonical resolver, intent parser)
  domain/    — Pure Python (planner, grocery, pantry, recipe service)
  models/    — SQLModel schemas (no behaviour)
  db.py      — Engine, session factory
  main.py    — FastAPI app, lifespan

The hard rule: domain/ never imports from services/, adapters/, or
LLM clients. Enforced by the ruff config in pyproject.toml.
"""

__version__ = "0.1.0"
