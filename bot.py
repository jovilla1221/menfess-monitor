#!/usr/bin/env python3
"""MENFESS MONITOR (@monitorfessbot) — monitor channel menfess, notif keyword ke user."""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, sqlite3, threading, time, html
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import httpx
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6247336698"))
DB = Path(__file__).parent / "data" / "menfess.db"
INTERVAL = int(os.environ.get("INTERVAL", "30"))

# ── Default channels ──
DEFAULT_CHANNELS = [
    "JUNEFESS", "ASETFESS", "basewib", "basewtt", "BukaLapakBA",
    "dagangfess", "inibasewts", "basejajan", "Swalayan",
    "MoneyFess", "berdagangonline", "basewtb",
]

# ── DB ──
_LOCK = threading.RLock()
_CONN = None

def conn():
    global _CONN
    with _LOCK:
        if _CONN is None:
            Path(DB).parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(str(DB), timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            _CONN = c
        return _CONN

def init_db():
    with _LOCK:
        c = conn()
        c.executescript("""
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    keyword TEXT NOT NULL,
    channel_filter TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, keyword, channel_filter)
);
CREATE TABLE IF NOT EXISTS seen_posts (
    post_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS channels (
    name TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1
);
""")
        # seed default channels
        for ch in DEFAULT_CHANNELS:
            c.execute("INSERT OR IGNORE INTO channels(name) VALUES(?)", (ch,))
        c.commit()

def get_channels() -> List[str]:
    with _LOCK:
        return [r["name"] for r in conn().execute("SELECT name FROM channels WHERE enabled=1").fetchall()]

def add_channel(name: str):
    with _LOCK:
        conn().execute("INSERT OR IGNORE INTO channels(name,enabled) VALUES(?,1)", (name,))
        conn().execute("UPDATE channels SET enabled=1 WHERE name=?", (name,))
        conn().commit()

def remove_channel(name: str):
    with _LOCK:
        conn().execute("UPDATE channels SET enabled=0 WHERE name=?", (name,))
        conn().commit()

def add_keyword(uid: int, kw: str, ch: str = ""):
    with _LOCK:
        conn().execute("INSERT OR IGNORE INTO keywords(user_id, keyword, channel_filter) VALUES(?,?,?)", (uid, kw.lower(), ch.lower()))
        conn().commit()

def remove_keyword(uid: int, kid: int) -> bool:
    with _LOCK:
        cur = conn().execute("DELETE FROM keywords WHERE id=? AND user_id=?", (kid, uid))
        conn().commit()
        return cur.rowcount > 0

def list_keywords(uid: int) -> List[dict]:
    with _LOCK:
        return [dict(r) for r in conn().execute("SELECT * FROM keywords WHERE user_id=? ORDER BY id", (uid,)).fetchall()]

def is_seen(post_id: str) -> bool:
    with _LOCK:
        return conn().execute("SELECT 1 FROM seen_posts WHERE post_id=?", (post_id,)).fetchone() is not None

def mark_seen(post_id: str):
    with _LOCK:
        conn().execute("INSERT OR IGNORE INTO seen_posts(post_id) VALUES(?)", (post_id,))
        conn().execute("DELETE FROM seen_posts WHERE post_id NOT IN (SELECT post_id FROM seen_posts ORDER BY ROWID DESC LIMIT 500)")
        conn().commit()

# ── Scraper ──
async def scrape_channel(ch: str) -> List[Dict]:
    url = f"https://t.me/s/{ch}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept-Language": "id,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"@{ch} HTTP {resp.status_code}")
                return []
        except Exception as e:
            logger.warning(f"Scrape @{ch}: {e}")
            return []

    soup = BeautifulSoup(resp.text, "html.parser")
    messages = []
    now = int(time.time())

    for msg_div in soup.select(".tgme_widget_message_wrap"):
        post_el = msg_div.select_one("[data-post]")
        if not post_el:
            post_el = msg_div.select_one("[data-post-id]")
            if not post_el:
                continue
        raw_post = post_el.get("data-post") or post_el.get("data-post-id", "0")
        post_id = f"{ch}_{raw_post.replace('/', '_')}"
        if is_seen(post_id):
            continue

        text_el = msg_div.select_one(".tgme_widget_message_text")
        text = text_el.get_text(separator="\n", strip=True) if text_el else ""

        # extract link
        link_el = msg_div.select_one("a.tgme_widget_message_date")
        link = link_el.get("href", "") if link_el else ""

        # extract media
        media = bool(msg_div.select_one(".tgme_widget_message_photo, .tgme_widget_message_video, .tgme_widget_message_document"))

        if text:
            messages.append({
                "post_id": post_id,
                "channel": ch,
                "text": text[:2000],
                "link": link or f"https://t.me/{ch}/{raw_post.split('/')[-1] if '/' in raw_post else raw_post}",
                "media": media,
                "time": now,
            })
            mark_seen(post_id)

    return messages

# ── Match engine ──
def match_keywords(text: str, keywords: List[dict]) -> List[dict]:
    t = text.lower()
    hits = []
    for kw in keywords:
        if kw["keyword"] in t:
            if kw.get("channel_filter") and kw["channel_filter"] not in t:
                continue
            hits.append(kw)
    return hits

# ── Keyboards ──
MAIN_KB = ReplyKeyboardMarkup([
    ["➕ Tambah Keyword", "📋 Keyword Saya"],
    ["📡 Channel", "❓ Bantuan"],
], resize_keyboard=True)

CHANNEL_KB = ReplyKeyboardMarkup([
    ["📡 Daftar Channel", "➕ Tambah Channel", "❌ Hapus Channel"],
    ["🔙 Kembali"],
], resize_keyboard=True)

BACK = lambda uid: InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])

