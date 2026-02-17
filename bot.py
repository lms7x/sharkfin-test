import os
import json
import asyncio
from datetime import datetime, timezone
import discord
from discord.ext import tasks
import aiohttp
from playwright.async_api import async_playwright

# ============================================================
# CONFIGURATION
# ============================================================

DISCORD_TOKEN      = os.environ.get('DISCORD_TOKEN')
YOUR_DISCORD_ID    = int(os.environ.get('DISCORD_USER_ID', 0))
FLIGHT_MINUTES     = 94
BUFFER_MINUTES     = 2
WARNING_MINUTES    = 10
CHECK_INTERVAL     = 1       # minutes
PROMETHEUS_URL     = 'https://prombot.co.uk/travel/hawaii'
YATA_URL           = 'https://yata.yt/api/v1/travel/export/'

# ============================================================
# STATE
# ============================================================

state = {
    'quantity':             None,
    'depletion_time':       None,
    'restock_time':         None,
    'next_restock':         None,
    'warning_sent':         False,
    'depart_sent':          False,
    'restock_alert_sent':   False,
    'cycle_history':        [],
    'avg_restock_delay':    None,
    'prometheus_api_url':   None,   # Discovered automatically on first run
}

# ============================================================
# UTILITIES
# ============================================================

def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def fmt(ms):
    s = abs(ms) / 1000
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m"

def ts(ms):
    """Discord timestamp for dynamic display"""
    epoch = int(ms / 1000)
    return f"<t:{epoch}:T> (<t:{epoch}:R>)"

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ============================================================
# DATA: PROMETHEUS (primary source)
# ============================================================

async def discover_prometheus_api():
    """
    Load prombot.co.uk in a headless browser and intercept the API
    calls it makes. Once discovered, we cache the URL and use it
    directly on subsequent checks (much faster).
    """
    log("🔍 Launching browser to discover Prometheus API...")
    found = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage',
                  '--disable-gpu', '--single-process']
        )
        page = await browser.new_page()

        async def on_response(response):
            try:
                if response.status == 200:
                    ct = response.headers.get('content-type', '')
                    if 'json' in ct and not found:
                        body = await response.json()
                        body_str = json.dumps(body).lower()
                        if any(k in body_str for k in ['shark', '1485', 'hawaii', '"haw"']):
                            found['url']  = response.url
                            found['data'] = body
                            log(f"🎯 Prometheus API found: {response.url}")
            except:
                pass

        page.on('response', on_response)

        try:
            await page.goto(PROMETHEUS_URL, wait_until='networkidle', timeout=30000)
            await asyncio.sleep(6)
        except Exception as e:
            log(f"⚠️  Browser load error: {e}")
        finally:
            await browser.close()

    return found

def parse_prometheus(data):
    """
    Flexibly parse whatever structure Prometheus returns.
    Looks for Shark Fin (id 1485) anywhere in the response.
    """
    if not data:
        return None
    body = json.dumps(data).lower()
    if 'shark' not in body and '1485' not in body:
        return None

    def search(obj):
        if isinstance(obj, list):
            for item in obj:
                result = search(item)
                if result:
                    return result
        elif isinstance(obj, dict):
            id_val   = obj.get('id')
            name_val = str(obj.get('name', '')).lower()
            if id_val == 1485 or id_val == '1485' or 'shark' in name_val:
                return {'quantity': obj.get('quantity', 0),
                        'cost':     obj.get('cost', 0),
                        'source':   'Prometheus'}
            for v in obj.values():
                result = search(v)
                if result:
                    return result
        return None

    return search(data)

async def get_from_prometheus():
    """Fetch shark fin data from Prometheus. Discovers API on first call."""

    # If we already know the endpoint, use it directly (fast path)
    if state['prometheus_api_url']:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    state['prometheus_api_url'],
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        result = parse_prometheus(data)
                        if result:
                            return result
                        # Structure changed — rediscover
                        log("⚠️  Cached Prometheus endpoint no longer has shark data, rediscovering...")
                        state['prometheus_api_url'] = None
        except Exception as e:
            log(f"⚠️  Prometheus direct fetch error: {e}")

    # Slow path: launch browser and intercept API calls
    found = await discover_prometheus_api()
    if found.get('url'):
        state['prometheus_api_url'] = found['url']
        result = parse_prometheus(found.get('data'))
        if result:
            return result

    return None

