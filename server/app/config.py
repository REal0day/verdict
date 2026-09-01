from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # core
    secret_key: str = "dev-secret"
    encryption_key: str = ""  # urlsafe-b64 32 bytes
    access_token_expire_min: int = 720
    database_url: str = "sqlite:///./irs.sqlite3"

    # bootstrap admin
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "admin"

    # self-service registration (no email verification — domain allowlist only)
    signup_enabled: bool = False
    # comma-separated list of allowed email domains (case-insensitive, no @)
    signup_allowed_domains: str = "example.com"

    # disk-backed blob storage (session uploads etc.)
    data_dir: str = "/data"
    session_upload_max_total_bytes: int = 5 * 1024 * 1024 * 1024  # 5 GiB
    session_upload_max_files: int = 20000

    # folder-import staging dir (one subdir per FolderImport, cleaned on confirm/cancel)
    imports_staging_dir: str = "/data/imports"
    # hard cap per upload to stop accidental DoS via huge dir picks.
    # Sized for real source trees (which routinely exceed a few thousand files);
    # override via IRS_IMPORTS_MAX_FILES / IRS_IMPORTS_MAX_TOTAL_BYTES if needed.
    imports_max_total_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB
    imports_max_files: int = 50000

    # AI
    default_ai_provider: str = "anthropic"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-4-5"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-1.5-pro"
    xai_api_key: str | None = None
    xai_model: str = "grok-2"

    model_config = SettingsConfigDict(env_prefix="IRS_", env_file=".env", extra="ignore")


# Second settings class without prefix for provider keys (ANTHROPIC_API_KEY etc.)
class ProviderKeys(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-4-5"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-1.5-pro"
    xai_api_key: str | None = None
    xai_model: str = "grok-2"


settings = Settings()
provider_keys = ProviderKeys()
