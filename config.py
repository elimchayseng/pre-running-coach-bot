import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("pre_coach")

# Quiet noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Testing mode: skip env validation and real client initialization
TESTING = os.getenv("TESTING", "").lower() in ("1", "true")

REQUIRED_ENV_VARS = ["HEROKU_INFERENCE_URL", "HEROKU_INFERENCE_KEY"]


def validate_env() -> bool:
    """Validate required environment variables at startup."""
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        return False
    return True


HEROKU_MODEL = os.getenv("HEROKU_MODEL", "claude-sonnet-4-6")

if not TESTING:
    if not validate_env():
        sys.exit(1)

    from openai import OpenAI

    # Heroku LLM client (OpenAI-compatible)
    llm_client = OpenAI(
        base_url=os.getenv("HEROKU_INFERENCE_URL"),
        api_key=os.getenv("HEROKU_INFERENCE_KEY"),
        timeout=30.0,
    )
else:
    llm_client = None

# Coach personality (condensed for token efficiency)
PRE_PERSONALITY = (
    "PRE is an elite endurance coach: clinical, demanding, uncompromising. "
    "Brutal truth over comfort. Thinks macrocycle → mesocycle → microcycle → today. "
    "Obsessive about biometrics (HRV, HR, RPE, sleep) — uses them to catch "
    "trouble early. Shuts down training when fatigue, pain, or form warrant it."
)
