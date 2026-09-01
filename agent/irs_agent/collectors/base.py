import os
from dataclasses import dataclass


@dataclass
class Collector:
    """A collector is just: a source_tool tag + default watch paths + glob."""
    name: str
    default_paths: list[str]
    glob: str = "*.md"


def _home(*parts) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


REGISTRY: dict[str, Collector] = {}


def register(c: Collector):
    REGISTRY[c.name] = c
    return c
