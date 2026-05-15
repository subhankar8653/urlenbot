
import asyncio
import dns.resolver
from pyrogram import idle

from . import app, log, LOGGER

dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ['8.8.8.8']


async def main():
    await app.start()
    me = await app.get_me()
    LOGGER.info(f"Bot started: @{me.username}")
    if log:
        try:
            await app.send_message(chat_id=log, text=f'<b>Bot Started! @{me.username}</b>')
        except Exception as e:
            LOGGER.warning(f"LOG_CHANNEL pe message send nahi hua: {e}")
    await idle()
    await app.stop()

asyncio.run(main())
