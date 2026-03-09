import os
import json
import logging
import asyncio
import httpx
import base64
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from duckduckgo_search import DDGS

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN         = os.environ["TELEGRAM_TOKEN"]
OPENROUTER_KEY         = os.environ["OPENROUTER_KEY"]
FORTYTWO_AGENT_ID      = os.environ.get("FORTYTWO_AGENT_ID", "")
FORTYTWO_AGENT_SECRET  = os.environ.get("FORTYTWO_AGENT_SECRET", "")
FORTYTWO_ACCESS_TOKEN  = os.environ.get("FORTYTWO_ACCESS_TOKEN", "")
FORTYTWO_REFRESH_TOKEN = os.environ.get("FORTYTWO_REFRESH_TOKEN", "")

MODEL          = "deepseek/deepseek-chat"
MAX_HISTORY    = 30
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
FORTYTWO_BASE  = "https://app.fortytwo.network/api"

SYSTEM_PROMPT = f"""You are OpenClaw (ClawBot), an elite AI agent built for one user.

YOUR CORE MISSIONS:
1. RESEARCH — Deep research on any topic, fast and accurate
2. OPPORTUNITY SCOUTING — Investment opportunities, business ideas, crypto trends, arbitrage, side hustles
3. DAILY ASSISTANT — Writing, assignments, scheduling, reminders, analysis, social media content
4. DATA ANALYSIS — Break down numbers, trends, data into clear insights
5. WEB SEARCH — Real-time web search built in
6. FORTYTWO SWARM — Route complex questions to 21,000+ agent swarm

OPPORTUNITY SCOUTING MINDSET:
- Think like an investor and entrepreneur
- Look for: undervalued assets, trending niches, growing markets, early signals
- Consider: crypto, stocks, real estate, dropshipping, freelancing, content, AI tools
- Rate each: Risk level, Effort, Potential return

PERSONALITY: Sharp, direct, no fluff. Proactive. Street-smart analyst + elite researcher.

Today: {datetime.now().strftime("%A, %B %d, %Y")}"""

conversations: dict = {}
reminders: dict = {}

# ── Fortytwo token state ──────────────────────────────────────────────────────
ft_state = {
    "access_token": FORTYTWO_ACCESS_TOKEN,
    "refresh_token": FORTYTWO_REFRESH_TOKEN
}

# ── Fortytwo API ──────────────────────────────────────────────────────────────
def ft_headers():
    return {
        "Authorization": f"Bearer {ft_state['access_token']}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://app.fortytwo.network"
    }

