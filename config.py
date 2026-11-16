import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _csv_ints(value: str) -> set[int]:
    return {int(item.strip()) for item in value.split(",") if item.strip()}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    owner_ids: set[int]
    timezone: str
    log_level: str
    public_channel_url: str
    premium_upi_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        owners = _csv_ints(os.getenv("OWNER_IDS", "1844994992"))
        if not owners:
            raise RuntimeError("Set at least one OWNER_IDS Telegram numeric ID")
        return cls(token, database_url, owners,
                   os.getenv("TIMEZONE", "Asia/Kolkata"),
                   os.getenv("LOG_LEVEL", "INFO").upper(),
                   os.getenv("PUBLIC_CHANNEL_URL", "").strip(),
                   os.getenv("PREMIUM_UPI_ID", "").strip())


settings = Settings.from_env()
