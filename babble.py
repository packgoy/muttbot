"""
Per-server Markov chain text generator with on-disk storage.

- on_message_added(server_id, text): call this for every new message
- generate(server_id): returns a random Markov-generated string (or None)

Messages are kept in memory (capped at MAX_MESSAGES per server) and appended to
<data_dir>/<server_id>.jsonl so they survive restarts. When a server goes over the
cap, the oldest message is evicted and its contributions are removed from the chain.
"""

import json
import os
import random
import sys
from collections import deque

END = None  # sentinel: "message ended here"


def _transitions(words, order):
    """Yield (state, next_word) pairs for one tokenized message."""
    n = len(words)
    if n < order:
        yield tuple(words), END
        return
    for i in range(n - order):
        yield tuple(words[i:i + order]), words[i + order]
    yield tuple(words[n - order:]), END


class _ServerChain:
    def __init__(self, path, order, max_messages):
        self.path = path
        self.order = order
        self.max_messages = max_messages
        self.messages = deque()   # raw message strings, oldest first
        self.chain = {}           # state tuple -> {next_word: count}
        self.lines_on_disk = 0
        self.fh = None
        self.texts = set()        # normalized messages: dedupe + echo detection
        self._load()

    # ---------- in-memory chain ----------
    def _tokenize(self, text):
        return [sys.intern(w) for w in text.split()]

    def _ingest(self, text):
        words = self._tokenize(text)
        if not words:
            return False
        key = " ".join(words)
        if key in self.texts:
            return False  # duplicate, skip it
        self.texts.add(key)
        chain = self.chain
        for state, nxt in _transitions(words, self.order):
            bucket = chain.get(state)
            if bucket is None:
                chain[state] = {nxt: 1}
            else:
                bucket[nxt] = bucket.get(nxt, 0) + 1
        self.messages.append(text)
        return True

    def _evict_oldest(self):
        text = self.messages.popleft()
        words = text.split()
        self.texts.discard(" ".join(words))
        chain = self.chain
        for state, nxt in _transitions(words, self.order):
            bucket = chain.get(state)
            if bucket is None:
                continue
            count = bucket.get(nxt, 0) - 1
            if count <= 0:
                bucket.pop(nxt, None)
                if not bucket:
                    del chain[state]
            else:
                bucket[nxt] = count

    # ---------- disk ----------
    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                self.lines_on_disk += 1
                try:
                    text = json.loads(line)
                except ValueError:
                    continue
                if isinstance(text, str) and self._ingest(text):
                    if len(self.messages) > self.max_messages:
                        self._evict_oldest()
        
        if self.lines_on_disk > len(self.messages):  # duplicates, junk lines or evicted messages
            self._compact()

    def _append_to_disk(self, text):
        if self.fh is None:
            self.fh = open(self.path, "a", encoding="utf-8")
        self.fh.write(json.dumps(text, ensure_ascii=False) + "\n")
        self.fh.flush()
        self.lines_on_disk += 1
        # The file is append-only, so rewrite it now and then to drop evicted messages.
        if self.lines_on_disk > self.max_messages * 1.5:
            self._compact()

    def _compact(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for text in self.messages:
                f.write(json.dumps(text, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)
        self.lines_on_disk = len(self.messages)

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None

    # ---------- public ----------
    def add(self, text):
        if not self._ingest(text):
            return
        self._append_to_disk(text)
        if len(self.messages) > self.max_messages:
            self._evict_oldest()

    def generate(self, max_words):
        if not self.messages:
            return None
        # Start from the opening words of a random stored message, so starts
        # are naturally weighted by how people actually begin messages.
        start = random.choice(self.messages).split()[:self.order]
        state = tuple(sys.intern(w) for w in start)
        out = list(state)
        chain = self.chain
        while len(out) < max_words:
            bucket = chain.get(state)
            if not bucket:
                break
            if len(bucket) == 1:
                word = next(iter(bucket))
            else:
                word = random.choices(list(bucket), weights=list(bucket.values()))[0]
            if word is END:
                break
            out.append(word)
            state = state[1:] + (word,) if len(state) >= self.order else state + (word,)
        return " ".join(out)


class MarkovManager:
    def __init__(self, data_dir="markov_data", order=2, max_messages=100_000):
        self.data_dir = data_dir
        self.order = order
        self.max_messages = max_messages
        self._servers = {}
        os.makedirs(data_dir, exist_ok=True)

    def _get(self, server_id):
        key = str(server_id)
        server = self._servers.get(key)
        if server is None:
            path = os.path.join(self.data_dir, f"{key}.jsonl")
            server = self._servers[key] = _ServerChain(path, self.order, self.max_messages)
        return server

    def has(self, server_id, text):
        return " ".join(text.split()) in self._get(server_id).texts

    def on_message_added(self, server_id, text):
        """Callback: call this whenever a message is added in a server."""
        text = text.strip()
        if text:
            self._get(server_id).add(text)

    def generate(self, server_id, max_words=40):
        """Return a random Markov-generated string, or None if the server has no messages yet."""
        return self._get(server_id).generate(max_words)

    def close(self):
        for server in self._servers.values():
            server.close()
    
    def count(self, server_id):
        """How many messages are currently indexed for a server."""
        return len(self._get(server_id).messages)


# ---------------------------------------------------------------------------
# Example discord.py wiring:
#
#   import discord
#   from markov import MarkovManager
#
#   markov = MarkovManager(order=2, max_messages=100_000)
#   intents = discord.Intents.default()
#   intents.message_content = True          # also enable it in the Developer Portal
#   client = discord.Client(intents=intents)
#
#   @client.event
#   async def on_message(message):
#       if message.author.bot or message.guild is None:
#           return
#       if client.user in message.mentions:  # when pinged, reply with nonsense
#           reply = markov.generate(message.guild.id)
#           if reply:
#               await message.reply(reply, mention_author=False,
#                                   allowed_mentions=discord.AllowedMentions.none())
#       else:
#           markov.on_message_added(message.guild.id, message.content)
#
#   client.run("YOUR_TOKEN")
# ---------------------------------------------------------------------------