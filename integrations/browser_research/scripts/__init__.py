"""Per-platform browser research scripts.

Each script exports a single dispatch contract: handler(action, **kwargs) -> dict.
Returned dict ALWAYS includes 'connection_mechanism' for transparency to the
agent / user.
"""
