"""Transport adapters.

Adapters translate between a transport (HTTP, MCP, CLI) and the
domain. They contain NO business logic — request shape in, domain call,
response shape out. Anything that smells like a decision belongs in
domain/ or services/.
"""
