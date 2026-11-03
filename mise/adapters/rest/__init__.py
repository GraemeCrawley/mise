"""REST adapter routers. One module per resource family. Each router
takes a `Session` dependency and calls into mise.domain / mise.services
— never directly into mise.models for persistence."""
