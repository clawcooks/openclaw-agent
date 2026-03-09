import os
import json
import logging
import asyncio
import httpx
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from duckduckgo_search import DDGS

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
OPENROUTER_KEY     = os.environ["OPENROUTER_KEY"]
FORTYTWO_AGENT_ID  = os.environ.get("FORTYTWO_AGENT_ID", "")
FORTYTWO_TOKEN     = os.environ.get("FORTYTWO_ACCESS_TOKEN", "")
FORTYTWO_REFRESH   = os.environ.get("FORTYTWO_REFRESH_TOKEN", "")

MODEL          = "deepseek/deepseek-chat"
MAX_HISTORY    = 30
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
FORTYTWO_API   = "https://app.fortytwo.network/api"

SYSTEM_PROMPT = f"""You are OpenClaw (ClawBot), an elite AI agent built for one user — your owner.

YOUR CORE MISSIONS:
1. RESEARCH — Deep research on any topic, fast and accurate
2. OPPORTUNITY SCOUTING — Identify investment opportunities, business ideas, crypto trends, emerging markets, arbitrage, side hustles
3. DAILY ASSISTANT — Writing, assignments, scheduling, reminders, analysis, social media content
4. DATA ANALYSIS — Break down numbers, trends, data into clear insights
5. WEB SEARCH — Real-time web search built in, used automatically
6. FORTYTWO SWARM — Route complex questions to 21,000+ agent swarm for consensus answers

OPPORTUNITY SCOUTING MINDSET:
- Think like an investor and entrepreneur
- Look for: undervalued assets, trending niches, growing markets, early signals
- Consider: crypto, stocks, real estate, dropshipping, freelancing, content, AI tools, arbitrage
- Give concrete actionable leads — rate each: Risk level, Effort, Potential return

PERSONALITY:
- Sharp, direct, no fluff
- Proactive — mention useful things even if not asked
- Street-smart analyst + elite researcher

RESPONSE FORMAT:
- Clear and structured with bullet points
- Bold key insights with *text*
- For opportunities: what it is, why now, how to start, risk level

Today: {datetime.now().strftime("%A, %B %d, %Y")}"""

conversations: dict = {}
reminders: dict = {}
fortytwo_token_cache = {"token": FORTYTWO_TOKEN, "refresh": FORTYTWO_REFRESH}

# ── Web search ────────────────────────────────────────────────────────────────
def web_search(query: str, max_results: int = 5) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        return "\n".join([f"- {r['title']}: {r['body'][:250]}" for r in results])
    except Exception as e:
        logger.error(f"Search error: {e}")
        return "Search unavailable."

# ── OpenRouter / DeepSeek ─────────────────────────────────────────────────────
async def call_deepseek(messages: list) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://openclaw.agent",
        "X-Title": "OpenClaw Agent"
    }
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "max_tokens": 2000,
        "temperature": 0.7
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