# ============================================================
# DATA: YATA (fallback source)
# ============================================================

async def get_from_yata():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                YATA_URL,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json(content_type=None)
                haw    = data.get('stocks', {}).get('haw', {})
                stocks = haw.get('stocks', [])
                shark  = next((s for s in stocks if s.get('id') == 1485), None)
                if shark:
                    return {'quantity':    shark['quantity'],
                            'cost':        shark.get('cost', 0),
                            'source':      'YATA',
                            'update_time': haw.get('update', 0) * 1000}
    except Exception as e:
        log(f"⚠️  YATA error: {e}")
    return None

async def get_best_data():
    """Try Prometheus first, fall back to YATA."""
    data = await get_from_prometheus()
    if data:
        log(f"📦 Prometheus → qty={data['quantity']} cost=${data['cost']:,}")
        return data

    log("ℹ️  Prometheus unavailable, trying YATA...")
    data = await get_from_yata()
    if data:
        log(f"📦 YATA → qty={data['quantity']} cost=${data['cost']:,}")
        return data

    log("❌ Both sources unavailable")
    return None

# ============================================================
# PREDICTION ENGINE (self-calibrating, YATA-delay aware)
# ============================================================

def record_cycle(restock_t, depletion_t, next_restock_t):
    cycle = {
        'depletion_duration': depletion_t     - restock_t,
        'restock_delay':      next_restock_t  - depletion_t,
        'total':              next_restock_t  - restock_t,
    }
    state['cycle_history'].append(cycle)
    if len(state['cycle_history']) > 10:
        state['cycle_history'].pop(0)

    delays = [c['restock_delay'] for c in state['cycle_history']]
    state['avg_restock_delay'] = sum(delays) / len(delays)

    n = len(state['cycle_history'])
    conf = '🟢 HIGH' if n >= 3 else '🟡 MEDIUM'
    log(f"📊 Cycle #{n} recorded | avg restock delay: {fmt(state['avg_restock_delay'])} | {conf}")

def predict_next_restock(depletion_time):
    if state['avg_restock_delay']:
        return depletion_time + state['avg_restock_delay']
    return depletion_time + (2 * 60 * 60 * 1000)   # default 2 hours

def calc_depart(restock_time):
    return restock_time - (FLIGHT_MINUTES * 60 * 1000) - (BUFFER_MINUTES * 60 * 1000)

def confidence_label():
    n = len(state['cycle_history'])
    if n >= 3: return f"🟢 HIGH ({n} cycles)"
    if n >= 1: return f"🟡 MEDIUM ({n} cycle)"
    return "🔴 LOW (default estimate)"

# ============================================================
# DISCORD EMBEDS
# ============================================================

def embed_depletion(data, depart_time):
    e = discord.Embed(
        title       = "🔴 SHARK FINS DEPLETED",
        description = "Stock sold out! Departure countdown started.",
        color       = 0xFF4444,
        timestamp   = datetime.now(timezone.utc)
    )
    e.add_field(name="📦 Source",          value=data['source'],                               inline=True)
    e.add_field(name="📊 Confidence",      value=confidence_label(),                           inline=True)
    e.add_field(name="🎯 Predicted Restock", value=ts(state['next_restock']),                  inline=False)
    e.add_field(name="✈️ Depart At",        value=ts(depart_time),                             inline=False)
    return e

def embed_warning(depart_time):
    e = discord.Embed(
        title       = "⏰ DEPART IN 10 MINUTES",
        description = "Start getting ready to fly!",
        color       = 0xFFAA00,
        timestamp   = datetime.now(timezone.utc)
    )
    e.add_field(name="✈️ Depart At",   value=ts(depart_time),           inline=False)
    e.add_field(name="🎯 Restock At",  value=ts(state['next_restock']),  inline=False)
    return e

