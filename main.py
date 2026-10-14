import os

import discord
import babble

PREFIX = "mutt"
DEFAULT_COMMAND = "sup"

# Command callbacks take the message and the text after the command name
# (already stripped of "mutt" and the command word). Parse it however you like.
COMMANDS = {}

babbleman = babble.MarkovManager(order=2, max_messages=10_000)

def command(name):
    """Decorator: register a function as `mutt <name>`."""
    def register(func):
        COMMANDS[name] = func
        return func
    return register






# COMMANDS





@command("sup")
async def sup(msg, args):
    await reply(msg, "sup")

@command("speak")
async def speak(msg, args):
    """Generate a Markov message based on the server's chat history."""
    text = babbleman.generate(msg.guild.id)
    if text:
        await reply(msg, text)
    else:
        await reply(msg, "no messages indexed yet")




# OTHER SHIT




async def reply(msg, text):
    """Reply to a message; user mentions ping, @everyone/@here and role pings don't."""
    await msg.reply(
        text,
        mention_author=False,
        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
    )

async def replied_to_bot(msg):
    """True if this message is a reply to one of the bot's own messages."""
    ref = msg.reference
    if ref is None or ref.message_id is None:
        return False
    target = ref.resolved
    if not isinstance(target, discord.Message):  # not cached, or deleted
        try:
            target = await msg.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return False
    return target.author.id == client.user.id

MIN_INDEXED = 100

def indexable(msg):
    """Normal human chat only: no bots, empty messages, or mutt commands."""
    text = msg.content.strip()
    return bool(text) and not msg.author.bot and not text.lower().startswith(PREFIX)

async def backfill(guild):
    """If the server has fewer than MIN_INDEXED messages indexed, pull some from history."""
    needed = MIN_INDEXED - babbleman.count(guild.id)
    print(f"backfill {guild.id} ({guild.name!r}): need {needed}, "
          f"{len(guild.text_channels)} text channels visible")
    if needed <= 0:
        return

    found = []
    batch_keys = set()  # duplicates within this backfill run
    for channel in guild.text_channels:
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            print(f"  skip #{channel.name}: missing View Channel / Read Message History")
            continue
        seen = empty = 0
        try:
            async for m in channel.history(limit=200):
                seen += 1
                if not m.content.strip():
                    empty += 1
                key = " ".join(m.content.split())
                if (indexable(m)
                        and key not in batch_keys
                        and not babbleman.has(guild.id, m.content)):
                    batch_keys.add(key)
                    found.append(m)
                    if len(found) >= needed:
                        break
        except discord.HTTPException as e:
            print(f"  #{channel.name}: error {e!r}")
        print(f"  #{channel.name}: saw {seen}, {empty} with empty content")
        if len(found) >= needed:
            break

    for m in reversed(found):  # oldest first, so the newest end up newest
        babbleman.on_message_added(guild.id, m.content)
    print(f"{guild.name}: indexed {len(found)} messages from history")

# MESSAGE_CONTENT is a privileged intent: enable it in the Developer Portal.
intents = discord.Intents.none()
intents.guilds = True          # required: without it the bot can't see servers or channels
intents.guild_messages = True
intents.dm_messages = True
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"{client.user.name.upper()}. IS. ONLINE.")
    for guild in client.guilds:
        await backfill(guild)


@client.event
async def on_guild_join(guild):
    await backfill(guild)


@client.event
async def on_message(msg):
    if msg.author.bot:
        return

    text = msg.content.strip()
    if not text.lower().startswith(PREFIX):
        # Ordinary chat: index it, and if it's a reply to the bot, answer like `mutt speak`.
        if msg.guild:
            babbleman.on_message_added(msg.guild.id, msg.content)
            if await replied_to_bot(msg):
                async with msg.channel.typing():
                    await speak(msg, "")
        return

    text = text[len(PREFIX):].strip()  # "mutt cap x" and "muttcap x" both become "cap x"

    # Split off the command word; everything after it is left for the command to parse.
    name, _, args = (text or DEFAULT_COMMAND).partition(" ")
    func = COMMANDS.get(name.lower())
    if func is None:
        return  # ordinary chat like "mutt brain" is ignored

    async with msg.channel.typing():
        try:
            await func(msg, args.strip())
        except Exception as e:
            print(f"command {name!r} failed: {e!r}")
            await reply(msg, "something went wrong")


token = os.environ.get("DIDDY_TOKEN", "").strip()
if not token:
    raise SystemExit("set DIDDY_TOKEN env! `export DIDDY_TOKEN=...`")

client.run(token)