# ── Commands ──
async def start(upd, ctx):
    u = upd.effective_user
    await upd.message.reply_text(
        f"🔍 *Menfess Monitor*\n"
        f"Halo {u.first_name}! Pantau keyword di 12+ channel menfess.\n\n"
        f"`/add jual iphone` — pantau keyword\n"
        f"`/add basewib jual` — keyword di channel tertentu\n"
        f"`/list` — lihat keyword\n"
        f"`/del 3` — hapus keyword ke-3\n\n"
        f"Bot ngecek tiap {INTERVAL} detik.\n"
        f"Kalau ketemu keyword → DM kamu!",
        parse_mode="Markdown", reply_markup=MAIN_KB)

async def cmd_add(upd, ctx):
    u = upd.effective_user.id
    args = ctx.args
    if not args:
        ctx.user_data["mode"] = "add"
        await upd.message.reply_text("✏️ Ketik keyword:\n`jual iphone`\nAtau: `basewib jual iphone` (channel + keyword)", parse_mode="Markdown", reply_markup=MAIN_KB)
        return
    if len(args) >= 2:
        ch = args[0]
        kw = " ".join(args[1:])
    else:
        ch = ""
        kw = args[0]
    add_keyword(u, kw, ch)
    await upd.message.reply_text(f"✅ Keyword *{kw}* ditambahkan{' di @'+ch if ch else ''}!", parse_mode="Markdown", reply_markup=MAIN_KB)

async def cmd_list(upd, ctx):
    u = upd.effective_user.id
    kws = list_keywords(u)
    if not kws:
        await upd.message.reply_text("📋 Belum ada keyword. `/add kata`", parse_mode="Markdown", reply_markup=MAIN_KB)
        return
    txt = "📋 *Keyword Saya*\n"
    for i, kw in enumerate(kws, 1):
        where = f" @{kw['channel_filter']}" if kw.get("channel_filter") else " (semua)"
        txt += f"\n{i}. `{kw['keyword']}`{where}"
    txt += f"\n\nTotal: {len(kws)} keyword"
    await upd.message.reply_text(txt, parse_mode="Markdown", reply_markup=MAIN_KB)