# ── Fortytwo Swarm ────────────────────────────────────────────────────────────
async def refresh_fortytwo_token() -> bool:
    """Refresh Fortytwo access token using refresh token."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{FORTYTWO_API}/auth/refresh",
                json={"refreshToken": fortytwo_token_cache["refresh"]},
                headers={"Content-Type": "application/json"}
            )
            if resp.status_code == 200:
                data = resp.json()
                fortytwo_token_cache["token"] = data.get("accessToken", fortytwo_token_cache["token"])
                return True
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
    return False

async def ask_fortytwo_swarm(question: str) -> str:
    """Submit a question to the Fortytwo swarm and get consensus answer."""
    if not FORTYTWO_AGENT_ID or not fortytwo_token_cache["token"]:
        return "Fortytwo swarm not configured. Check Railway variables."

    headers = {
        "Authorization": f"Bearer {fortytwo_token_cache['token']}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Submit query to swarm
            resp = await client.post(
                f"{FORTYTWO_API}/queries",
                json={
                    "question": question,
                    "agentId": FORTYTWO_AGENT_ID,
                    "mode": "consensus"
                },
                headers=headers
            )

            # Try token refresh if unauthorized
            if resp.status_code == 401:
                refreshed = await refresh_fortytwo_token()
                if refreshed:
                    headers["Authorization"] = f"Bearer {fortytwo_token_cache['token']}"
                    resp = await client.post(
                        f"{FORTYTWO_API}/queries",
                        json={"question": question, "agentId": FORTYTWO_AGENT_ID, "mode": "consensus"},
                        headers=headers
                    )

            if resp.status_code in (200, 201):
                data = resp.json()
                query_id = data.get("id") or data.get("queryId")
                if not query_id:
                    return f"Query submitted but no ID returned: {json.dumps(data)[:200]}"

                # Poll for result (up to 45 seconds)
                for _ in range(9):
                    await asyncio.sleep(5)
                    result_resp = await client.get(
                        f"{FORTYTWO_API}/queries/{query_id}",
                        headers=headers
                    )
                    if result_resp.status_code == 200:
                        result = result_resp.json()
                        status = result.get("status", "")
                        if status in ("completed", "done", "finished"):
                            answer = result.get("topAnswer") or result.get("answer") or result.get("result")
                            if answer:
                                return f"Swarm consensus answer:\n\n{answer}"
                        elif status == "failed":
                            return "Swarm query failed. Try again."

                return "Swarm is still processing. Check app.fortytwo.network for results."
            else:
                return f"Swarm error {resp.status_code}: {resp.text[:200]}"

    except Exception as e:
        logger.error(f"Fortytwo error: {e}")
        return f"Swarm connection error: {str(e)[:100]}"

async def fortytwo_heartbeat() -> str:
    """Send heartbeat to keep Fortytwo agent alive."""
    if not FORTYTWO_AGENT_ID or not fortytwo_token_cache["token"]:
        return "Not configured"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{FORTYTWO_API}/agents/{FORTYTWO_AGENT_ID}/heartbeat",
                headers={"Authorization": f"Bearer {fortytwo_token_cache['token']}"}
            )
            return "alive" if resp.status_code in (200, 204) else f"status {resp.status_code}"
    except Exception as e:
        return f"error: {e}"

# ── Auto heartbeat task ───────────────────────────────────────────────────────
async def heartbeat_loop():
    """Keep Fortytwo agent alive by sending heartbeat every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        status = await fortytwo_heartbeat()
        logger.info(f"Fortytwo heartbeat: {status}")

# ── Message handler ───────────────────────────────────────────────────────────
def needs_search(text: str) -> bool:
    keywords = ["search","find","look up","latest","news","current","today","price",
                "trending","scout","opportunity","invest","market","crypto","stock",
                "research","what is","who is","how much","best way","should i"]
    return any(k in text.lower() for k in keywords)

def needs_swarm(text: str) -> bool:
    keywords = ["swarm","fortytwo","consensus","multiple perspectives","what do agents think",
                "ask the network","complex question","controversial","debated"]
    return any(k in text.lower() for k in keywords)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text.strip()
    if user_id not in conversations:
        conversations[user_id] = []
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Auto-route to swarm if needed
    if needs_swarm(user_text):
        await update.message.reply_text("Routing to Fortytwo swarm...")
        swarm_result = await ask_fortytwo_swarm(user_text)
        await update.message.reply_text(swarm_result[:4000])
        return

    full_content = user_text
    if needs_search(user_text):
        results = web_search(user_text)
        full_content = f"{user_text}\n\n[LIVE WEB SEARCH]:\n{results}"

    conversations[user_id].append({"role": "user", "content": full_content})
    if len(conversations[user_id]) > MAX_HISTORY:
        conversations[user_id] = conversations[user_id][-MAX_HISTORY:]

    try:
        reply = await call_deepseek(conversations[user_id])
    except Exception as e:
        logger.error(f"API error: {e}")
        reply = "Connection error. Try again."

    conversations[user_id].append({"role": "assistant", "content": reply})
    for i in range(0, len(reply), 4000):
        await update.message.reply_text(reply[i:i+4000])

