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
    prefix = "-" if ms < 0 else ""
    return f"{prefix}{h}h {m}m" if h > 0 else f"{prefix}{m}m"

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

def get_depart_time(restock_ms):
    return restock_ms - (FLIGHT_MINUTES * 60 * 1000) - (BUFFER_MINUTES * 60 * 1000)

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
    e.add_field(name="✈️ Flight Time", value="1h 34m",               inline=True)
    e.add_field(name="⏱️ Buffer",      value=f"{BUFFER_MINUTES} min", inline=True)
    e.add_field(name="🔄 Check Rate",  value="Every 1 minute",       inline=True)
    return e

def embed_depletion(data, dept, restock):
    e = discord.Embed(
        title="🔴 SHARK FINS DEPLETED",
        description="Stock sold out!",
        color=0xFF4444,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="📦 Source",     value=data['source'],       inline=True)
    e.add_field(name="💰 Last Price", value=f"${data['cost']:,}", inline=True)
    if restock and dept:
        e.add_field(name="🎯 Restock At", value=ts(restock), inline=False)
        e.add_field(name="✈️ Depart At",  value=ts(dept),    inline=False)
    return e

def embed_warning(dept, restock):
    e = discord.Embed(
        title="⏰ DEPART IN 10 MINUTES",
        description="Start getting ready to fly to Hawaii!",
        color=0xFFAA00,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="✈️ Depart At",  value=ts(dept),    inline=False)
    e.add_field(name="🎯 Restock At", value=ts(restock), inline=False)
    return e

def embed_depart(dept, restock, late_by_ms=0):
    landing = dept + (FLIGHT_MINUTES * 60 * 1000) + (BUFFER_MINUTES * 60 * 1000)
    if late_by_ms > 60000:
        title = f"✈️ FLY NOW - {fmt(late_by_ms)} LATE BUT GO!"
        desc  = f"**Ideal departure was {fmt(late_by_ms)} ago - still worth flying!**"
    else:
        title = "✈️ FLY NOW TO HAWAII!"
        desc  = "**Buy your ticket immediately!**"
    e = discord.Embed(
        title=title,
        description=desc,
        color=0x0099FF,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="🛬 Landing At", value=ts(landing),  inline=False)
    e.add_field(name="🎯 Restock At", value=ts(restock),  inline=False)
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
# CORE LOGIC: fire travel notifications immediately on new restock time
# ============================================================

async def handle_new_restock_time(restock, cost, source):
    """
    Called the moment we see a new nextRestock timestamp.
    Immediately figures out where we are in the timeline and
    fires the right notification without waiting for a future check.
    """
    n    = now_ms()
    dept = get_depart_time(restock)
    warn = dept - (WARNING_MINUTES * 60 * 1000)

    log(f"New restock time: {restock} | depart: {dept} | now: {n} | diff: {fmt(dept - n)}")

    # Too late - restock already happened or is imminent, skip travel alerts
    if n >= restock:
        log("Restock time already passed, skipping travel notifications")
        return

    # Past depart time but restock not yet - send depart immediately (you're late but still go!)
    if n >= dept:
        late_by = n - dept
        log(f"Past ideal depart time by {fmt(late_by)} - sending late departure alert")
        await dm(embed_depart(dept, restock, late_by_ms=late_by))
        state['depart_sent']  = True
        state['warning_sent'] = True  # Skip warning, already past it
        return

    # Past warning time but before depart - send warning immediately
    if n >= warn:
        log("Inside warning window - sending warning immediately")
        await dm(embed_warning(dept, restock))
        state['warning_sent'] = True
        # Depart will fire on next check via normal loop
        return

    # We're early - warning and depart will fire via normal loop
    log(f"On schedule - warning in {fmt(warn - n)}, depart in {fmt(dept - n)}")

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

    # ── New restock time from Prometheus ────────────────────────
    # This is the KEY trigger - fires immediately on any new restock time
    if data['next_restock'] and data['next_restock'] != state['next_restock']:
        state['next_restock'] = data['next_restock']
        state['warning_sent'] = False
        state['depart_sent']  = False
        state['restock_sent'] = False
        await handle_new_restock_time(data['next_restock'], data['cost'], data['source'])

    # ── Depletion detected ──────────────────────────────────────
    if prev_qty is not None and prev_qty > 0 and qty == 0:
        dept = get_depart_time(state['next_restock']) if state['next_restock'] else None
        await dm(embed_depletion(data, dept, state['next_restock']))
        log(f"Depletion alert sent")

    # ── Restock detected ────────────────────────────────────────
    elif prev_qty == 0 and qty > 0 and not state['restock_sent']:
        state['restock_sent'] = True
        await dm(embed_restock(data))
        log("Restock alert sent")

    # ── Normal countdown loop (for when we're early enough) ─────
    if state['next_restock'] and qty == 0:
        dept      = get_depart_time(state['next_restock'])
        warn_time = dept - (WARNING_MINUTES * 60 * 1000)

        if not state['warning_sent'] and warn_time <= n < dept:
            await dm(embed_warning(dept, state['next_restock']))
            state['warning_sent'] = True
            log("Warning notification sent")

        if not state['depart_sent'] and dept <= n < state['next_restock']:
            await dm(embed_depart(dept, state['next_restock']))
            state['depart_sent'] = True
            log("Departure notification sent")

    # ── Status log ──────────────────────────────────────────────
    if state['next_restock'] and qty == 0:
        dept = get_depart_time(state['next_restock'])
        log(f"[{data['source']}] SOLD OUT | restock in {fmt(state['next_restock']-n)} | depart in {fmt(dept-n)} | W={state['warning_sent']} D={state['depart_sent']}")
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
