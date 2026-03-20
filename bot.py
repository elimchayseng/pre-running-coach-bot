"""
PRE: Running Coach Bot - Core bot logic with mem0 memory
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import HEROKU_MODEL, llm_client, mem0_client

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are PRE, a friendly and knowledgeable running coach bot named after Steve Prefontaine.
You help athletes with their training, race preparation, pacing strategies, and motivation.

Key principles:
- Be encouraging but realistic about training loads
- Consider the athlete's history and context when giving advice
- Provide specific, actionable recommendations
- Ask clarifying questions when needed
- Remember past conversations and build on them

Use the context provided from memory to personalize your responses."""


def get_user_id(update: Update) -> str:
    """Generate consistent user ID from Telegram user."""
    return f"telegram_{update.effective_user.id}"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    welcome_message = (
        f"Hey {user.first_name}! I'm PRE, your running coach bot.\n\n"
        "I can help you with:\n"
        "- Training plans and workout suggestions\n"
        "- Race preparation and pacing strategies\n"
        "- Recovery and injury prevention\n"
        "- Motivation and goal setting\n\n"
        "Just send me a message about your running goals or questions!"
    )
    await update.message.reply_text(welcome_message)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages with mem0 memory integration."""
    user_message = update.message.text
    user_id = get_user_id(update)

    logger.info(f"Message from {user_id}: {user_message[:50]}...")

    try:
        # Search for relevant memories
        memories = mem0_client.search(user_message, user_id=user_id, limit=5)

        # Build context from memories
        memory_context = ""
        if memories:
            memory_texts = [m.get("memory", "") for m in memories if m.get("memory")]
            if memory_texts:
                memory_context = "\n\nRelevant context from previous conversations:\n- " + "\n- ".join(memory_texts)

        # Build messages for LLM
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + memory_context},
            {"role": "user", "content": user_message},
        ]

        # Get response from LLM
        response = llm_client.chat.completions.create(model=HEROKU_MODEL, messages=messages, max_tokens=1024)

        assistant_message = response.choices[0].message.content

        # Store the conversation in memory
        mem0_client.add(
            [{"role": "user", "content": user_message}, {"role": "assistant", "content": assistant_message}],
            user_id=user_id,
        )

        # Send response
        await update.message.reply_text(assistant_message)

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        await update.message.reply_text("Sorry, I encountered an error. Please try again in a moment.")