async def ft_refresh():
    """Refresh Fortytwo access token."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{FORTYTWO_BASE}/auth/refresh",
                json={"refreshToken": ft_state["refresh_token"]},
                headers={"Content-Type": "application/json", "HTTP-Referer": "https://app.fortytwo.network"}
            )
            if resp.status_code == 200:
                data = resp.json()
                ft_state["access_token"] = data.get("accessToken", ft_state["access_token"])
                ft_state["refresh_token"] = data.get("refreshToken", ft_state["refresh_token"])
                logger.info("Fortytwo token refreshed")
                return True
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
    return False

async def ft_login():
    """Login to Fortytwo using agent ID and secret."""
    if not FORTYTWO_AGENT_ID or not FORTYTWO_AGENT_SECRET:
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{FORTYTWO_BASE}/auth/login",
                json={"agentId": FORTYTWO_AGENT_ID, "secret": FORTYTWO_AGENT_SECRET},
                headers={"Content-Type": "application/json", "HTTP-Referer": "https://app.fortytwo.network"}
            )
            if resp.status_code == 200:
                data = resp.json()
                ft_state["access_token"] = data.get("accessToken", "")
                ft_state["refresh_token"] = data.get("refreshToken", "")
                logger.info("Fortytwo login successful")
                return True
            logger.error(f"Fortytwo login failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Fortytwo login error: {e}")
    return False

async def ft_request(method, path, json_data=None, retry=True):
    """Make authenticated request to Fortytwo API."""
    if not ft_state["access_token"]:
        await ft_login()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method,
                f"{FORTYTWO_BASE}{path}",
                json=json_data,
                headers=ft_headers()
            )
            if resp.status_code == 401 and retry:
                refreshed = await ft_refresh()
                if not refreshed:
                    await ft_login()
                return await ft_request(method, path, json_data, retry=False)
            return resp
    except Exception as e:
        logger.error(f"Fortytwo request error: {e}")
        return None

async def ask_fortytwo_swarm(question: str) -> str:
    """Submit question to Fortytwo swarm and get answer."""
    if not FORTYTWO_AGENT_ID:
        return "Fortytwo not configured. Add FORTYTWO_AGENT_ID and FORTYTWO_AGENT_SECRET to Railway variables."

    # Encode question as base64 (as the real CLI does)
    encrypted = base64.b64encode(question.encode("utf-8")).decode("utf-8")

    # Create query
    resp = await ft_request("POST", "/queries", {"encryptedContent": encrypted, "specialization": "general"})
    if not resp or resp.status_code not in (200, 201):
        err = resp.text[:200] if resp else "no response"
        return f"Swarm error creating query: {err}"

    data = resp.json()
    query_id = data.get("id") or data.get("queryId")
    if not query_id:
        return f"Query created but no ID: {str(data)[:200]}"

    # Join the query
    await ft_request("POST", f"/queries/{query_id}/join")

    # Poll for results (up to 60 seconds)
    for i in range(12):
        await asyncio.sleep(5)
        result_resp = await ft_request("GET", f"/queries/{query_id}")
        if not result_resp:
            continue
        if result_resp.status_code == 200:
            result = result_resp.json()
            status = result.get("status", "")
            if status in ("completed", "done", "finished", "closed"):
                answers = result.get("answers") or result.get("topAnswers") or []
                if answers:
                    top = answers[0]
                    content = top.get("content") or top.get("encryptedContent", "")
                    try:
                        decoded = base64.b64decode(content).decode("utf-8")
                    except Exception:
                        decoded = content
                    return f"Fortytwo Swarm Answer:\n\n{decoded}"
                raw = result.get("topAnswer") or result.get("answer") or result.get("result")
                if raw:
                    try:
                        decoded = base64.b64decode(raw).decode("utf-8")
                    except Exception:
                        decoded = raw
                    return f"Fortytwo Swarm Answer:\n\n{decoded}"

    return f"Swarm query submitted (ID: {query_id[:8]}...) — still processing. Check app.fortytwo.network for results."

async def ft_balance() -> str:
    resp = await ft_request("GET", "/balance")
    if resp and resp.status_code == 200:
        data = resp.json()
        bal = data.get("balance") or data.get("for") or data.get("amount") or str(data)
        return str(bal)
    return "unknown"

# ── Heartbeat loop ────────────────────────────────────────────────────────────
async def heartbeat_loop():
    while True:
        await asyncio.sleep(300)
        try:
            resp = await ft_request("GET", "/agent")
            logger.info(f"Fortytwo heartbeat: {resp.status_code if resp else 'failed'}")
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")

# ── Web search ────────────────────────────────────────────────────────────────
def web_search(query: str, max_results: int = 5) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        return "\n".join([f"- {r['title']}: {r['body'][:250]}" for r in results])
    except Exception as e:
        return f"Search unavailable: {e}"

# ── DeepSeek ──────────────────────────────────────────────────────────────────
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

def needs_search(text: str) -> bool:
    keywords = ["search","find","look up","latest","news","current","today","price",
                "trending","scout","opportunity","invest","market","crypto","stock",
                "research","what is","who is","how much","best way","should i"]
    return any(k in text.lower() for k in keywords)

def needs_swarm(text: str) -> bool:
    keywords = ["swarm","fortytwo","consensus","ask the network","multiple perspectives",
                "what do agents think","complex","controversial"]
    return any(k in text.lower() for k in keywords)

# ── Message handler ───────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text.strip()
    if user_id not in conversations:
        conversations[user_id] = []
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    if needs_swarm(user_text):
        await update.message.reply_text("Routing to Fortytwo swarm...")
        result = await ask_fortytwo_swarm(user_text)
        await update.message.reply_text(result[:4000])
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
        reply = f"Error: {e}"

    conversations[user_id].append({"role": "assistant", "content": reply})
    for i in range(0, len(reply), 4000):
        await update.message.reply_text(reply[i:i+4000])

# ── Commands ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "OpenClaw (ClawBot) online.\n\n"
        "Commands:\n"
        "/swarm [question] - ask Fortytwo swarm\n"
        "/balance - check FOR token balance\n"
        "/scout [topic] - scout opportunities\n"
        "/research [topic] - deep research\n"
        "/remind 30m [task] - set reminder\n"
        "/reminders - list reminders\n"
        "/status - agent status\n"
        "/clear - reset memory\n\n"
        "Talk naturally — web search is automatic!"
    )

async def swarm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = " ".join(context.args) if context.args else None
    if not question:
        await update.message.reply_text("Usage: /swarm What is the best crypto to hold in 2026?")
        return
    await update.message.reply_text("Sending to Fortytwo swarm... (takes ~30-60 seconds)")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    result = await ask_fortytwo_swarm(question)
    await update.message.reply_text(result[:4000])

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    bal = await ft_balance()
    await update.message.reply_text(f"Your Fortytwo FOR balance: {bal}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = await ft_balance()
    swarm = "connected" if FORTYTWO_AGENT_ID else "not configured"
    await update.message.reply_text(
        f"OpenClaw Status:\n"
        f"AI: DeepSeek V3 (OpenRouter)\n"
        f"Fortytwo: {swarm}\n"
        f"FOR Balance: {bal}\n"
        f"Web Search: active\n"
        f"Hosting: Railway 24/7"
    )

async def scout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args) if context.args else "investment opportunities 2026"
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    results = web_search(f"{topic} opportunities 2026 trending profitable")
    prompt = f"Scout opportunities for: {topic}\n\nWeb data:\n{results}\n\nGive 3-5 concrete opportunities with: what it is, why now, how to start, risk level, potential return."
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
    prompt = f"Deep research on: {topic}\n\nWeb data:\n{results}\n\nComprehensive analysis with key insights, trends, and actionable takeaways."
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
        await update.message.reply_text("Usage: /remind 30m Check email\n/remind 2h Meeting\n/remind 1d Report")
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
    app.add_handler(CommandHandler("balance", balance_cmd))
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
