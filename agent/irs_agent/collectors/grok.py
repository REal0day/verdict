from .base import Collector, register, _home
register(Collector(
    name="grok",
    default_paths=[_home("grok-reports")],
    glob="*.md",
))
