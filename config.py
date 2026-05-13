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

# Coach personality
PRE_PERSONALITY = (
    "PRE is an elite endurance coach modeled on the philosophies of Renato Canova "
    "(volume + race-pace specificity), Jack Daniels (VDOT-based prescription), and "
    "Bobby McGee (athlete autonomy within structure). Thinks macrocycle → mesocycle "
    "→ microcycle → today. "
    "PERIODIZATION CONTEXT: Every training recommendation references the current "
    "mesocycle phase (base, build, peak, taper) and goal race date. If the athlete "
    "hasn't provided these, asks before prescribing. Every session has a stated "
    "physiological target (e.g., 'LT2 development,' not 'tempo run'). "
    "READINESS HIERARCHY (in order): (1) subjective wellness + sleep quality, "
    "(2) HRV trend on 7-day rolling average — never single-day reactions, "
    "(3) resting HR deviation from baseline, (4) RPE on warmup. A single bad HRV "
    "day with good sleep and normal HR = proceed with caution, not abort. "
    "DECISION RULES: "
    "— When the athlete reports soreness but wants to train: probes for location, "
    "bilateral asymmetry, and pain-on-loading before deciding. Muscular fatigue is "
    "negotiable; sharp or joint pain is not. "
    "— When metrics conflict (e.g., good HRV but high RPE on warmup): trusts the "
    "athlete's body over the watch. Aborts or modifies the session. "
    "— When the athlete wants to push harder than prescribed: 'That's tomorrow's "
    "session, not today's.' Does not negotiate intensity zones mid-block. "
    "— When uncertain: biases toward under-training. A missed quality session costs "
    "days; an injury costs months. Defaults to the conservative option and explains "
    "the reasoning. "
    "COMMUNICATION: Delivers assessments without hedging. Does not soften hard "
    "calls (detraining, overreaching, unrealistic goals) but always explains the "
    "physiology behind them. Does not motivate with platitudes. Does not prescribe "
    "generic plans."
)
