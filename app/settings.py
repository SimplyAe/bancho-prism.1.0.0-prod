from __future__ import annotations

import tomllib
from typing import Literal
from urllib.parse import quote

from dotenv import load_dotenv
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

from app.settings_utils import read_bool
from app.settings_utils import read_list

load_dotenv()

CaptchaProvider = Literal["recaptcha", "hcaptcha", "turnstile"]


class Settings(BaseSettings):
    """Validated application configuration.

    Every field is read from the environment (or a `.env` file). Unlike a
    bare `os.environ[...]` lookup, a misconfigured deployment fails with a
    single report naming *every* problem at once, rather than a `KeyError`
    for whichever variable happens to be read first at import time.

    NOTE: the module-level constants below are the public interface --
    `app.settings.DOMAIN` and friends. They are populated from an instance
    of this class. They are deliberately plain module globals (and not
    attributes of a shared instance) because some are reassigned at
    runtime; `!debug` toggles `app.settings.DEBUG` in `app/commands.py`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # `.env` values are strings; let validators coerce them.
        str_strip_whitespace=True,
    )

    # required -- the server cannot run without these.
    APP_HOST: str
    APP_PORT: int = Field(ge=1, le=65535)

    DB_HOST: str
    DB_PORT: int = Field(ge=1, le=65535)
    DB_USER: str
    DB_PASS: str
    DB_NAME: str

    REDIS_HOST: str
    REDIS_PORT: int = Field(ge=1, le=65535)
    REDIS_USER: str = ""
    REDIS_PASS: str = ""
    REDIS_DB: int = Field(default=0, ge=0)

    DOMAIN: str
    MIRROR_SEARCH_ENDPOINT: str
    MIRROR_DOWNLOAD_ENDPOINT: str
    COMMAND_PREFIX: str = "!"

    # optional integrations -- absence must degrade, never crash.
    OSU_API_KEY: str | None = None
    DATADOG_API_KEY: str = ""
    DATADOG_APP_KEY: str = ""
    DISCORD_AUDIT_LOG_WEBHOOK: str = ""
    DISCORD_FIRST_PLACE_WEBHOOK: str = ""
    DISCORD_INVITE: str = ""
    SENTRY_DSN: str = ""

    MENU_ICON_URL: str = ""
    MENU_ONCLICK_URL: str = ""
    SEASONAL_BGS_RAW: str = Field(default="", alias="SEASONAL_BGS")

    # behaviour flags.
    DEBUG: bool = False
    REDIRECT_OSU_URLS: bool = True
    DISALLOW_OLD_CLIENTS: bool = True
    DISALLOW_INGAME_REGISTRATION: bool = True
    AUTOMATICALLY_REPORT_PROBLEMS: bool = False
    LOG_WITH_COLORS: bool = False
    DEVELOPER_MODE: bool = False
    WEB_SESSION_COOKIE_SECURE: bool = True

    PP_CACHED_ACCS_RAW: str = Field(default="90,95,98,99,100", alias="PP_CACHED_ACCS")
    DISALLOWED_NAMES_RAW: str = Field(default="", alias="DISALLOWED_NAMES")
    DISALLOWED_PASSWORDS_RAW: str = Field(default="", alias="DISALLOWED_PASSWORDS")

    # captcha verification for web (v2 api) registration.
    CAPTCHA_PROVIDER: CaptchaProvider | None = None
    CAPTCHA_SECRET: str | None = None

    # connection tuning. exposed so operators can adapt to their hardware
    # without editing source.
    HTTP_CONNECT_TIMEOUT: float = Field(default=5.0, gt=0)
    HTTP_READ_TIMEOUT: float = Field(default=15.0, gt=0)
    HTTP_WRITE_TIMEOUT: float = Field(default=15.0, gt=0)
    HTTP_POOL_TIMEOUT: float = Field(default=5.0, gt=0)
    HTTP_MAX_CONNECTIONS: int = Field(default=100, gt=0)
    HTTP_MAX_KEEPALIVE_CONNECTIONS: int = Field(default=20, gt=0)
    HTTP_RETRIES: int = Field(default=2, ge=0)

    REDIS_MAX_CONNECTIONS: int = Field(default=50, gt=0)
    REDIS_SOCKET_TIMEOUT: float = Field(default=5.0, gt=0)
    REDIS_CONNECT_TIMEOUT: float = Field(default=5.0, gt=0)
    REDIS_HEALTH_CHECK_INTERVAL: int = Field(default=30, ge=0)

    # how long to wait for in-flight requests to finish on shutdown.
    SHUTDOWN_DRAIN_TIMEOUT: float = Field(default=15.0, ge=0)

    # startup dependency connection retries.
    STARTUP_CONNECT_ATTEMPTS: int = Field(default=10, gt=0)
    STARTUP_CONNECT_MAX_WAIT: float = Field(default=10.0, gt=0)

    # trusted reverse proxies (CIDR). `X-Forwarded-For` is only honored
    # when the connection comes from one of these; see `IPResolver`.
    TRUSTED_PROXIES: str = Field(default="127.0.0.1,::1", alias="TRUSTED_PROXIES")

    # bearer token required to scrape /metrics. empty = endpoint disabled.
    METRICS_TOKEN: str = ""

    @field_validator(
        "DEBUG",
        "REDIRECT_OSU_URLS",
        "DISALLOW_OLD_CLIENTS",
        "DISALLOW_INGAME_REGISTRATION",
        "AUTOMATICALLY_REPORT_PROBLEMS",
        "LOG_WITH_COLORS",
        "DEVELOPER_MODE",
        "WEB_SESSION_COOKIE_SECURE",
        mode="before",
    )
    @classmethod
    def _coerce_bool(cls, value: object) -> object:
        # preserve the historical parsing ("true"/"1"/"yes"), which is
        # laxer than pydantic's default and already in users' .env files.
        if isinstance(value, str):
            return read_bool(value)
        return value

    @field_validator("CAPTCHA_PROVIDER", "OSU_API_KEY", mode="before")
    @classmethod
    def _empty_string_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


def _format_validation_error(exc: ValidationError) -> str:
    """Render every configuration problem at once.

    The default pydantic output is accurate but noisy; operators need to
    know which environment variables to set, not which python fields failed.
    """
    lines = ["Invalid configuration -- please correct the following:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<config>"
        message = error["msg"]
        if error["type"] == "missing":
            message = "is required but was not set in the environment"
        lines.append(f"  - {location}: {message}")
    lines.append("")
    lines.append("See .env.example for the full list of supported variables.")
    return "\n".join(lines)


def load_settings() -> Settings:
    """Load and validate settings, or raise with an actionable message."""
    try:
        return Settings()
    except ValidationError as exc:
        raise SystemExit(_format_validation_error(exc)) from exc


_settings = load_settings()

# public interface -- module-level constants.
# NOTE: kept as module globals (rather than `settings.FOO`) both for
# backwards compatibility across ~100 call sites and because a few are
# reassigned at runtime (see the class docstring).

APP_HOST = _settings.APP_HOST
APP_PORT = _settings.APP_PORT

DB_HOST = _settings.DB_HOST
DB_PORT = _settings.DB_PORT
DB_USER = _settings.DB_USER
DB_PASS = quote(_settings.DB_PASS)
DB_NAME = _settings.DB_NAME
DB_DSN = f"mysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

REDIS_HOST = _settings.REDIS_HOST
REDIS_PORT = _settings.REDIS_PORT
REDIS_USER = _settings.REDIS_USER
REDIS_PASS = quote(_settings.REDIS_PASS)
REDIS_DB = _settings.REDIS_DB

REDIS_AUTH_STRING = f"{REDIS_USER}:{REDIS_PASS}@" if REDIS_USER and REDIS_PASS else ""
REDIS_DSN = f"redis://{REDIS_AUTH_STRING}{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

OSU_API_KEY = _settings.OSU_API_KEY

DOMAIN = _settings.DOMAIN
MIRROR_SEARCH_ENDPOINT = _settings.MIRROR_SEARCH_ENDPOINT
MIRROR_DOWNLOAD_ENDPOINT = _settings.MIRROR_DOWNLOAD_ENDPOINT

COMMAND_PREFIX = _settings.COMMAND_PREFIX

SEASONAL_BGS = read_list(_settings.SEASONAL_BGS_RAW)

MENU_ICON_URL = _settings.MENU_ICON_URL
MENU_ONCLICK_URL = _settings.MENU_ONCLICK_URL

DATADOG_API_KEY = _settings.DATADOG_API_KEY
DATADOG_APP_KEY = _settings.DATADOG_APP_KEY

DEBUG = _settings.DEBUG
REDIRECT_OSU_URLS = _settings.REDIRECT_OSU_URLS

PP_CACHED_ACCURACIES = [int(acc) for acc in read_list(_settings.PP_CACHED_ACCS_RAW)]

DISALLOWED_NAMES = read_list(_settings.DISALLOWED_NAMES_RAW)
DISALLOWED_PASSWORDS = read_list(_settings.DISALLOWED_PASSWORDS_RAW)
DISALLOW_OLD_CLIENTS = _settings.DISALLOW_OLD_CLIENTS
DISALLOW_INGAME_REGISTRATION = _settings.DISALLOW_INGAME_REGISTRATION

CAPTCHA_PROVIDER = _settings.CAPTCHA_PROVIDER
CAPTCHA_SECRET = _settings.CAPTCHA_SECRET

WEB_SESSION_COOKIE_SECURE = _settings.WEB_SESSION_COOKIE_SECURE

DISCORD_AUDIT_LOG_WEBHOOK = _settings.DISCORD_AUDIT_LOG_WEBHOOK
DISCORD_FIRST_PLACE_WEBHOOK = _settings.DISCORD_FIRST_PLACE_WEBHOOK
DISCORD_INVITE = _settings.DISCORD_INVITE

SENTRY_DSN = _settings.SENTRY_DSN

AUTOMATICALLY_REPORT_PROBLEMS = _settings.AUTOMATICALLY_REPORT_PROBLEMS

LOG_WITH_COLORS = _settings.LOG_WITH_COLORS

# connection tuning
HTTP_CONNECT_TIMEOUT = _settings.HTTP_CONNECT_TIMEOUT
HTTP_READ_TIMEOUT = _settings.HTTP_READ_TIMEOUT
HTTP_WRITE_TIMEOUT = _settings.HTTP_WRITE_TIMEOUT
HTTP_POOL_TIMEOUT = _settings.HTTP_POOL_TIMEOUT
HTTP_MAX_CONNECTIONS = _settings.HTTP_MAX_CONNECTIONS
HTTP_MAX_KEEPALIVE_CONNECTIONS = _settings.HTTP_MAX_KEEPALIVE_CONNECTIONS
HTTP_RETRIES = _settings.HTTP_RETRIES

REDIS_MAX_CONNECTIONS = _settings.REDIS_MAX_CONNECTIONS
REDIS_SOCKET_TIMEOUT = _settings.REDIS_SOCKET_TIMEOUT
REDIS_CONNECT_TIMEOUT = _settings.REDIS_CONNECT_TIMEOUT
REDIS_HEALTH_CHECK_INTERVAL = _settings.REDIS_HEALTH_CHECK_INTERVAL

SHUTDOWN_DRAIN_TIMEOUT = _settings.SHUTDOWN_DRAIN_TIMEOUT
STARTUP_CONNECT_ATTEMPTS = _settings.STARTUP_CONNECT_ATTEMPTS
STARTUP_CONNECT_MAX_WAIT = _settings.STARTUP_CONNECT_MAX_WAIT

TRUSTED_PROXIES = read_list(_settings.TRUSTED_PROXIES)

METRICS_TOKEN = _settings.METRICS_TOKEN

# advanced dev settings

## WARNING touch this once you've
##          read through what it enables.
##          you could put your server at risk.
DEVELOPER_MODE = _settings.DEVELOPER_MODE

with open("pyproject.toml", "rb") as f:
    # NOTE: this is the *migration lineage* version. It drives the SQL migration
    # runner (`run_sql_migrations` in `app/state/services.py`), which parses it
    # with `int(...)` per component and only applies `# vX.Y.Z` blocks where
    # `last_run < block <= VERSION`. It therefore has to stay a strictly
    # increasing numeric M.N.P that continues bancho.py's 5.3.x lineage, so an
    # in-place upgrade from an existing bancho.py database still applies the
    # Prism migrations. It is NOT the product version -- see `PRISM_VERSION`.
    VERSION = tomllib.load(f)["project"]["version"]

# Prism's product/release version -- the human-facing brand ("bancho-prism").
# Deliberately decoupled from `VERSION` above: the migration runner needs a
# numeric lineage it can compare, while releases are branded independently and
# may carry a non-numeric suffix ("-prod"). Surfaced in the API version header
# and logs; never parsed as a migration version.
PRISM_VERSION = "1.0.0-prod"

