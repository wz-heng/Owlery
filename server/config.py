import logging
import os

from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
)

logger = logging.getLogger(__name__)

# The app was named Octopus before it became Owlery (docs/plans/rename-owlery.md).
# Both prefixes are still honored — see `settings_customise_sources` — so an
# existing deployment's `.env` / systemd EnvironmentFile keeps working. This
# matters most for `auth_token`: `server.crypto` derives the credential
# encryption key from its VALUE, so silently falling back to the "changeme"
# default would make every stored secret undecryptable.
_ENV_PREFIX = "OWLERY_"
_LEGACY_ENV_PREFIX = "OCTOPUS_"


class Settings(BaseSettings):
    auth_token: str = "changeme"
    host: str = "0.0.0.0"
    port: int = 8000
    default_working_dir: str = "."
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8000"]
    db_path: str = "owlery.db"

    # Root of all durable app state. Every directory below defaults to a
    # subdirectory of it; set `OWLERY_HOME_DIR` to relocate the whole tree
    # (tests and e2e point it at a throwaway temp dir). `~` is expanded at use
    # time (not config-load time) so tests that override $HOME via monkeypatch
    # see the override.
    home_dir: str = "~/.owlery"
    # Pre-rename location of `home_dir`. On startup, if it exists and
    # `home_dir` does not, the whole tree is moved across and the absolute
    # paths the DB stores into it are rewritten (server/legacy_rename.py).
    # Set to "" to disable the migration entirely.
    legacy_home_dir: str = "~/.octopus"

    # The six state dirs. Empty means "derive from home_dir" — the validator
    # below fills them in, so `settings.attachments_dir` is always a real path.
    # Each is independently overridable, which is how tests keep their state
    # out of the developer's real home.
    #
    # attachments_dir: user-uploaded attachments, one subdir per session_id.
    attachments_dir: str = ""
    # large_prompts_dir: per-session spill directory for prompts too large to
    # deliver as positional argv (the kernel's MAX_ARG_STRLEN ceiling is
    # ~128 KB). When a prompt exceeds the threshold, Owlery writes it to a file
    # under this root and sends the backend a small pointer message instructing
    # the model to Read the file. See server/large_prompts.py.
    large_prompts_dir: str = ""
    # codex_home_dir: per-credential CODEX_HOME root (codex-backend.md §7,
    # option B). Each in-app Codex login gets `<codex_home_dir>/<credential_id>/`,
    # which holds the `auth.json` Codex writes + manages.
    codex_home_dir: str = ""
    # agents_dir: per-agent durable state root (docs/plans/memory.md §2). Each
    # agent gets `<agents_dir>/<agent_id>/memory/` — the canonical native,
    # agent-written markdown memory both harnesses point at (Claude via
    # CLAUDE_COWORK_MEMORY_PATH_OVERRIDE, Codex via its instructions blurb).
    agents_dir: str = ""
    # fork_dir: holds every `/fork` working-dir copy (session-fork.md).
    fork_dir: str = ""
    # research_dir: completed deep-research reports (native-deep-research.md).
    research_dir: str = ""

    # Dev mode (enables uvicorn reload)
    debug: bool = False

    # Cloudflare Tunnel (opt-in)
    enable_tunnel: bool = False

    # Bridge configuration (opt-in)
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: list[str] = []
    telegram_api_base_url: str = "https://api.telegram.org"

    # If an AskUserQuestion goes unanswered for this long, the server
    # synthesizes an "act autonomously" reply so the session doesn't
    # wedge forever (matters most for bridge/scheduled-task sessions
    # where no human will ever see the prompt). 0 disables auto-answer.
    ask_user_question_timeout_seconds: int = 1800

    # Per-turn safety watchdog (turn-safety.md §3). A turn that emits no harness
    # event for `turn_idle_timeout_seconds`, or that runs longer than
    # `turn_max_seconds` overall, is stopped and surfaced as an error instead of
    # hanging forever (the deep-research wedge). Idle is generous — a single web
    # sub-turn can legitimately take tens of seconds. 0 disables that check.
    turn_idle_timeout_seconds: int = 300
    turn_max_seconds: int = 1800

    # Native deep research (native-deep-research.md §6/§8). Max jobs running
    # concurrently across all sessions (per-job leaf fan-out is bounded inside
    # the pipeline); and a hard per-job wall-clock cap.
    research_max_concurrent_jobs: int = 2
    research_job_timeout_seconds: int = 1200

    # Connectors (connectors.md §7). The public base URL is what connector
    # OAuth redirect URIs are built against; behind a tunnel it must be set
    # to the stable public host. Unset → computed as http://127.0.0.1:{port}.
    # Per-kind client credentials gate availability in the catalog: a kind is
    # only installable once both its id and secret are present.
    public_base_url: str | None = None
    gmail_oauth_client_id: str | None = None
    gmail_oauth_client_secret: str | None = None
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None

    model_config = {"env_prefix": _ENV_PREFIX, "env_file": ".env", "extra": "ignore"}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read `OWLERY_*` first, then fall back to the pre-rename `OCTOPUS_*`
        names. Sources are tried in order, so the legacy pair only supplies a
        field that neither the new env var nor the new `.env` key set."""
        legacy_env = EnvSettingsSource(settings_cls, env_prefix=_LEGACY_ENV_PREFIX)
        legacy_dotenv = DotEnvSettingsSource(
            settings_cls,
            env_file=settings_cls.model_config.get("env_file"),
            env_prefix=_LEGACY_ENV_PREFIX,
        )
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            legacy_env,
            legacy_dotenv,
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _derive_state_dirs(self) -> "Settings":
        """Fill the unset state dirs from `home_dir`. Done here rather than with
        literal defaults so that overriding `home_dir` alone relocates the whole
        tree — which is what makes the legacy migration safe to key off a single
        setting, and what lets tests isolate their state with one env var."""
        home = self.home_dir.rstrip("/") or "~/.owlery"
        for field, leaf in (
            ("attachments_dir", "attachments"),
            ("large_prompts_dir", "large-prompts"),
            ("codex_home_dir", "codex"),
            ("agents_dir", "agents"),
            ("fork_dir", "fork"),
            ("research_dir", "research"),
        ):
            if not getattr(self, field):
                object.__setattr__(self, field, f"{home}/{leaf}")
        return self

    @property
    def resolved_public_base_url(self) -> str:
        """Base URL for OAuth redirect URIs — explicit config or localhost."""
        return self.public_base_url or f"http://127.0.0.1:{self.port}"

    @property
    def resolved_home_dir(self) -> str:
        """`home_dir` with `~` expanded — the root of all durable app state."""
        return os.path.expanduser(self.home_dir)

    @property
    def resolved_fork_dir(self) -> str:
        """`fork_dir` with `~` expanded. Expansion happens HERE, not at config
        load, so a test that monkeypatches $HOME relocates the tree."""
        return os.path.expanduser(self.fork_dir)

    @property
    def resolved_research_dir(self) -> str:
        """`research_dir` with `~` expanded — see `resolved_fork_dir`."""
        return os.path.expanduser(self.research_dir)


settings = Settings()


def _warn_on_legacy_env() -> None:
    """One deprecation line naming the legacy vars that are still doing work."""
    legacy = sorted(
        k
        for k in os.environ
        if k.startswith(_LEGACY_ENV_PREFIX)
        and f"{_ENV_PREFIX}{k[len(_LEGACY_ENV_PREFIX):]}" not in os.environ
    )
    if legacy:
        logger.warning(
            "Reading deprecated %s* environment variables: %s. Rename them to "
            "%s* — the legacy names will be dropped in a future release.",
            _LEGACY_ENV_PREFIX,
            ", ".join(legacy),
            _ENV_PREFIX,
        )


_warn_on_legacy_env()
