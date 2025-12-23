import asyncio
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

print("--- CYBERPUNK SESSION GENERATOR ---")
api_id = input("Enter API ID: ")
api_hash = input("Enter API HASH: ")

async def main():
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\n--- COPY THE STRING BELOW (KEEP IT SAFE) ---")
        print(client.session.save())
        print("--------------------------------------------")
        print("Paste this into your Environment Variables as 'SESSION_STRING'")

if __name__ == '__main__':
    asyncio.run(main())
