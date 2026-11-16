import time
import httpx

DISCORD_API_BASE = "https://discord.com/api/v9"

class DiscordRateLimitError(Exception):
    def __init__(self, retry_after: float, detail: str = "Rate limit / Slowmode Discord"):
        self.retry_after = max(5, int(retry_after) + 2)
        self.detail = detail
        super().__init__(detail)

def clean_token(token: str) -> str:
    t = token.strip().strip('"').strip("'")
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if t.startswith("Bot "):
        t = t[4:].strip()
    return t

def _headers(token: str) -> dict:
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:159.0) Gecko/20100101 Firefox/159.0',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Content-Type': 'application/json',
        'Authorization': clean_token(token),
        'X-Discord-Locale': 'en-US',
        'X-Discord-Timezone': 'Asia/Jakarta',
        'Origin': 'https://discord.com',
    }

async def fetch_user_profile(token: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{DISCORD_API_BASE}/users/@me", headers=_headers(token))
        if r.status_code != 200:
            print(f"[Discord Auth Error] {r.status_code}: {r.text}")
            r.raise_for_status()
        u = r.json()
        avatar = (
            f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png"
            if u.get("avatar") else "https://cdn.discordapp.com/embed/avatars/0.png"
        )
        return {"discord_id": u["id"], "username": u["username"], "avatar_url": avatar}

async def fetch_joined_guilds(token: str) -> list:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{DISCORD_API_BASE}/users/@me/guilds", headers=_headers(token))
        r.raise_for_status()
        return [{"id": g["id"], "name": g["name"], "icon": g.get("icon")} for g in r.json()]

async def fetch_guild_text_channels(token: str, guild_id: str, guild_name: str = "") -> list:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/channels", headers=_headers(token))
        if r.status_code == 403:
            return []
        r.raise_for_status()
        return [
            {"id": ch["id"], "name": ch["name"], "guild_name": guild_name}
            for ch in r.json() if ch.get("type") in (0, 5)
        ]

async def fetch_single_channel(token: str, channel_id: str, guild_name: str = "Direct Channel") -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{DISCORD_API_BASE}/channels/{channel_id.strip()}", headers=_headers(token))
        r.raise_for_status()
        ch = r.json()
        return {
            "id": ch["id"],
            "name": ch.get("name") or f"channel-{channel_id.strip()}",
            "guild_name": guild_name
        }

async def send_channel_message(token: str, channel_id: str, content: str) -> str:
    payload = {
        "mobile_network_type": "unknown",
        "content": content,
        "nonce": str(int(time.time())),
        "tts": False,
        "flags": 0
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            headers=_headers(token),
            json=payload
        )
        if r.status_code == 429:
            try:
                data = r.json()
            except Exception:
                data = {}
            retry_after = float(data.get("retry_after", 60))
            msg = data.get("message", "Slowmode / Rate Limit Discord")
            raise DiscordRateLimitError(retry_after, f"{msg} (tunggu {int(retry_after)} detik)")
        r.raise_for_status()
        return r.json()["id"]

async def delete_channel_message(token: str, channel_id: str, message_id: str) -> bool:
    async with httpx.AsyncClient() as client:
        r = await client.delete(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
            headers=_headers(token)
        )
        return r.status_code in (200, 204)