def embed_depart(depart_time):
    landing_time = depart_time + (FLIGHT_MINUTES * 60 * 1000) + (BUFFER_MINUTES * 60 * 1000)
    e = discord.Embed(
        title       = "✈️ FLY NOW TO HAWAII!",
        description = "**Buy your ticket immediately!**",
        color       = 0x0099FF,
        timestamp   = datetime.now(timezone.utc)
    )
    e.add_field(name="🛬 Landing At",    value=ts(landing_time),          inline=False)
    e.add_field(name="🎯 Restock At",   value=ts(state['next_restock']),  inline=False)
    e.add_field(name="⚡ Remember",      value="15-second protection window on arrival!", inline=False)
    return e

def embed_restock(data):
    e = discord.Embed(
        title       = "🟢 SHARK FINS RESTOCKED!",
        description = f"**{data['quantity']:,} items** now available!",
        color       = 0x00CC44,
        timestamp   = datetime.now(timezone.utc)
    )
    e.add_field(name="💰 Price",          value=f"${data['cost']:,}",     inline=True)
    e.add_field(name="📦 Source",         value=data['source'],           inline=True)
    e.add_field(name="📈 Cycles Tracked", value=str(len(state['cycle_history'])), inline=True)
    return e

# ============================================================
# BOT
# ============================================================

intents = discord.Intents.default()
bot     = discord.Client(intents=intents)

async def dm(content="", embed=None):
    user = await bot.fetch_user(YOUR_DISCORD_ID)
    await user.send(content=content, embed=embed)

@bot.event
async def on_ready():
    log(f"🦈 Shark Fin Bot online as {bot.user}")
    await dm(embed=discord.Embed(
        title       = "🦈 Shark Fin Bot Online",
        description = "Monitoring Hawaii shark fins every minute.\nWill notify when it's time to travel!",
        color       = 0x5865F2,
        timestamp   = datetime.now(timezone.utc)
    ))
    monitor.start()

@tasks.loop(minutes=CHECK_INTERVAL)
async def monitor():
    n    = now_ms()
    data = await get_best_data()
    if not data:
        return

    qty      = data['quantity']
    prev_qty = state['quantity']
    state['quantity'] = qty

    # ── Depletion detected ──────────────────────────────────
    if prev_qty is not None and prev_qty > 0 and qty == 0:
        state['depletion_time']     = n
        state['next_restock']       = predict_next_restock(n)
        state['warning_sent']       = False
        state['depart_sent']        = False
        state['restock_alert_sent'] = False

        depart_time = calc_depart(state['next_restock'])
        await dm(embed=embed_depletion(data, depart_time))
        log("📤 Depletion notification sent")

    # ── Restock detected ────────────────────────────────────
    elif prev_qty == 0 and qty > 0:
        prev_restock = state['restock_time']
        if prev_restock and state['depletion_time']:
            record_cycle(prev_restock, state['depletion_time'], n)

        state['restock_time']       = n
        state['restock_alert_sent'] = True

        await dm(embed=embed_restock(data))
        log("📤 Restock notification sent")

    # ── Countdown notifications ─────────────────────────────
    if state['next_restock']:
        depart_time = calc_depart(state['next_restock'])
        warn_time   = depart_time - (WARNING_MINUTES * 60 * 1000)

        if not state['warning_sent'] and warn_time <= n < depart_time:
            await dm(embed=embed_warning(depart_time))
            state['warning_sent'] = True
            log("📤 Warning notification sent")

        if not state['depart_sent'] and depart_time <= n < depart_time + (5 * 60 * 1000):
            await dm(embed=embed_depart(depart_time))
            state['depart_sent'] = True
            log("📤 Departure notification sent")

    # ── Status log ──────────────────────────────────────────
    if state['next_restock']:
        time_to_restock = state['next_restock'] - n
        depart_time     = calc_depart(state['next_restock'])
        time_to_depart  = depart_time - n
        log(f"🎯 Restock in {fmt(time_to_restock)} | ✈️  Depart in {fmt(time_to_depart)}")
    else:
        log("📊 Learning... waiting to observe depletion cycle")

@monitor.before_loop
async def before_monitor():
    await bot.wait_until_ready()

# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == '__main__':
    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN environment variable not set!")
        exit(1)
    if not YOUR_DISCORD_ID:
        print("❌ DISCORD_USER_ID environment variable not set!")
        exit(1)
    bot.run(DISCORD_TOKEN)
