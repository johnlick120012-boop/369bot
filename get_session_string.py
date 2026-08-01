import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

# Load environment variables
load_dotenv()

# Clean up environment variables (strip outer quotes, newlines, and trailing spaces)
for key, value in list(os.environ.items()):
    if value:
        os.environ[key] = value.strip().strip('"').strip("'").strip()


api_id_str = os.getenv("TELEGRAM_API_ID")
api_hash = os.getenv("TELEGRAM_API_HASH")

if not api_id_str or not api_hash:
    print("Error: TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured in .env")
    exit(1)

try:
    api_id = int(api_id_str)
except ValueError:
    print(f"Error: TELEGRAM_API_ID must be an integer, got: {api_id_str}")
    exit(1)

client = TelegramClient('groq_userbot_session', api_id, api_hash)

async def main():
    await client.connect()
    if await client.is_user_authorized():
        # Export the current active session to a string session format
        session_string = StringSession.save(client.session)
        print("\n==========================================================================================")
        print("TELEGRAM_SESSION_STRING:")
        print(session_string)
        print("==========================================================================================\n")
        print("Copy the long string above and set it as the environment variable 'TELEGRAM_SESSION_STRING'")
        print("in your Railway / production deployment settings to fix the authorization error.")
    else:
        print("Error: Local session 'groq_userbot_session.session' is not authorized.")
        print("Please run login_telegram.py first to authorize your session.")

if __name__ == "__main__":
    import asyncio
    try:
        client.loop.run_until_complete(main())
    except Exception as e:
        print(f"An error occurred: {e}")
