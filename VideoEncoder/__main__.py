
import dns.resolver
from pyrogram import idle

from . import app, log

dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = [
    '8.8.8.8']  # this is a google public dns


async def main():
    await app.start()
    from .utils.thumb_style import load_style_cache
    await load_style_cache()
    await app.send_message(chat_id=log, text=f'<b>Bot Started! @{(await app.get_me()).username}</b>')
    await idle()
    await app.stop()

app.loop.run_until_complete(main())
