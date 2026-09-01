from .base import Collector, register, _home
register(Collector(
    name="gemini",
    default_paths=[_home("gemini-reports")],
    glob="*.md",
))
