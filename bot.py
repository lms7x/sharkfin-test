import os
import json
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
LANDING_BUFFER   = 2   # Land 2min AFTER restock
CHECK_INTERVAL   = 1
WARNING_MINUTES  = 10
PROMETHEUS_URL   = 'https://api.prombot.co.uk/api/travel'
YATA_URL         = 'https://yata.yt/api/v1/travel/export/'

# ============================================================
# STATE - Self-calibrating with cycle tracking
# ============================================================

state = {
    'quantity': None,
    'last_depletion': None,
    'last_restock': None,
    'cycle_history': [],  # [{depletion, restock, duration}]
    'avg_cycle_duration': None,
    'predicted_restock': None,
    'prometheus_restock': None,
    'warning_sent': False,
    'depart_sent': False,
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

def calc_depart_time(restock_ms):
    """Land 2 min AFTER restock, so depart = restock - 94min flight + 2min = restock - 92min"""
    return restock_ms - (FLIGHT_MINUTES * 60 * 1000) + (LANDING_BUFFER * 60 * 1000)

def record_cycle(depletion_time, restock_time):
    """Track complete cycle for self-calibration"""
    duration = restock_time - depletion_time
    state['cycle_history'].append({
        'depletion': depletion_time,
        'restock': restock_time,
        'duration': duration
    })
    
    # Keep last 10 cycles
    if len(state['cycle_history']) > 10:
        state['cycle_history'].pop(0)
    
    # Calculate average cycle duration
    durations = [c['duration'] for c in state['cycle_history']]
    state['avg_cycle_duration'] = sum(durations) / len(durations)
    
    log(f"📊 Cycle recorded | duration: {fmt(duration)} | avg: {fmt(state['avg_cycle_duration'])} | cycles: {len(state['cycle_history'])}")

def predict_next_restock(depletion_time):
    """Self-calibrated prediction based on learned cycles"""
    if state['avg_cycle_duration']:
        return depletion_time + state['avg_cycle_duration']
    # Default: 2 hours
    return depletion_time + (2 * 60 * 60 * 1000)

def validate_and_adjust(predicted, actual):
    """Compare prediction vs Prometheus actual, adjust if needed"""
    error = actual - predicted
    error_min = error / (60 * 1000)
    
    log(f"🎓 Validation: predicted={ts(predicted)} | actual={ts(actual)} | error={fmt(error)}")
    
    # If error > 5 min, adjust the average
    if abs(error_min) > 5 and state['avg_cycle_duration']:
        adjustment = error * 0.3  # 30% correction
        old_avg = state['avg_cycle_duration']
        state['avg_cycle_duration'] += adjustment
        log(f"⚙️ Adjusted avg cycle: {fmt(old_avg)} → {fmt(state['avg_cycle_duration'])}")

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
                            'quantity': shark['quantity'],
                            'cost': shark.get('cost', 0),
                            'next_restock': parse_iso(shark.get('nextRestock')),
                            'source': 'Prometheus'
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
                            'quantity': shark['quantity'],
                            'cost': shark.get('cost', 0),
                            'next_restock': None,
                            'source': 'YATA'
                        }
    except Exception as e:
        log(f"YATA error: {e}")
    return None

async def get_data():
    data = await get_prometheus()
    if data:
        return data
    log("Prometheus unavailable, using YATA...")
    return await get_yata()

# ============================================================
# EMBEDS
# ============================================================

def embed_online():
    e = discord.Embed(
        title="🦈 Shark Fin Bot Online - Self-Calibrating",
        description="Monitoring Hawaii shark fins via Prometheus.\nLearning cycle patterns for optimal timing!",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="✈️ Flight",   value="1h 34m",      inline=True)
    e.add_field(name="🎯 Landing",  value="2min AFTER restock", inline=True)
    e.add_field(name="🔄 Check",    value="Every 1min",  inline=True)
    return e

def embed_depletion(cost):
    e = discord.Embed(
        title="🔴 SHARK FINS DEPLETED",
        description="Stock sold out! Learning cycle timing...",
        color=0xFF4444,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="💰 Last Price", value=f"${cost:,}", inline=True)
    cycles = len(state['cycle_history'])
    if cycles > 0:
        e.add_field(name="📊 Cycles Learned", value=str(cycles), inline=True)
        e.add_field(name="⏱️ Avg Duration", value=fmt(state['avg_cycle_duration']), inline=True)
    if state['predicted_restock']:
        e.add_field(name="🎯 Predicted Restock", value=ts(state['predicted_restock']), inline=False)
        dept = calc_depart_time(state['predicted_restock'])
        e.add_field(name="✈️ Depart At", value=ts(dept), inline=False)
    else:
        e.add_field(name="⏳ Status", value="Watching this cycle to learn timing", inline=False)
    return e

