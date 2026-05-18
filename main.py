import os
import time
import asyncio
import httpx
import json
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from telegram.constants import ParseMode
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID"))
METALS_API_KEY = os.getenv("METALS_DEV_API_KEY")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

STATE_FILE = "bot_state.json"
START_TIME = datetime.now()

# --- DUMMY WEB SERVER ---
def run_health_check_server():
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is alive!")
        def log_message(self, format, *args): return
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- STATE MANAGEMENT ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: return {"alerts": {}, "frequency": 300, "last_prices": {}}
    return {"alerts": {}, "frequency": 300, "last_prices": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)

state = load_state()
if "alerts" not in state or not state["alerts"]:
    state["alerts"] = {
        "BTC": {"targets": [{"val": 105000, "above": True}, {"val": 70000, "above": False}], "threshold": -5.0},
        "ETH": {"targets": [{"val": 4000, "above": True}, {"val": 2000, "above": False}], "threshold": -5.0},
        "SOL": {"targets": [{"val": 250, "above": True}, {"val": 80, "above": False}], "threshold": -5.0},
        "SUI": {"targets": [{"val": 5.0, "above": True}, {"val": 1.05, "above": False}], "threshold": -5.0},
        "HYPE": {"targets": [{"val": 50, "above": True}, {"val": 35, "above": False}], "threshold": -8.0},
        "GOLD": {"targets": [{"val": 2800, "above": True}, {"val": 2300, "above": False}], "threshold": -2.0},
        "SILVER": {"targets": [{"val": 35, "above": True}, {"val": 28, "above": False}], "threshold": -3.0},
    }
if "frequency" not in state: state["frequency"] = 300
if "last_prices" not in state: state["last_prices"] = {}
save_state(state)

async def fetch_fear_greed():
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.alternative.me/fng/", timeout=10)
            data = resp.json()
            return f"🎭 **Fear & Greed Index**: {data['data'][0]['value']} ({data['data'][0]['value_classification']})"
    except: return None

async def fetch_market_data():
    async with httpx.AsyncClient() as client:
        # Fetch crypto prices (including PAXG and KAG as reference for gold/silver change)
        crypto_ids = {
            "BTC": "bitcoin",
            "ETH": "ethereum",
            "SOL": "solana",
            "SUI": "sui",
            "HYPE": "hyperliquid",
            "PAXG": "pax-gold",
            "KAG": "kinesis-silver"
        }
        ids_str = ",".join(crypto_ids.values())
        base_url = "https://api.coingecko.com/api/v3" if COINGECKO_API_KEY.startswith("CG-") else "https://pro-api.coingecko.com/api/v3"
        cg_url = f"{base_url}/simple/price?ids={ids_str}&vs_currencies=usd&include_24hr_change=true"
        cg_headers = {"x-cg-demo-api-key" if COINGECKO_API_KEY.startswith("CG-") else "x-cg-pro-api-key": COINGECKO_API_KEY}
        market_data = {}
        try:
            resp = await client.get(cg_url, headers=cg_headers, timeout=15)
            raw = resp.json()
            for symbol, cid in crypto_ids.items():
                if cid in raw:
                    p, c = raw[cid]["usd"], raw[cid].get("usd_24h_change", 0)
                    if symbol not in ["PAXG", "KAG"]:
                        market_data[symbol] = {"price": p, "change": c}
                    state["last_prices"][symbol] = {"price": p, "change": c}
        except Exception as e:
            print(f"CG Fetch Error: {e}")

        # Fetch Spot Gold & Silver prices using completely free Gold-API.com
        for m_symbol in ["GOLD", "SILVER"]:
            try:
                symbol_code = "XAU" if m_symbol == "GOLD" else "XAG"
                cg_ref = "PAXG" if m_symbol == "GOLD" else "KAG"
                m_url = f"https://api.gold-api.com/price/{symbol_code}"
                m_resp = await client.get(m_url, timeout=15)
                if m_resp.status_code == 200:
                    m_data = m_resp.json()
                    p = float(m_data["price"])
                    # Use change from PAXG/KAG reference
                    c = state["last_prices"].get(cg_ref, {}).get("change", 0)
                    market_data[m_symbol] = {"price": p, "change": c}
                    state["last_prices"][m_symbol] = {"price": p, "change": c}
                else:
                    print(f"Gold-API Status Error for {m_symbol}: {m_resp.status_code}")
            except Exception as e:
                print(f"Gold-API Fetch Error for {m_symbol}: {e}")
            
            # Failover 1: If Gold-API fails but CoinGecko succeeded, use CoinGecko token prices
            if m_symbol not in market_data and cg_ref in state["last_prices"]:
                market_data[m_symbol] = state["last_prices"][cg_ref]
                state["last_prices"][m_symbol] = state["last_prices"][cg_ref]

            # Failover 2: If everything fails, use last known price from state
            if m_symbol not in market_data and m_symbol in state["last_prices"]:
                market_data[m_symbol] = state["last_prices"][m_symbol]
        
        save_state(state)
        return market_data

