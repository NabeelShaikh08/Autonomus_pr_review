from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Unknown env vars are fine — don't error on them
        extra="ignore",
    )

    # GitHub App credentials
    github_app_id: str = ""
    github_private_key: str = ""     # Full PEM string (newlines as \n)
    github_webhook_secret: str = ""

    # Anthropic
    anthropic_api_key: str = ""

    # Behaviour
    max_files_per_review: int = 50   # Skip files beyond this count
    max_patch_chars: int = 80_000    # Truncate total diff beyond this
    log_level: str = "INFO"

    # Files/extensions to never review
    ignored_patterns: list[str] = [
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "Pipfile.lock",
        "*.min.js",
        "*.min.css",
        "*.map",
        "dist/",
        "build/",
        "vendor/",
        "node_modules/",
        "__pycache__/",
        "*.pb",
        "*.pyc",
        "migrations/",
        "*.generated.*",
        "*.auto.*",
    ]

    def is_configured(self) -> bool:
        """Return True only when all required credentials are present."""
        return bool(
            self.github_app_id
            and self.github_private_key
            and self.github_webhook_secret
            and self.anthropic_api_key
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
