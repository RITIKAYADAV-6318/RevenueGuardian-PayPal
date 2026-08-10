"""Centralized settings and .env loading for Revenue Guardian."""
import logging
import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ConfigSettings")
INSECURE_DEFAULT_JWT_SECRET = "INSECURE_DEFAULT_REVOPS_SECRET_KEY_CHANGE_THIS_IN_PRODUCTION"

def load_dotenv(dotenv_path: str = ".env") -> None:
    path = Path(dotenv_path)
    if not path.exists():
        logger.info("No %s file found. Using system environment variables.", dotenv_path)
        return
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

load_dotenv()

def _get_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value in (None, ""):
        return default
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, raw_value, default)
        return default

def _get_float_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value in (None, ""):
        return default
    try:
        return float(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r. Falling back to %s.", name, raw_value, default)
        return default

def sqlite_path_from_database_url(database_url: str) -> str:
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        return database_url[len(prefix):] or "crm.db"
    logger.warning("Only sqlite:/// DATABASE_URL values are supported locally. Using crm.db.")
    return "crm.db"

class Settings(BaseModel):
    gemini_api_key: str = Field(default="")
    slack_webhook_url: Optional[str] = Field(default=None)
    database_url: str = Field(default="sqlite:///crm.db")
    jwt_secret_key: str = Field(default=INSECURE_DEFAULT_JWT_SECRET)
    jwt_algorithm: str = Field(default="HS256")
    access_token_expire_minutes: int = Field(default=60)
    model_name: str = Field(default="gemini-2.0-flash")
    temperature: float = Field(default=0.2)
    analysis_date: str = Field(default="2026-06-30")
    use_live_adk: bool = Field(default=False)

    @property
    def sqlite_db_path(self) -> str:
        return sqlite_path_from_database_url(self.database_url)

    def validate_security(self) -> None:
        if not self.gemini_api_key:
            logger.warning("GEMINI_API_KEY is not set. Live ADK/Gemini execution is disabled unless USE_LIVE_ADK=true.")
        if self.jwt_secret_key == INSECURE_DEFAULT_JWT_SECRET:
            logger.warning("Using the default JWT_SECRET_KEY. This is acceptable for local demo only; change it for deployment.")

def get_settings() -> Settings:
    settings = Settings(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL") or None,
        database_url=os.environ.get("DATABASE_URL", "sqlite:///crm.db"),
        jwt_secret_key=os.environ.get("JWT_SECRET_KEY", INSECURE_DEFAULT_JWT_SECRET),
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
        access_token_expire_minutes=_get_int_env("ACCESS_TOKEN_EXPIRE_MINUTES", 60),
        model_name=os.environ.get("GEMINI_MODEL_NAME", "gemini-2.0-flash"),
        temperature=_get_float_env("GEMINI_TEMPERATURE", 0.2),
        analysis_date=os.environ.get("ANALYSIS_DATE", "2026-06-30"),
        use_live_adk=os.environ.get("USE_LIVE_ADK", "false").lower() in {"1", "true", "yes", "on"},
    )
    settings.validate_security()
    return settings

settings = get_settings()