def embed_warning(dept, restock):
    e = discord.Embed(
        title="⏰ DEPART IN 10 MINUTES",
        description="Get ready to fly to Hawaii!",
        color=0xFFAA00,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="✈️ Depart At",  value=ts(dept),    inline=False)
    e.add_field(name="🎯 Restock At", value=ts(restock), inline=False)
    e.add_field(name="🛬 Landing", value=f"{LANDING_BUFFER} min AFTER restock", inline=False)
    return e

def embed_depart(dept, restock):
    landing = dept + (FLIGHT_MINUTES * 60 * 1000)
    e = discord.Embed(
        title="✈️ FLY NOW TO HAWAII!",
        description="**Buy your ticket immediately!**",
        color=0x0099FF,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="🛬 Landing At", value=ts(landing),  inline=False)
    e.add_field(name="🎯 Restock At", value=ts(restock),  inline=False)
    e.add_field(name="⏱️ Timing", value=f"Land {LANDING_BUFFER}min after restock ✅", inline=False)
    return e

def embed_restock(qty, cost):
    e = discord.Embed(
        title="🟢 SHARK FINS RESTOCKED!",
        description=f"**{qty:,} items** available - buy now!",
        color=0x00CC44,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="💰 Price", value=f"${cost:,}", inline=True)
    cycles = len(state['cycle_history'])
    if cycles > 0:
        e.add_field(name="📊 Learned Cycles", value=str(cycles), inline=True)
        conf = "🟢 HIGH" if cycles >= 3 else "🟡 LEARNING"
        e.add_field(name="🎯 Confidence", value=conf, inline=True)
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
    log(f"🦈 Shark Fin Bot online as {bot.user}")
    await dm(embed_online())
    monitor.start()

@tasks.loop(minutes=CHECK_INTERVAL)
async def monitor():
    n = now_ms()
    data = await get_data()
    if not data:
        log("Both sources unavailable")
        return

    qty = data['quantity']
    prev_qty = state['quantity']
    state['quantity'] = qty

    # ── Update Prometheus restock time ──────────────────────────
    if data['next_restock']:
        if data['next_restock'] != state['prometheus_restock']:
            state['prometheus_restock'] = data['next_restock']
            log(f"🔍 Prometheus nextRestock: {ts(data['next_restock'])}")
            
            # Validate our prediction if we had one
            if state['predicted_restock']:
                validate_and_adjust(state['predicted_restock'], data['next_restock'])
            
            # Use Prometheus time as ground truth
            state['predicted_restock'] = data['next_restock']
            state['warning_sent'] = False
            state['depart_sent'] = False

    # ── Depletion detected ──────────────────────────────────────
    if prev_qty is not None and prev_qty > 0 and qty == 0:
        state['last_depletion'] = n
        
        # Make prediction based on learned cycles
        state['predicted_restock'] = predict_next_restock(n)
        state['warning_sent'] = False
        state['depart_sent'] = False
        
        await dm(embed_depletion(data['cost']))
        log(f"🔴 Depletion | predicted next: {ts(state['predicted_restock'])}")

    # ── Restock detected ────────────────────────────────────────
    elif prev_qty == 0 and qty > 0:
        state['last_restock'] = n
        
        # Record complete cycle
        if state['last_depletion']:
            record_cycle(state['last_depletion'], n)
        
        await dm(embed_restock(qty, data['cost']))
        log(f"🟢 Restock")
        
        # Reset
        state['predicted_restock'] = None
        state['prometheus_restock'] = None

    # ── Travel notifications ─────────────────────────────────────
    if state['predicted_restock'] and qty == 0:
        dept = calc_depart_time(state['predicted_restock'])
        warn = dept - (WARNING_MINUTES * 60 * 1000)

        if not state['warning_sent'] and warn <= n < dept:
            await dm(embed_warning(dept, state['predicted_restock']))
            state['warning_sent'] = True
            log("⏰ Warning sent")

        if not state['depart_sent'] and dept <= n < state['predicted_restock']:
            await dm(embed_depart(dept, state['predicted_restock']))
            state['depart_sent'] = True
            log("✈️ Depart sent")

    # ── Status log ──────────────────────────────────────────────
    if state['predicted_restock'] and qty == 0:
        dept = calc_depart_time(state['predicted_restock'])
        log(f"SOLD OUT | restock in {fmt(state['predicted_restock']-n)} | depart in {fmt(dept-n)} | cycles={len(state['cycle_history'])}")
    else:
        log(f"qty={qty} | ${data['cost']:,}")

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
