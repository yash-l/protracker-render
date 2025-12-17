from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("Enter API ID: "))
api_hash = input("Enter API Hash: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n👇 COPY THIS LONG STRING CAREFULLY 👇\n")
    print(client.session.save())
    print("\n👆 SAVE IT FOR RENDER 👆\n")
