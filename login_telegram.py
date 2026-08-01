import os
from dotenv import load_dotenv
from telethon import TelegramClient

# Load environment variables from .env file
load_dotenv()

# Clean up environment variables (strip outer quotes, newlines, and trailing spaces)
for key, value in list(os.environ.items()):
    if value:
        os.environ[key] = value.strip().strip('"').strip("'").strip()


api_id_str = os.getenv("TELEGRAM_API_ID")
api_hash = os.getenv("TELEGRAM_API_HASH")
phone = os.getenv("TELEGRAM_PHONE")

if not api_id_str or not api_hash or not phone:
    print("Error: TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_PHONE must be configured in .env")
    exit(1)

try:
    api_id = int(api_id_str)
except ValueError:
    print(f"Error: TELEGRAM_API_ID must be an integer, got: {api_id_str}")
    exit(1)

print(f"Initializing TelegramClient for: {phone}")
client = TelegramClient('groq_userbot_session', api_id, api_hash)

async def main():
    # start() will prompt for the verification code (and password if enabled) in the console
    await client.start(phone=phone)
    if await client.is_user_authorized():
        print("\n============================================================")
        print("SUCCESS: Telegram session authorized successfully!")
        print("The session has been saved to 'groq_userbot_session.session'.")
        me = await client.get_me()
        print(f"Logged in as: {me.first_name} {me.last_name or ''} (@{me.username or 'N/A'}, ID: {me.id})")
        print("============================================================\n")
    else:
        print("Failed to authorize the session.")

if __name__ == "__main__":
    import asyncio
    try:
        # Run the client within the asyncio event loop
        client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
    except Exception as e:
        print(f"\nAn error occurred during login: {e}")