async def cmd_del(upd, ctx):
    u = upd.effective_user.id
    if not ctx.args:
        await upd.message.reply_text("❌ `/del 3` — hapus keyword ke-3", reply_markup=MAIN_KB)
        return
    try:
        kid = int(ctx.args[0])
        kws = list_keywords(u)
        if kid < 1 or kid > len(kws):
            await upd.message.reply_text(f"❌ Keyword #{kid} tidak ada", reply_markup=MAIN_KB)
            return
        remove_keyword(u, kws[kid - 1]["id"])
        await upd.message.reply_text(f"✅ Keyword #{kid} dihapus.", reply_markup=MAIN_KB)
    except:
        await upd.message.reply_text("❌ `/del 3`", reply_markup=MAIN_KB)

async def cmd_channels(upd, ctx):
    chs = get_channels()
    txt = "📡 *Channel yang dipantau*\n" + "\n".join(f"• @{c}" for c in chs)
    await upd.message.reply_text(txt, parse_mode="Markdown", reply_markup=CHANNEL_KB)

async def cmd_add_channel(upd, ctx):
    await upd.message.reply_text("✏️ Ketik username channel:\nContoh: `basewib` atau `@basewib`\n\nKalau sudah, bot mulai pantau channel itu.", reply_markup=CHANNEL_KB)
    ctx.user_data["mode"] = "add_ch"

async def cmd_del_channel(upd, ctx):
    await upd.message.reply_text("✏️ Ketik username channel yang mau dihapus:\nContoh: `basewib`", reply_markup=CHANNEL_KB)
    ctx.user_data["mode"] = "del_ch"

# ── Text Router ──
async def on_text(upd, ctx):
    if not upd.message or not upd.message.text: return
    u = upd.effective_user.id
    txt = upd.message.text.strip()

    mode = ctx.user_data.get("mode")

    # Menu buttons
    if txt == "➕ Tambah Keyword":
        ctx.user_data["mode"] = "add"
        await upd.message.reply_text("✏️ Ketik keyword:\n`jual iphone`\nAtau: `basewib jual` (filter channel)", parse_mode="Markdown")
        return
    if txt == "📋 Keyword Saya":
        await cmd_list(upd, ctx)
        return
    if txt == "📡 Channel":
        await cmd_channels(upd, ctx)
        return
    if txt == "📡 Daftar Channel":
        await cmd_channels(upd, ctx)
        return
    if txt == "➕ Tambah Channel":
        ctx.user_data["mode"] = "add_ch"
        await upd.message.reply_text("✏️ Username channel:\n`basewib`")
        return
    if txt == "❌ Hapus Channel":
        ctx.user_data["mode"] = "del_ch"
        await upd.message.reply_text("✏️ Username channel:\n`basewib`")
        return
    if txt == "🔙 Kembali":
        ctx.user_data.clear()
        await upd.message.reply_text("🏠 Menu", reply_markup=MAIN_KB)
        return
    if txt == "❓ Bantuan":
        await start(upd, ctx)
        return

    if mode == "add":
        parts = txt.split()
        if len(parts) >= 2 and not parts[0][0].isalnum() or False:
            ch, kw = parts[0].lstrip("@"), " ".join(parts[1:])
            add_keyword(u, kw, ch)
            await upd.message.reply_text(f"✅ Keyword *{kw}* di @{ch}!", parse_mode="Markdown", reply_markup=MAIN_KB)
        else:
            kw = txt
            add_keyword(u, kw, "")
            await upd.message.reply_text(f"✅ Keyword *{kw}* (semua channel)!", parse_mode="Markdown", reply_markup=MAIN_KB)
        ctx.user_data.pop("mode", None)
        return

    if mode == "add_ch":
        ch = txt.lstrip("@")
        add_channel(ch)
        await upd.message.reply_text(f"✅ @{ch} ditambahkan ke pantauan!", reply_markup=MAIN_KB)
        ctx.user_data.pop("mode", None)
        return

    if mode == "del_ch":
        ch = txt.lstrip("@").lower()
        remove_channel(ch)
        await upd.message.reply_text(f"✅ @{ch} dihapus dari pantauan.", reply_markup=MAIN_KB)
        ctx.user_data.pop("mode", None)
        return

    # Heuristic add
    kws = list_keywords(u)
    if kws:
        ctx.user_data["mode"] = "add"
        await upd.message.reply_text("✏️ Ketik keyword baru, atau gunakan menu di atas.", reply_markup=MAIN_KB)
        return

    await upd.message.reply_text("Pilih menu di bawah.", reply_markup=MAIN_KB)

