import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(base_url=os.getenv("HEROKU_INFERENCE_URL"), api_key=os.getenv("HEROKU_INFERENCE_KEY"))

print("Testing Heroku inference connection...")
response = client.chat.completions.create(
    model=os.getenv("HEROKU_MODEL"),
    messages=[{"role": "user", "content": "Say 'Hello runner!' in exactly 3 words."}],
    max_tokens=20,
)
print(f"Response: {response.choices[0].message.content}")
print("Heroku LLM connection successful!")
