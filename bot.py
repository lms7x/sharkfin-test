import os
from datetime import datetime, timezone
import discord
from discord.ext import tasks
import aiohttp

# ============================================================
# CONFIGURATION
# ============================================================

DISCORD_TOKEN    = os.environ.get('DISCORD_TOKEN')
YOUR_DISCORD_ID  = int(os.environ.get('DISCORD_USER_ID', 0))
FLIGHT_MINUTES   = 94
BUFFER_MINUTES   = 2
WARNING_MINUTES  = 10
CHECK_INTERVAL   = 1
PROMETHEUS_URL   = 'https://api.prombot.co.uk/api/travel'
YATA_URL         = 'https://yata.yt/api/v1/travel/export/'

# ============================================================
# STATE
# ============================================================

state = {
    'quantity':     None,
    'next_restock': None,
    'warning_sent': False,
    'depart_sent':  False,
    'restock_sent': False,
}

# ============================================================
# UTILITIES
# ============================================================

def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def fmt(ms):
    s = abs(int(ms)) // 1000
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m" if h > 0 else f"{m}m"

def ts(ms):
    epoch = int(ms / 1000)
    return f"<t:{epoch}:T> (<t:{epoch}:R>)"

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def parse_iso(dt_str):
    if not dt_str:
        return None
    try:
        return int(datetime.fromisoformat(dt_str.replace('Z', '+00:00')).timestamp() * 1000)
    except:
        return None

def depart_time():
    if not state['next_restock']:
        return None
    return state['next_restock'] - (FLIGHT_MINUTES * 60 * 1000) - (BUFFER_MINUTES * 60 * 1000)

# ============================================================
# DATA
# ============================================================