# ── Cron Monitor ──
async def monitor_cron(ctx: ContextTypes.DEFAULT_TYPE):
    channels = get_channels()
    all_kws = []  # cache: we query fresh each time
    if not channels:
        return

    for ch in channels:
        try:
            posts = await scrape_channel(ch)
            if not posts:
                continue
            logger.info(f"@{ch}: {len(posts)} new posts")

            # Get all active keywords
            with _LOCK:
                all_kws = [dict(r) for r in conn().execute("SELECT * FROM keywords").fetchall()]

            for post in posts:
                hits = match_keywords(post["text"], all_kws)
                if not hits:
                    continue
                # Group hits by user
                user_hits = {}
                for kw in hits:
                    uid = kw["user_id"]
                    if uid not in user_hits:
                        user_hits[uid] = []
                    user_hits[uid].append(kw["keyword"])

                for uid, matched_keywords in user_hits.items():
                    kw_list = "`, `".join(matched_keywords)
                    await ctx.bot.send_message(
                        uid,
                        f"🔔 *Match!* `{kw_list}`\n"
                        f"📢 @{ch}\n\n"
                        f"{post['text'][:280]}..." if len(post['text']) > 280 else f"{post['text']}\n"
                        f"{'📸 Media' if post['media'] else ''}\n"
                        f"🔗 [Lihat Pesan]({post['link']})",
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
        except Exception as e:
            logger.error(f"Monitor @{ch}: {e}")
            continue

    # Prune old keywords
    with _LOCK:
        conn().execute("DELETE FROM seen_posts WHERE post_id NOT IN (SELECT post_id FROM seen_posts ORDER BY ROWID DESC LIMIT 1000)")
        conn().commit()

# ── Callbacks ──
async def callback(upd, ctx):
    q = upd.callback_query
    await q.answer()
    if q.data == "menu":
        ctx.user_data.clear()
        await q.edit_message_text("🏠 Menu", reply_markup=MAIN_KB)

# ── Admin ──
async def cmd_admin(upd, ctx):
    if upd.effective_user.id != ADMIN_ID: return
    with conn() as c:
        keywords = c.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
        users = c.execute("SELECT COUNT(DISTINCT user_id) FROM keywords").fetchone()[0]
        channels = c.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        seen = c.execute("SELECT COUNT(*) FROM seen_posts").fetchone()[0]
    await upd.message.reply_text(
        f"🛡️ *Admin*\n👥 User: {users}\n🔑 Keyword: {keywords}\n📡 Channel: {channels}\n📦 Seen: {seen}\n/stop — stop bot",
        parse_mode="Markdown")

# ── Error ──
async def error(upd, ctx):
    logger.exception("Error: %s", ctx.error)

# ── Main ──
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing")
    init_db()
    logger.info(f"🧵 Menfess Monitor starting — {len(get_channels())} channels, interval {INTERVAL}s")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("channel", cmd_channels))
    app.add_handler(CommandHandler("addchannel", cmd_add_channel))
    app.add_handler(CommandHandler("delchannel", cmd_del_channel))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_error_handler(error)

    # Cron
    app.job_queue.run_repeating(monitor_cron, interval=INTERVAL, first=15)

    logger.info("🔍 Menfess Monitor running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()