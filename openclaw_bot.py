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

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
MODEL = "deepseek/deepseek-chat"
MAX_HISTORY = 30
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = f"""You are OpenClaw, an elite AI agent built for one user — your owner.

YOUR CORE MISSIONS:
1. RESEARCH — Deep research on any topic, fast and accurate
2. OPPORTUNITY SCOUTING — Actively identify investment opportunities, business ideas, crypto trends, emerging markets, arbitrage, side hustles, and any lead worth pursuing
3. DAILY ASSISTANT — Help with writing, assignments, scheduling, reminders, analysis, social media content
4. DATA ANALYSIS — Break down numbers, trends, and data into clear insights
5. WEB SEARCH — You have real-time web search. Use it proactively

OPPORTUNITY SCOUTING MINDSET:
- Always think like an investor and entrepreneur
- Look for: undervalued assets, trending niches, growing markets, early signals
- Consider: crypto, stocks, real estate, dropshipping, freelancing, content, AI tools, arbitrage
- Give concrete actionable leads, not vague advice
- Rate opportunities: Risk level, Effort needed, Potential return

PERSONALITY:
- Sharp, direct, no fluff
- Proactive — if you notice something useful, mention it
- Think like a street-smart analyst + elite researcher
- Always give your best recommendation

RESPONSE FORMAT:
- Clear and structured
- Use bullet points for lists
- Bold key insights with *text*
- For opportunities: what it is, why now, how to start, risk level

Today: {datetime.now().strftime("%A, %B %d, %Y")}"""

conversations: dict = {}
reminders: dict = {}

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
                "trending","scout","opportunity","invest","market","crypto","stock","research",
                "what is","who is","how much","best way","should i"]
    return any(k in text.lower() for k in keywords)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text.strip()
    if user_id not in conversations:
        conversations[user_id] = []
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
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
        reply = "Connection error. Try again in a moment."
    conversations[user_id].append({"role": "assistant", "content": reply})
    for i in range(0, len(reply), 4000):
        await update.message.reply_text(reply[i:i+4000])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "OpenClaw Agent online.\n\n"
        "Your personal AI for research, opportunity scouting, writing, analysis and daily tasks.\n\n"
        "Commands:\n"
        "/scout [topic] - scout opportunities\n"
        "/research [topic] - deep research\n"
        "/remind 30m [task] - set reminder\n"
        "/reminders - list reminders\n"
        "/clear - reset memory\n"
        "/help - this menu\n\n"
        "Or just talk to me naturally. What do you need?"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "OpenClaw Help:\n\n"
        "/scout crypto - scout crypto opportunities\n"
        "/scout dropshipping - find business ideas\n"
        "/research AI trends 2026 - deep research\n"
        "/remind 30m Check email\n"
        "/remind 2h Call client\n"
        "/remind 1d Submit report\n"
        "/reminders - see active reminders\n"
        "/clear - wipe memory\n\n"
        "Just talk normally for anything else - web search is automatic!"
    )

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations[update.effective_user.id] = []
    await update.message.reply_text("Memory cleared.")

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

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("scout", scout))
    app.add_handler(CommandHandler("research", research))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("reminders", list_reminders))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("OpenClaw Agent starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
