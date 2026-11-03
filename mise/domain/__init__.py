"""Domain layer — pure Python. Planner, GroceryBuilder, PantryManager,
RecipeService live here.

Strict rules (enforced by code review + the layered ruff config):
  - No imports from mise.adapters or mise.services
  - No imports from anthropic, openai, httpx, requests
  - All persistence via Session passed in by the adapter
  - Pure functions wherever possible — easier to test exhaustively
"""