# ── Commands ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    swarm_status = "connected" if FORTYTWO_AGENT_ID else "not configured"
    await update.message.reply_text(
        f"OpenClaw (ClawBot) online.\n"
        f"Fortytwo Swarm: {swarm_status}\n\n"
        "Your personal AI for research, opportunity scouting, writing, analysis and daily tasks.\n\n"
        "Commands:\n"
        "/swarm [question] - ask 21,000+ agent swarm\n"
        "/scout [topic] - scout opportunities\n"
        "/research [topic] - deep research\n"
        "/remind 30m [task] - set reminder\n"
        "/reminders - list reminders\n"
        "/status - check agent status\n"
        "/clear - reset memory\n\n"
        "Talk naturally — web search is automatic!"
    )

async def swarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = " ".join(context.args) if context.args else None
    if not question:
        await update.message.reply_text("Usage: /swarm What is the best crypto to hold in 2026?")
        return
    await update.message.reply_text("Sending to Fortytwo swarm...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    result = await ask_fortytwo_swarm(question)
    await update.message.reply_text(result[:4000])

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hb = await fortytwo_heartbeat()
    await update.message.reply_text(
        f"OpenClaw Status:\n"
        f"AI Model: DeepSeek V3 (OpenRouter)\n"
        f"Fortytwo Agent ID: {FORTYTWO_AGENT_ID[:8]}...\n"
        f"Fortytwo Heartbeat: {hb}\n"
        f"Web Search: active\n"
        f"Hosting: Railway 24/7"
    )

async def scout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args) if context.args else "investment opportunities 2026"
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    results = web_search(f"{topic} opportunities 2026 trending profitable")
    prompt = f"Scout opportunities for: {topic}\n\nWeb data:\n{results}\n\nGive 3-5 concrete opportunities with: what it is, why now, how to start, risk level (low/med/high), potential return."
    uid = update.effective_user.id
    if uid not in conversations: conversations[uid] = []
    conversations[uid].append({"role": "user", "content": prompt})
    try:
        reply = await call_deepseek(conversations[uid])
    except Exception as e:
        reply = f"Error: {e}"
    conversations[uid].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply[:4000])

async def research(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args) if context.args else "tech trends 2026"
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    results = web_search(topic, max_results=8)
    prompt = f"Deep research on: {topic}\n\nWeb data:\n{results}\n\nComprehensive analysis: key insights, trends, data points, actionable takeaways."
    uid = update.effective_user.id
    if uid not in conversations: conversations[uid] = []
    conversations[uid].append({"role": "user", "content": prompt})
    try:
        reply = await call_deepseek(conversations[uid])
    except Exception as e:
        reply = f"Error: {e}"
    conversations[uid].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply[:4000])

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /remind 30m Check email\n/remind 2h Meeting\n/remind 1d Report due")
        return
    time_str = context.args[0].lower()
    task = " ".join(context.args[1:])
    uid = update.effective_user.id
    seconds = 0
    if time_str.endswith("m"): seconds = int(time_str[:-1]) * 60
    elif time_str.endswith("h"): seconds = int(time_str[:-1]) * 3600
    elif time_str.endswith("d"): seconds = int(time_str[:-1]) * 86400
    else:
        await update.message.reply_text("Use: 30m, 2h, or 1d")
        return
    if uid not in reminders: reminders[uid] = []
    reminders[uid].append({"task": task, "time": time_str})
    await update.message.reply_text(f"Reminder set: '{task}' in {time_str}")
    async def fire():
        await asyncio.sleep(seconds)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"REMINDER: {task}")
        if uid in reminders:
            reminders[uid] = [r for r in reminders[uid] if r["task"] != task]
    asyncio.create_task(fire())

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in reminders or not reminders[uid]:
        await update.message.reply_text("No active reminders.")
        return
    lines = [f"- {r['task']} (in {r['time']})" for r in reminders[uid]]
    await update.message.reply_text("Active reminders:\n" + "\n".join(lines))

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations[update.effective_user.id] = []
    await update.message.reply_text("Memory cleared.")

async def post_init(app):
    asyncio.create_task(heartbeat_loop())
    logger.info("Fortytwo heartbeat loop started")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("swarm", swarm_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("scout", scout))
    app.add_handler(CommandHandler("research", research))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("reminders", list_reminders))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("OpenClaw Agent starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
