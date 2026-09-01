from .base import Collector, register, _home
register(Collector(
    name="openai",
    default_paths=[_home("openai-reports")],
    glob="*.md",
))