async def get_prometheus():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(PROMETHEUS_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    haw = data.get('stocks', {}).get('haw', {})
                    shark = next((s for s in haw.get('stocks', []) if s.get('id') == 1485), None)
                    if shark:
                        return {
                            'quantity':    shark['quantity'],
                            'cost':        shark.get('cost', 0),
                            'next_restock': parse_iso(shark.get('nextRestock')),
                            'source':      'Prometheus'
                        }
    except Exception as e:
        log(f"Prometheus error: {e}")
    return None

async def get_yata():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(YATA_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    haw = data.get('stocks', {}).get('haw', {})
                    shark = next((s for s in haw.get('stocks', []) if s.get('id') == 1485), None)
                    if shark:
                        return {
                            'quantity':    shark['quantity'],
                            'cost':        shark.get('cost', 0),
                            'next_restock': None,
                            'source':      'YATA'
                        }
    except Exception as e:
        log(f"YATA error: {e}")
    return None

async def get_data():
    data = await get_prometheus()
    if data:
        return data
    log("Prometheus unavailable, falling back to YATA...")
    return await get_yata()

# ============================================================
# EMBEDS
# ============================================================

def embed_online():
    e = discord.Embed(
        title="🦈 Shark Fin Bot Online",
        description="Monitoring Hawaii shark fins every minute via Prometheus.\nYou'll get a DM when it's time to travel!",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="✈️ Flight Time", value="1h 34m",              inline=True)
    e.add_field(name="⏱️ Buffer",      value=f"{BUFFER_MINUTES} min", inline=True)
    e.add_field(name="🔄 Check Rate",  value="Every 1 minute",      inline=True)
    return e

def embed_depletion(data, dept):
    e = discord.Embed(
        title="🔴 SHARK FINS DEPLETED",
        description="Stock sold out! Departure timer started.",
        color=0xFF4444,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="📦 Source",     value=data['source'],       inline=True)
    e.add_field(name="💰 Last Price", value=f"${data['cost']:,}", inline=True)
    if state['next_restock'] and dept:
        e.add_field(name="🎯 Restock At", value=ts(state['next_restock']), inline=False)
        e.add_field(name="✈️ Depart At",  value=ts(dept),                  inline=False)
    else:
        e.add_field(name="⏳ Restock Time", value="Waiting for Prometheus data...", inline=False)
    return e

def embed_warning(dept):
    e = discord.Embed(
        title="⏰ DEPART IN 10 MINUTES",
        description="Start getting ready to fly to Hawaii!",
        color=0xFFAA00,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="✈️ Depart At",  value=ts(dept),                  inline=False)
    e.add_field(name="🎯 Restock At", value=ts(state['next_restock']), inline=False)
    return e

def embed_depart(dept):
    landing = dept + (FLIGHT_MINUTES * 60 * 1000) + (BUFFER_MINUTES * 60 * 1000)
    e = discord.Embed(
        title="✈️ FLY NOW TO HAWAII!",
        description="**Buy your ticket immediately!**",
        color=0x0099FF,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="🛬 Landing At", value=ts(landing),               inline=False)
    e.add_field(name="🎯 Restock At", value=ts(state['next_restock']), inline=False)
    e.add_field(name="⚡ Remember",   value="15-second protection window on arrival!", inline=False)
    return e

def embed_restock(data):
    e = discord.Embed(
        title="🟢 SHARK FINS RESTOCKED!",
        description=f"**{data['quantity']:,} items** now available!",
        color=0x00CC44,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="💰 Price",  value=f"${data['cost']:,}", inline=True)
    e.add_field(name="📦 Source", value=data['source'],       inline=True)
    return e

# ============================================================
# BOT
# ============================================================

intents = discord.Intents.default()
bot = discord.Client(intents=intents)

async def dm(embed):
    user = await bot.fetch_user(YOUR_DISCORD_ID)
    await user.send(embed=embed)

@bot.event
async def on_ready():
    log(f"Shark Fin Bot online as {bot.user}")
    await dm(embed_online())
    monitor.start()

@tasks.loop(minutes=CHECK_INTERVAL)
async def monitor():
    n    = now_ms()
    data = await get_data()
    if not data:
        log("Both sources unavailable")
        return

    qty      = data['quantity']
    prev_qty = state['quantity']
    state['quantity'] = qty

    # Always update next_restock from Prometheus when stock is 0
    if data['next_restock']:
        state['next_restock'] = data['next_restock']

    dept = depart_time()

    # ── On first run after restart, initialize state correctly ──
    # If bot restarted while stock was already 0, treat as if we
    # just detected depletion so countdown notifications work
    if prev_qty is None and qty == 0 and state['next_restock']:
        log("Bot restarted while stock depleted - restoring countdown state")
        state['warning_sent'] = False
        state['depart_sent']  = False
        state['restock_sent'] = False

    # ── Depletion detected ──────────────────────────────────────
    if prev_qty is not None and prev_qty > 0 and qty == 0:
        state['warning_sent'] = False
        state['depart_sent']  = False
        state['restock_sent'] = False
        await dm(embed_depletion(data, dept))
        log(f"Depletion alert sent | Restock: {state['next_restock']} | Depart: {dept}")

    # ── Restock detected ────────────────────────────────────────
    elif prev_qty == 0 and qty > 0:
        state['restock_sent'] = True
        await dm(embed_restock(data))
        log("Restock alert sent")

    # ── Countdown (fires whenever stock is 0 and time is right) ─
    # Uses wide window (30 min) so it's never missed between checks
    if dept and qty == 0:
        warn_time = dept - (WARNING_MINUTES * 60 * 1000)

        if not state['warning_sent'] and warn_time <= n < dept:
            await dm(embed_warning(dept))
            state['warning_sent'] = True
            log("Warning notification sent")

        # Wide 30-min window so a bot restart never misses this
        if not state['depart_sent'] and dept <= n < dept + (30 * 60 * 1000):
            await dm(embed_depart(dept))
            state['depart_sent'] = True
            log("Departure notification sent")

    # ── Status log ──────────────────────────────────────────────
    if dept and qty == 0:
        log(f"[{data['source']}] SOLD OUT | Restock in {fmt(state['next_restock'] - n)} | Depart in {fmt(dept - n)}")
    else:
        log(f"[{data['source']}] qty={qty} | ${data['cost']:,}")

@monitor.before_loop
async def before_monitor():
    await bot.wait_until_ready()

if __name__ == '__main__':
    if not DISCORD_TOKEN:
        print("DISCORD_TOKEN not set!")
        exit(1)
    if not YOUR_DISCORD_ID:
        print("DISCORD_USER_ID not set!")
        exit(1)
    bot.run(DISCORD_TOKEN)
