import discord
import os
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DM_BOT_DISCORD_TOKEN")

intents = discord.Intents.none()
intents.dm_messages = True # Sadece DM almak için

bot = discord.Client(intents=intents) # commands.Bot yerine Client kullan

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    print(f'{bot.user} is online!') # Konsola yazdır

@bot.event
async def on_message(message):
    if message.guild is None and not message.author.bot:
        print(f"DM received from {message.author}: {message.content}")
        try:
            await message.channel.send(f"Mesajınızı aldım: {message.content}")
        except discord.errors.Forbidden:
            print(f"Cannot send DM to {message.author} (Forbidden)")
        except Exception as e:
            print(f"Error sending DM reply: {e}")

if DISCORD_TOKEN:
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("HATA: Geçersiz Discord Token!")
    except discord.errors.PrivilegedIntentsRequired:
        print("HATA: Gerekli Intentler (DM Messages?) etkinleştirilmemiş!")
    except Exception as e:
        print(f"HATA: {e}")
else:
    print("HATA: DM_BOT_DISCORD_TOKEN bulunamadı!")