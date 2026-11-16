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
    log_channel_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "9089475048:AAEsyXJ-EEnCTibbYEWoMn8K9cYfzqOC5GY").strip()
        database_url = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_zpfEPnlk5N2y@ep-nameless-sunset-azpqdzr2-pooler.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        owners = _csv_ints(os.getenv("OWNER_IDS", "1844994992"))
        if not owners:
            raise RuntimeError("Set at least one OWNER_IDS Telegram numeric ID")
        raw_log_setting = os.getenv("LOG_LEVEL", "INFO").strip()
        # Backward compatibility: a negative Telegram channel ID in LOG_LEVEL
        # is treated as the private audit-log destination.
        log_channel_id = raw_log_setting if raw_log_setting.startswith("-") and raw_log_setting[1:].isdigit() else os.getenv("LOG_CHANNEL_ID", "").strip()
        log_level = "INFO" if log_channel_id and raw_log_setting == log_channel_id else raw_log_setting.upper()
        return cls(token, database_url, owners,
                   os.getenv("TIMEZONE", "Asia/Kolkata"),
                   log_level,
                   os.getenv("PUBLIC_CHANNEL_URL", "").strip(),
                   os.getenv("PREMIUM_UPI_ID", "premium-test@invalid").strip(),
                   log_channel_id)


settings = Settings.from_env()
