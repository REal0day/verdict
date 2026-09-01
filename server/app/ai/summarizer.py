import logging
from .base import get_provider

log = logging.getLogger(__name__)

_SUMMARY_SYSTEM = (
    "You are a concise technical writer. You receive markdown reports produced "
    "by AI coding assistants (Claude Code, etc.). Produce a short summary "
    "(<= 6 bullet points) capturing: what was attempted, key changes, results, "
    "errors/risks, and suggested next steps."
)


def summarize(
    text: str, provider_name: str | None = None, model: str | None = None
) -> str | None:
    try:
        provider = get_provider(provider_name, model)
        return provider.chat(
            _SUMMARY_SYSTEM,
            [{"role": "user", "content": text[:100_000]}],
            max_tokens=2048,
        )
    except Exception as e:  # never block ingest on summarizer failure
        log.warning("summarize failed: %s", e)
        return None
