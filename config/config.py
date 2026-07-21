import os
from dotenv import load_dotenv

load_dotenv()

def get_env(key: str, required: bool = True, default=None):
    value = os.getenv(key, default)
    if required and not value:
        raise RuntimeError(f"❌ Missing required environment variable: {key}")
    return value

class Settings:
    ADMIN_SECRET_KEY: str = get_env("ADMIN_SECRET_KEY")
    SF_REFRESH_TOKEN: str = get_env("SF_REFRESH_TOKEN")
    SF_INSTANCE_URL: str = get_env("SF_INSTANCE_URL")
    SF_CLIENT_ID: str = get_env("SF_CLIENT_ID")
    SF_CLIENT_SECRET: str = get_env("SF_CLIENT_SECRET")
    ELEVEN_LABS_KEY: str = get_env("ELEVEN_LABS_KEY")
    ELEVEN_AGENT_ID: str = get_env("ELEVEN_AGENT_ID")
    ANTHROPIC_API_KEY: str = get_env("ANTHROPIC_API_KEY")
    ANTHROPIC_API_KEY_RUBRIC: str = get_env("ANTHROPIC_API_KEY_RUBRIC", required=False, default=None)
    ELEVENLABS_URL = "https://api.elevenlabs.io/v1/convai/twilio/outbound-call"
    ALAB_SPREADSHEET_NAME="ALab (dialforce) - Nationwide High Intent First only- 03.09.26"
    ALAB_WORKSHEET_NAME="ALab (dialforce) - Nationwide High Intent First only- 03.09.26"
    MAPPER_SHEET_NAME="Sellers First AGENTS"
    MAPPER_WORKSHEET_NAME="BOT area codes (LIVE)"

settings = Settings()

ADMIN_SECRET_KEY = settings.ADMIN_SECRET_KEY
SF_REFRESH_TOKEN = settings.SF_REFRESH_TOKEN
SF_INSTANCE_URL = settings.SF_INSTANCE_URL
SF_CLIENT_ID = settings.SF_CLIENT_ID
SF_CLIENT_SECRET = settings.SF_CLIENT_SECRET
ELEVEN_LABS_KEY = settings.ELEVEN_LABS_KEY
ELEVEN_AGENT_ID = settings.ELEVEN_AGENT_ID
ELEVENLABS_URL = settings.ELEVENLABS_URL
ALAB_SPREADSHEET_NAME = settings.ALAB_SPREADSHEET_NAME
ALAB_WORKSHEET_NAME = settings.ALAB_WORKSHEET_NAME
MAPPER_SHEET_NAME = settings.MAPPER_SHEET_NAME
MAPPER_WORKSHEET_NAME = settings.MAPPER_WORKSHEET_NAME
ALAB_SPREADSHEET_NAME = settings.ALAB_SPREADSHEET_NAME
ALAB_WORKSHEET_NAME = settings.ALAB_WORKSHEET_NAME
MAPPER_SHEET_NAME = settings.MAPPER_SHEET_NAME
MAPPER_WORKSHEET_NAME = settings.MAPPER_WORKSHEET_NAME
ANTHROPIC_API_KEY = settings.ANTHROPIC_API_KEY
ANTHROPIC_API_KEY_RUBRIC = settings.ANTHROPIC_API_KEY_RUBRIC or settings.ANTHROPIC_API_KEY
# DEFAULT_PHONE = 'phnum_7801k60w0n6vecav04d878ej4g7x'
DEFAULT_PHONE = 'phnum_3401knvxn7egetn9vs54vkc4b4hp'
