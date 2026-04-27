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

# Required environment variables
REQUIRED_ENV_VARS = ["HEROKU_INFERENCE_URL", "HEROKU_INFERENCE_KEY"]


def validate_env() -> bool:
    """Validate required environment variables at startup."""
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        return False
    return True


# Mem0 client with custom instructions
CUSTOM_INSTRUCTIONS = """
Extract from marathon training conversations:
- Race goals (target times, race dates, qualifying standards)
- Training metrics (weekly mileage, long run distances, pace targets)
- Physical constraints (injuries, recovery needs, cross-training requirements)
- Preferences (time of day, terrain, weather conditions, gear)
- Progress milestones (PRs, successful workouts, breakthroughs)

Exclude:
- Greetings and small talk
- Filler words and casual acknowledgments
- Hypotheticals unless planning-related
"""

CUSTOM_CATEGORIES = [
    {"goals": "Race targets, time goals, Boston qualifying"},
    {"training": "Weekly plans, workouts, mileage"},
    {"constraints": "Injuries, recovery, limitations"},
    {"preferences": "Schedule, terrain, gear preferences"},
]

HEROKU_MODEL = os.getenv("HEROKU_MODEL", "claude-3-5-sonnet")

if not TESTING:
    # Validate on import
    if not validate_env():
        sys.exit(1)

    from openai import OpenAI

    # Heroku LLM client (OpenAI-compatible)
    llm_client = OpenAI(
        base_url=os.getenv("HEROKU_INFERENCE_URL"),
        api_key=os.getenv("HEROKU_INFERENCE_KEY"),
        timeout=30.0,  # 30 second timeout
    )

    # Mem0 client — kept around until memory_manager.py is deleted (step 6).
    # Optional: returns None if MEM0_API_KEY is unset, so the harness boots
    # without Mem0 credentials.
    if os.getenv("MEM0_API_KEY"):
        from mem0 import MemoryClient
        mem0_client = MemoryClient(api_key=os.getenv("MEM0_API_KEY"))
    else:
        mem0_client = None
else:
    mem0_client = None
    llm_client = None

# Coach personality (condensed for token efficiency)
PRE_PERSONALITY = (
    "PRE is an elite endurance coach: clinical, demanding, uncompromising. "
    "Brutal truth over comfort. Thinks macrocycle → mesocycle → microcycle → today. "
    "Obsessive about biometrics (HRV, HR, RPE, sleep) — uses them to catch "
    "trouble early. Shuts down training when fatigue, pain, or form warrant it."
)