async def generate_report():
    data = await fetch_market_data()
    fng = await fetch_fear_greed()
    if not data: return "⚠️ Error fetching market data."
    alerts, pos, neg = [], [], []
    for asset in state["alerts"].keys():
        if asset not in data: continue
        price, change = data[asset]["price"], data[asset]["change"]
        config = state["alerts"][asset]
        change_str = f"**({change:+.2f}%)**"
        line = f"• **{asset}**: ${price:,.2f} {change_str}"
        for target in config["targets"]:
            if (target["above"] and price >= target["val"]) or (not target["above"] and price <= target["val"]):
                icon = "🚀" if target["above"] else "🚨"
                alerts.append(f"{icon} **{asset}** is at ${price:,.2f} {change_str}")
                break
        if change <= config["threshold"]:
            alerts.append(f"📉 **{asset} CRASHING**: {change_str} drop!")
        if change >= 0: pos.append(line)
        else: neg.append(line)
    msg = f"📊 **MARKET UPDATE** ({datetime.now().strftime('%H:%M')})\n\n"
    if fng: msg += f"{fng}\n\n"
    if alerts: msg += "🔔 **ALERTS**\n" + "\n".join(alerts) + "\n\n"
    if pos: msg += "📈 **POSITIVE (+)**\n" + "\n".join(pos) + "\n\n"
    if neg: msg += "📉 **NEGATIVE (-)**\n" + "\n".join(neg)
    msg += f"\n\n_Prices per coin / troy ounce (Metals)_"
    return msg

# --- COMMAND HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 C&C Alert Bot Online! Use /help to see all commands.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    freq_mins = state["frequency"] // 60
    help_text = (
        "🤖 **Available Commands:**\n\n"
        "/now - Instant market update\n"
        "/targets - View all active alerts\n"
        "/setalert <Asset> <Price> - Set a new alert\n"
        "/delete <Asset> <Index> - Remove an alert\n"
        f"/frequency <Mins> - Change update speed ({freq_mins}m)\n"
        "/status - Check bot health"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def setalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        asset, price = context.args[0].upper(), float(context.args[1])
        if asset not in state["alerts"]:
            await update.message.reply_text(f"❌ Asset '{asset}' not supported.")
            return
        data = await fetch_market_data()
        above = price > data[asset]["price"]
        state["alerts"][asset]["targets"].append({"val": price, "above": above})
        save_state(state)
        await update.message.reply_text(f"✅ Alert set: **{asset}** when price is {'ABOVE' if above else 'BELOW'} **${price:,.2f}**")
    except:
        await update.message.reply_text("❌ Usage: `/setalert BTC 110000`")

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        asset, index = context.args[0].upper(), int(context.args[1]) - 1
        if asset in state["alerts"] and 0 <= index < len(state["alerts"][asset]["targets"]):
            removed = state["alerts"][asset]["targets"].pop(index)
            save_state(state)
            await update.message.reply_text(f"🗑 Removed alert: **{asset}** at **${removed['val']:,.2f}**")
        else:
            await update.message.reply_text("❌ Invalid asset or index.")
    except:
        await update.message.reply_text("❌ Usage: `/delete SUI 1`")

async def frequency_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        mins = int(context.args[0])
        if mins < 1:
            await update.message.reply_text("❌ Minimum frequency is 1 minute.")
            return
        new_seconds = mins * 60
        state["frequency"] = new_seconds
        save_state(state)
        jobs = context.job_queue.get_jobs_by_name("scheduled_check")
        for job in jobs: job.schedule_removal()
        context.job_queue.run_repeating(scheduled_check, interval=new_seconds, first=new_seconds, name="scheduled_check")
        await update.message.reply_text(f"⏱ Updates set to every **{mins} minutes**.")
    except:
        await update.message.reply_text("❌ Usage: `/frequency 10`")

async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await generate_report(), parse_mode=ParseMode.MARKDOWN)

async def targets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🎯 **CURRENT TARGETS**\n"]
    for asset, cfg in state["alerts"].items():
        if cfg["targets"]:
            t_list = [f"{i+1}. {'Above' if t['above'] else 'Below'} ${t['val']:,.2f}" for i, t in enumerate(cfg["targets"])]
            lines.append(f"• **{asset}**:\n{', '.join(t_list)}\n")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = str(datetime.now() - START_TIME).split('.')[0]
    freq = state["frequency"] // 60
    await update.message.reply_text(f"✅ **Online**\n⏱ **Uptime**: {uptime}\n🔔 **Frequency**: Every {freq} mins", parse_mode=ParseMode.MARKDOWN)

async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHAT_ID, text=await generate_report(), parse_mode=ParseMode.MARKDOWN)

async def self_ping(context: ContextTypes.DEFAULT_TYPE):
    if RENDER_URL:
        try:
            async with httpx.AsyncClient() as client:
                await client.get(RENDER_URL, timeout=10)
        except: pass

if __name__ == "__main__":
    threading.Thread(target=run_health_check_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("now", now_command))
    app.add_handler(CommandHandler("targets", targets_command))
    app.add_handler(CommandHandler("setalert", setalert_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("frequency", frequency_command))
    app.add_handler(CommandHandler("status", status_command))
    app.job_queue.run_repeating(scheduled_check, interval=state["frequency"], first=5, name="scheduled_check")
    app.job_queue.run_repeating(self_ping, interval=600, first=600)
    app.run_polling()
