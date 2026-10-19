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

    # Groq
    groq_api_key: str = ""

    # Behaviour
    max_files_per_review: int = 50   # Skip files beyond this count
    max_patch_chars: int = 80_000    # Truncate total diff beyond this
    log_level: str = "INFO"

    # Files/extensions to never review
    ignored_patterns: list[str] = [
        # Dependency lock files
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "Pipfile.lock",
        "Gemfile.lock",
        "composer.lock",
        "Cargo.lock",
        "go.sum",
        "*.lock",

        # Minified / bundled assets
        "*.min.js",
        "*.min.css",
        "*.bundle.js",
        "*.chunk.js",

        # Source maps
        "*.map",

        # Generated / auto code
        "*.generated.*",
        "*.auto.*",
        "*.g.dart",
        "*.pb.go",
        "*.pb.swift",
        "*.pb.cc",
        "*.pb.h",
        "*.pb",

        # Build output directories
        "dist/",
        "build/",
        "out/",
        ".next/",
        ".nuxt/",
        ".svelte-kit/",
        "target/",
        "bin/",
        "obj/",
        "__pycache__/",

        # Dependency directories
        "node_modules/",
        "vendor/",
        ".venv/",
        "venv/",
        "env/",

        # Compiled bytecode
        "*.pyc",
        "*.pyo",
        "*.class",
        "*.o",
        "*.a",
        "*.so",
        "*.dll",
        "*.exe",

        # Database migrations (auto-generated)
        "migrations/",
        "alembic/versions/",

        # Test snapshots and fixtures
        "*.snap",
        "__snapshots__/",

        # Documentation and config that doesn't need code review
        "*.md",
        "*.rst",
        "*.txt",
        "*.csv",
        "*.json",
        "*.yaml",
        "*.yml",
        "*.toml",
        "*.ini",
        "*.cfg",
        "*.env*",

        # Assets
        "*.svg",
        "*.png",
        "*.jpg",
        "*.jpeg",
        "*.gif",
        "*.ico",
        "*.woff",
        "*.woff2",
        "*.ttf",
        "*.eot",
    ]

    def is_configured(self) -> bool:
        """Return True only when all required credentials are present."""
        return bool(
            self.github_app_id
            and self.github_private_key
            and self.github_webhook_secret
            and self.groq_api_key
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
