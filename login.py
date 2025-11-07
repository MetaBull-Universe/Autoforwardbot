#everything is working some commands are not added , sometime you have to use /work command to re start the autoforwarding because you commited some changes
# Public (always on): /start, /help, /status, /login, /config, /upgrade, /upgrade_status, /logout
# Premium required: /incoming, /outgoing, /work, /stop, /remove_incoming, /remove_outgoing,
#                   /addfilter, /showfilter, /removefilter, /deleteallfilters, /delay
# Features:
# - OTP format: 123456 or HELLO123456
# - Handles Telegram 2FA password (with hint)
# - Top 14 chats selection via pinned dialogs
# - Multi-select with inline buttons (1..14)
# - Forwarding as USER (not bot), media+caption supported
# - Razorpay payment links (new link every time), verify button
# - Cumulative 30-day subscription extension
# ---------------------------------------------------

import os, re, asyncio, json, base64
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Set, Any, Optional

from dotenv import load_dotenv
from supabase import create_client, Client

import aiohttp  # async HTTP for Razorpay

from telethon import TelegramClient, events, errors, Button
from telethon.tl import functions
from telethon.tl.functions.bots import SetBotInfoRequest, SetBotCommandsRequest
from telethon.types import BotCommand, BotCommandScopeDefault
from telethon.utils import get_peer_id

load_dotenv()

API_ID        = int(os.getenv("API_ID", "0"))
API_HASH      = os.getenv("API_HASH", "")
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY", "")
SESSION_DIR   = os.getenv("SESSION_DIR", "sessions")
TOP_N         = 14
FORWARD_THROTTLE = 1.0  # seconds between sends to avoid spam & rate limits

# Razorpay / Plan
RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
PLAN_AMOUNT_PAISE   = int(os.getenv("PLAN_AMOUNT_PAISE", "29900"))  # 299 INR
PLAN_DURATION_DAYS  = int(os.getenv("PLAN_DURATION_DAYS", "30"))

assert API_ID and API_HASH and BOT_TOKEN, "Set API_ID, API_HASH, BOT_TOKEN in .env"
assert SUPABASE_URL and SUPABASE_KEY, "Set SUPABASE_URL, SUPABASE_KEY in .env"
assert RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET, "Set RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET in .env"

os.makedirs(SESSION_DIR, exist_ok=True)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = TelegramClient("login_bot_runner", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

PHONE_RE = re.compile(r"^\+\d{6,15}$", re.IGNORECASE)
OTP_RE   = re.compile(r"^(?:HELLO\s*)?(\d{4,8})$", re.IGNORECASE)  # 123456 or HELLO123456

# --- in-memory states ---
login_state: Dict[int, Dict[str, Any]]  = {}
select_state: Dict[int, Dict[str, Any]] = {}   # for incoming/outgoing & remove flows
forward_loops: Dict[int, Dict[str, Any]] = {}  # uid -> {"client": user_client}

# ---------------- COMMANDS (single source of truth) ----------------
COMMANDS: List[Tuple[str, str]] = [
    ("start", "Show all commands & how to use"),
    ("help", "Same as /start"),
    ("status", "Check login status"),
    ("login", "Login your Telegram account"),
    ("config", "View current mapping"),
    ("upgrade", "Buy/Renew Premium (₹299 / 30 days)"),
    ("upgrade_status", "Check subscription status"),
    ("logout", "Delete session & logout"),

    # premium-only below:
    ("incoming", "Select source chats (login + premium)"),
    ("outgoing", "Select target chats (login + premium)"),
    ("work", "Start auto-forward (login + premium)"),
    ("stop", "Stop auto-forward (login + premium)"),
    ("remove_incoming", "Remove saved sources (login + premium)"),
    ("remove_outgoing", "Remove saved targets (login + premium)"),
    ("addfilter", "Replace @left with @right in forwarded text/captions (premium)"),
    ("showfilter", "Show all saved replace filters (premium)"),
    ("removefilter", "Delete a filter by its left name (premium)"),
    ("deleteallfilters", "Delete all your text-replace filters (premium)"),
    ("delay", "Set send delay in seconds (0-999) (premium)"),
    ("removedelay", "Remove any set forwarding delay (premium)"),
    ("start_text", "Add a custom starting text to all forwarded messages (premium)"),
    ("end_text", "Add a custom ending text to all forwarded messages (premium)"),
    ("remove_text", "Remove saved start/end texts (premium)"),


]

def commands_text() -> str:
    lines = ["👋 **Welcome!** Here are all available commands:", ""]
    icons = {
        "start": "🏁", "help": "ℹ️", "status": "🧩", "login": "📱", "config": "📋",
        "upgrade": "💳", "upgrade_status": "📊", "logout": "🚪",
        "incoming": "📥", "outgoing": "📤", "work": "▶️", "stop": "⏸️",
        "remove_incoming": "❌", "remove_outgoing": "❌",
        "addfilter": "🧩", "showfilter": "🧾", "removefilter": "🗑️", "deleteallfilters": "🧨",
        "delay": "⏱️",
    }
    for cmd, desc in COMMANDS:
        lines.append(f"• {icons.get(cmd, '•')} /{cmd} — {desc}")
    lines.append("")
    lines.append("_Premium needed for forwarding, filters, remove/add targets, delay._")
    return "\n".join(lines)

# ---------------- SUPABASE HELPERS ----------------
# ---------- FILTERS ----------
# ---------- START/END TEXT HELPERS ----------
def sp_set_start_text(uid: int, text: str):
    payload = {
        "user_id": uid,
        "start_text": text.strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("user_text_addons").upsert(payload, on_conflict="user_id").execute()

def sp_set_end_text(uid: int, text: str):
    payload = {
        "user_id": uid,
        "end_text": text.strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("user_text_addons").upsert(payload, on_conflict="user_id").execute()

def sp_remove_texts(uid: int):
    supabase.table("user_text_addons").delete().eq("user_id", uid).execute()

def sp_get_text_addons(uid: int) -> dict:
    res = supabase.table("user_text_addons").select("*").eq("user_id", uid).limit(1).execute()
    return res.data[0] if res.data else {"start_text": "", "end_text": ""}



def sp_add_filter(uid: int, from_name: str, to_name: str) -> Tuple[bool, str]:
    from_name = from_name.strip(); to_name = to_name.strip()
    if not from_name or not to_name:
        return False, "⚠️ Dono words do: `/addfilter old==new`"

    # Optional guard: avoid exact-same mapping
    if from_name.lower() == to_name.lower():
        return False, "⚠️ Left aur right same nahi ho sakte."

    try:
        # ✅ Do NOT send generated columns in payload
        supabase.table("user_text_filters").insert({
            "user_id": uid,
            "from_name": from_name,
            "to_name": to_name,
        }).execute()

        return True, f"✅ Filter set: `{from_name}` → `{to_name}`"

    except Exception as ex:
        msg = str(ex).lower()
        # Duplicate/unique index on (user_id, from_name_lower)
        if ("duplicate" in msg or "unique" in msg or
            "idx_user_text_filters_unique" in msg or "23505" in msg):
            return False, ("❌ Same left name already exists for this user.\n"
                           "Try another left value or remove the old one via `/removefilter old`.")
        return False, f"❌ Failed: {ex}"


def sp_list_filters(uid: int) -> List[dict]:
    res = supabase.table("user_text_filters").select("*")\
        .eq("user_id", uid).order("created_at", desc=True).execute()
    return res.data or []

def sp_delete_filter(uid: int, from_name: str) -> Tuple[bool, str]:
    from_name = from_name.strip()
    if not from_name: return False, "⚠️ Use: `/removefilter old` (ya `@old`)"
    supabase.table("user_text_filters").delete()\
        .eq("user_id", uid).eq("from_name_lower", from_name.lower()).execute()
    check = supabase.table("user_text_filters").select("id").eq("user_id", uid)\
        .eq("from_name_lower", from_name.lower()).limit(1).execute()
    if check.data: return False, "❌ Could not remove (DB). Try again."
    return True, f"🗑️ Removed filter for `{from_name}`"

def sp_delete_filters_batch(uid: int, from_names: List[str]) -> int:
    if not from_names: return 0
    lowers = [fn.strip().lower() for fn in from_names if fn and fn.strip()]
    if not lowers: return 0
    supabase.table("user_text_filters").delete().eq("user_id", uid)\
        .in_("from_name_lower", lowers).execute()
    rem = supabase.table("user_text_filters").select("id").eq("user_id", uid)\
        .in_("from_name_lower", lowers).execute().data or []
    return max(0, len(lowers) - len(rem))

# ---------- FILTERS: Compile & Apply ----------
def compile_filters_for_user(uid: int) -> List[Tuple[re.Pattern, str]]:
    rows = sp_list_filters(uid)
    rows.sort(key=lambda r: len(r.get("from_name") or ""), reverse=True)
    compiled = []
    for r in rows:
        src = (r.get("from_name") or "").strip()
        dst = (r.get("to_name") or "").strip()
        if not src or not dst: continue
        if src.startswith("@"):
            name = re.escape(src[1:])
            pat = re.compile(rf"(?i)(?<!\w)@{name}(?!\w)")
        else:
            name = re.escape(src)
            pat = re.compile(rf"(?i)(?<!\w){name}(?!\w)")
        compiled.append((pat, dst))
    return compiled

def apply_text_filters(text: str, compiled_filters: List[Tuple[re.Pattern, str]]) -> str:
    if not text: return text
    out = text
    for pat, repl in compiled_filters:
        out = pat.sub(repl, out)
    return out

# ---------- Sessions / Mappings / Delay ----------
def sp_get_session(uid: int) -> Optional[dict]:
    res = supabase.table("user_sessions").select("*").eq("user_id", uid).limit(1).execute()
    return res.data[0] if res.data else None

def sp_upsert_session(uid: int, phone: str, session_file: str):
    payload = {
        "user_id": uid, "phone": phone, "session_file": session_file, "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("user_sessions").upsert(payload, on_conflict="user_id").execute()
    except Exception:
        existing = supabase.table("user_sessions").select("user_id").eq("user_id", uid).limit(1).execute()
        if existing and existing.data:
            supabase.table("user_sessions").update(payload).eq("user_id", uid).execute()
        else:
            supabase.table("user_sessions").insert(payload).execute()

def sp_delete_session(uid: int):
    supabase.table("user_sessions").delete().eq("user_id", uid).execute()

def sp_upsert_mapping(uid: int, sender_id: int, receivers: List[int]):
    payload = {
        "user_id": uid, "sender_id": int(sender_id), "receivers": receivers,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    try:
        supabase.table("forward_mappings").upsert(payload, on_conflict="user_id,sender_id").execute()
    except Exception:
        supabase.table("forward_mappings").delete().eq("user_id", uid).eq("sender_id", sender_id).execute()
        supabase.table("forward_mappings").insert(payload).execute()

def sp_load_rows(uid: int) -> List[dict]:
    return supabase.table("forward_mappings").select("*").eq("user_id", uid).execute().data or []

def sp_load_mapping(uid: int) -> Dict[int, List[int]]:
    mp: Dict[int, List[int]] = {}
    for r in sp_load_rows(uid):
        mp[int(r["sender_id"])] = list(r.get("receivers") or [])
    return mp

def sp_delete_senders(uid: int, sender_ids: List[int]):
    for sid in sender_ids:
        supabase.table("forward_mappings").delete().eq("user_id", uid).eq("sender_id", sid).execute()

def sp_remove_targets_globally(uid: int, target_ids: List[int]):
    rows = sp_load_rows(uid)
    kill = set(map(int, target_ids))
    for r in rows:
        rec = list(r.get("receivers") or [])
        new_rec = [x for x in rec if int(x) not in kill]
        if new_rec != rec:
            if new_rec:
                sp_upsert_mapping(uid, int(r["sender_id"]), new_rec)
            else:
                supabase.table("forward_mappings").delete().eq("user_id", uid).eq("sender_id", r["sender_id"]).execute()

def sp_delete_all_filters(uid: int) -> int:
    rows = supabase.table("user_text_filters").select("id").eq("user_id", uid).execute().data or []
    count = len(rows)
    if count:
        supabase.table("user_text_filters").delete().eq("user_id", uid).execute()
    return count

def sp_set_delay(uid: int, seconds: int):
    payload = {
        "user_id": uid, "delay_seconds": int(seconds),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("user_settings").upsert(payload, on_conflict="user_id").execute()
    except Exception:
        existing = supabase.table("user_settings").select("user_id").eq("user_id", uid).limit(1).execute()
        if existing and existing.data:
            supabase.table("user_settings").update(payload).eq("user_id", uid).execute()
        else:
            supabase.table("user_settings").insert(payload).execute()

def sp_get_delay(uid: int) -> Optional[int]:
    res = supabase.table("user_settings").select("delay_seconds").eq("user_id", uid).limit(1).execute()
    data = res.data or []
    if not data: return None
    try: return int(data[0].get("delay_seconds") or 0)
    except Exception: return None

# ---------- SUBSCRIPTION HELPERS ----------
def sp_get_subscription(uid: int) -> Optional[dict]:
    res = supabase.table("user_subscriptions").select("*").eq("user_id", uid).limit(1).execute()
    return res.data[0] if res.data else None

def sp_is_sub_active(sub: Optional[dict]) -> bool:
    if not sub: return False
    try:
        exp = datetime.fromisoformat(str(sub["expires_at"]).replace("Z", "+00:00"))
    except Exception:
        return False
    return exp > datetime.now(timezone.utc)

def sp_extend_subscription(uid: int, plus_days: int, payment_id: str, plink_id: str, plink_url: str):
    now = datetime.now(timezone.utc)
    sub = sp_get_subscription(uid)
    if sub and sp_is_sub_active(sub):
        start = datetime.fromisoformat(str(sub["started_at"]).replace("Z", "+00:00"))
        new_exp = datetime.fromisoformat(str(sub["expires_at"]).replace("Z", "+00:00")) + timedelta(days=plus_days)
        cycles = int(sub.get("total_cycles") or 0) + 1
    else:
        start = now
        new_exp = now + timedelta(days=plus_days)
        cycles = (int(sub.get("total_cycles") or 0) + 1) if sub else 1

    payload = {
        "user_id": uid,
        "started_at": start.isoformat(),
        "expires_at": new_exp.isoformat(),
        "total_cycles": cycles,
        "last_payment_id": payment_id,
        "last_paymentlink_id": plink_id,
        "last_paymentlink_url": plink_url,
        "updated_at": now.isoformat(),
    }
    try:
        supabase.table("user_subscriptions").upsert(payload, on_conflict="user_id").execute()
    except Exception:
        exists = supabase.table("user_subscriptions").select("user_id").eq("user_id", uid).limit(1).execute()
        if exists and exists.data:
            supabase.table("user_subscriptions").update(payload).eq("user_id", uid).execute()
        else:
            supabase.table("user_subscriptions").insert(payload).execute()
    return payload

# ---------------- TELEGRAM HELPERS ----------------
def session_path(uid: int, phone: str) -> str:
    digits = "".join([c for c in phone if c.isdigit()])
    return os.path.join(SESSION_DIR, f"{uid}_{digits}.session")

async def get_user_client(uid: int) -> TelegramClient:
    sess = sp_get_session(uid)
    if not sess:
        raise RuntimeError("No saved session. Use /login first.")
    
    local = os.path.join(SESSION_DIR, sess["session_file"])
    client = TelegramClient(local, API_ID, API_HASH)
    
    # ✅ safer connect (handles slow network or reconnects)
    await safe_connect(client)
    
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Session exists but not authorized. /login again.")
    
    return client

async def is_logged_in(uid: int) -> bool:
    try:
        uc = await get_user_client(uid)
        await uc.disconnect()
        return True
    except Exception:
        return False

def title_of(ent) -> str:
    if getattr(ent, "title", None): return ent.title
    fn = getattr(ent, "first_name", "") or ""
    ln = getattr(ent, "last_name", "") or ""
    if fn or ln: return (fn + " " + ln).strip()
    if getattr(ent, "username", None): return "@" + ent.username
    return f"id:{getattr(ent, 'id', '')}"

async def top_dialog_pairs(client: TelegramClient, limit: int = TOP_N) -> List[Tuple[int, str]]:
    dialogs = await client.get_dialogs(limit=200)
    return [(int(get_peer_id(d.entity)), title_of(d.entity)) for d in dialogs[:limit]]

async def titles_for_ids(client: TelegramClient, ids: List[int]) -> List[str]:
    names = []
    for _id in ids:
        try:
            ent = await client.get_entity(int(_id))
            names.append(title_of(ent))
        except Exception:
            names.append(f"id:{_id}")
    return names

def numbered_list_from_pairs(pairs: List[Tuple[int, str]]) -> str:
    return "\n".join([f"{i+1}. {pairs[i][1]}" for i in range(len(pairs))])

def multi_kb(n: int, selected: Set[int]) -> List[List[Button]]:
    rows, row = [], []
    for i in range(1, n + 1):
        label = f"{'✅ ' if (i - 1) in selected else ''}{i}"
        row.append(Button.inline(label, data=f"msel:{i}".encode()))
        if len(row) == 7:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([Button.inline("✅ Done", data=b"msel_done"),
                 Button.inline("✖ Cancel", data=b"msel_cancel")])
    return rows

# ---------------- RAZORPAY HELPERS ----------------
RP_BASE = "https://api.razorpay.com/v1"

def _rp_auth_headers():
    token = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

async def rp_create_payment_link(uid: int) -> Tuple[str, str]:
    payload = {
        "amount": PLAN_AMOUNT_PAISE,
        "currency": "INR",
        "description": f"Auto Forward Bot — {PLAN_DURATION_DAYS} days access for user {uid}",
        "notify": {"email": False, "sms": False},
        "reminder_enable": True,
        "expire_by": int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()),
        "notes": {"telegram_user_id": str(uid), "plan": f"{PLAN_DURATION_DAYS}d_299"},
        "callback_method": "get"
    }
    url = f"{RP_BASE}/payment_links"
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, headers=_rp_auth_headers(), data=json.dumps(payload)) as r:
            if r.status >= 300:
                text = await r.text()
                raise RuntimeError(f"Razorpay link create failed: {r.status} {text}")
            data = await r.json()
            return data["id"], data.get("short_url") or data.get("url")

async def rp_get_payment_link(link_id: str) -> dict:
    url = f"{RP_BASE}/payment_links/{link_id}"
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, headers=_rp_auth_headers()) as r:
            if r.status >= 300:
                text = await r.text()
                raise RuntimeError(f"Razorpay link fetch failed: {r.status} {text}")
            return await r.json()

# ---------------- BOT PROFILE ----------------
async def setup_bot_profile():
    try:
        me = await bot.get_me()
        await bot(SetBotInfoRequest(
            bot=me, lang_code="en",
            name="Auto Message Forwarder Bot",
            about="🔄 Forward messages between chats (even private).",
            description=("What can this bot do?\n\n"
                         "I can copy messages from any chat (public/private channel/group/bot/user) "
                         "to any other chat as soon as they arrive. Only *new* messages.\n\n"
                         "Send /start to begin 🚀")
        ))
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="en",
            commands=[BotCommand(cmd, desc) for cmd, desc in COMMANDS]
        ))
    except Exception as e:
        print("Profile/commands set error:", e)

# ---------------- GUARDS ----------------
async def guard_or_hint(e) -> bool:
    if await is_logged_in(e.sender_id):
        return True
    await e.respond("🔒 Please **/login** first to use this command.")
    return False

ALWAYS_ALLOWED = {"start","help","login","status","config","upgrade","upgrade_status","logout"}

async def premium_or_hint(e) -> bool:
    raw = (e.raw_text or "").strip()
    cmd = raw.split()[0].lstrip("/").split("@")[0].lower() if raw.startswith("/") else ""
    if cmd in ALWAYS_ALLOWED:
        return True
    sub = sp_get_subscription(e.sender_id)
    if sp_is_sub_active(sub):
        return True
    btns = [
        [Button.inline(f"💳 Upgrade ₹{PLAN_AMOUNT_PAISE//100} / {PLAN_DURATION_DAYS} days", data=b"upgrade_open")],
        [Button.inline("🔄 Check Status", data=b"upgrade_check")],
    ]
    await e.respond(
        "🔒 **Premium required**\n\n"
        "Upgrade to use forwarding, filters, remove/add targets, etc.\n"
        f"Plan: **₹{PLAN_AMOUNT_PAISE//100} for {PLAN_DURATION_DAYS} days** (adds +30 days on every renewal).",
        buttons=btns, parse_mode="md"
    )
    return False

# ---------------- PUBLIC COMMANDS ----------------
@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_cmd(e):
    await e.respond(commands_text(), parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/help$"))
async def help_cmd(e):
    await e.respond(commands_text(), parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/status$"))
async def status_cmd(e):
    data = sp_get_session(e.sender_id)
    if not data:
        return await e.respond("🔴 Not logged in. Use **/login** to connect your account.", parse_mode="md")
    if not await is_logged_in(e.sender_id):
        return await e.respond("🟠 Session found but not authorized locally. Please **/login** again.", parse_mode="md")
    await e.respond(f"🟢 Logged In\n**Phone:** {data['phone']}\n`{data['session_file']}`", parse_mode="md")

# ---------------- LOGIN FLOW (2FA SUPPORTED) ----------------
@bot.on(events.NewMessage(pattern=r"^/login$"))
async def login_cmd(e):
    uid = e.sender_id
    if await is_logged_in(uid):
        return await e.respond(
            "✅ **Already logged in.**\n"
            "Set chats: **/incoming** & **/outgoing**\n"
            "Start forwarding: **/work**"
        )
    login_state[uid] = {"step": "phone", "phone": None}
    await e.respond("📲 Apna number bhejo `+919876543210` iss format me.")

@bot.on(events.NewMessage)
async def login_flow(e):
    uid = e.sender_id
    msg = (e.raw_text or "").strip()
    if uid not in login_state:
        return
    st = login_state[uid]

    # STEP: PHONE
    if st["step"] == "phone":
        if not PHONE_RE.match(msg):
            return await e.respond("⚠️ Valid number bhejo like - `+919876543210`.")
        phone = msg
        local = session_path(uid, phone)
        client = TelegramClient(local, API_ID, API_HASH)
        st["phone"] = phone
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                sp_upsert_session(uid, phone, os.path.basename(local))
                await e.respond(f"✅ Already logged in as **{me.first_name}**.\nStart forwarding with **/work**.")
                login_state.pop(uid, None)
                return
            res = await client.send_code_request(phone)
            st["phone_code_hash"] = getattr(res, "phone_code_hash", None)
            await e.respond(
                "📩 OTP bhejo: `HELLO123456`.\n\n"
                "Didn't get the OTP? Resend by clicking on button 👇",
                buttons=[[Button.inline("🔁 Resend OTP", data=b"resend_otp")]]
            )
            st["step"] = "otp"
        except Exception as ex:
            await e.respond(f"❌ OTP send error: `{ex}`\nStart again with /login.")
            print("send_code_request error:", ex)
        finally:
            try: await client.disconnect()
            except: pass
        return

    # STEP: OTP
    if st["step"] == "otp":
        m = OTP_RE.match(msg)
        if not m:
            return await e.respond("⚠️ OTP `123456` ya `HELLO123456` format me bhejo.")
        otp = m.group(1)
        phone = st.get("phone")
        if not phone:
            login_state.pop(uid, None)
            return await e.respond("⚠️ Phone missing. Start /login again.")
        code_hash = st.get("phone_code_hash")
        if not code_hash:
            login_state.pop(uid, None)
            return await e.respond("❌ Phone code session expired or missing. Start again with /login.")

        local = session_path(uid, phone)
        client = TelegramClient(local, API_ID, API_HASH)
        try:
            await client.connect()
            try:
                await client.sign_in(phone, otp, phone_code_hash=code_hash)
                me = await client.get_me()
                sp_upsert_session(uid, phone, os.path.basename(local))
                await e.respond(
                    f"✅ Logged in as **{me.first_name}**.\n"
                    "Now set **/incoming** & **/outgoing**, then **/work** to start."
                )
                login_state.pop(uid, None)
                return
            except errors.SessionPasswordNeededError:
                try:
                    pwd = await client(functions.account.GetPasswordRequest())
                    hint = getattr(pwd, "hint", "") or ""
                except Exception:
                    hint = ""
                st["step"] = "2fa"
                st["twofa_session_path"] = local
                msg_hint = f" (hint: `{hint}`)" if hint else ""
                await e.respond(f"🔐 2FA enabled. Please enter your **Telegram password**{msg_hint}.\n"
                                "_We won't store your password; it's used once to finish login._")
                return
        except errors.PhoneCodeInvalidError:
            await e.respond("❌ Wrong OTP. `/login` try again.")
        except Exception as ex:
            await e.respond(f"❌ Login failed: `{ex}`\nStart again with /login.")
            print("sign_in error:", ex)
        finally:
            try: await client.disconnect()
            except: pass
        return

    # STEP: 2FA PASSWORD
    if st["step"] == "2fa":
        password = msg
        phone = st.get("phone")
        local = st.get("twofa_session_path") or session_path(uid, phone or "")
        if not phone or not local:
            login_state.pop(uid, None)
            return await e.respond("⚠️ Session expired. Start /login again.")
        client = TelegramClient(local, API_ID, API_HASH)
        try:
            await client.connect()
            await client.sign_in(password=password)
            me = await client.get_me()
            sp_upsert_session(uid, phone, os.path.basename(local))
            await e.respond(
                f"✅ 2FA verified. Logged in as **{me.first_name}**.\n"
                "Start forwarding with **/incoming** & **/outgoing**, then **/work**."
            )
        except errors.PasswordHashInvalidError:
            return await e.respond("❌ Wrong password, try again (or `/login` to restart).")
        except Exception as ex:
            await e.respond(f"❌ 2FA login failed: `{ex}`\nUse `/login` to reset and try again.")
            print("2FA sign_in error:", ex)
        finally:
            login_state.pop(uid, None)
            try: await client.disconnect()
            except: pass
        return

# ---------------- PREMIUM-GATED COMMANDS ----------------
@bot.on(events.NewMessage(pattern=r"^/incoming$"))
async def incoming_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    await e.respond(
        "📥 **Incoming (Sources)**\n"
        "Pehle un chats ko **pin** karo jinko source banana hai. Sirf **top 14** dikhenge.\n\n"
        "Ready? Tap:",
        buttons=[Button.inline("📌 I have pinned the chats", data=b"pin_incoming")]
    )

@bot.on(events.NewMessage(pattern=r"^/outgoing$"))
async def outgoing_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    await e.respond(
        "📤 **Outgoing (Targets)**\n"
        "Jin chats me bhejna hai, unhe **pin** karke top par lao. Sirf **top 14** dikhenge.\n\n"
        "Ready? Tap:",
        buttons=[Button.inline("📌 I have pinned the chats", data=b"pin_outgoing")]
    )

@bot.on(events.CallbackQuery(pattern=b"^pin_incoming$"))
async def cb_incoming(event):
    uid = event.sender_id
    try:
        uc = await get_user_client(uid)
    except Exception:
        return await event.answer("Login first with /login", alert=True)
    pairs = await top_dialog_pairs(uc, TOP_N)
    await uc.disconnect()
    select_state[uid] = {
        "mode": "incoming", "pairs": pairs, "selected": set(),
        "incoming_ids": [], "outgoing_ids": [],
    }
    await event.edit(
        "📥 Select **INCOMING** chats (multi-select). Numbers toggle, then **Done**.\n\n"
        + numbered_list_from_pairs(pairs),
        buttons=multi_kb(len(pairs), set()),
    )

@bot.on(events.CallbackQuery(pattern=b"^pin_outgoing$"))
async def cb_outgoing(event):
    uid = event.sender_id
    try:
        uc = await get_user_client(uid)
    except Exception:
        return await event.answer("Login first with /login", alert=True)
    pairs = await top_dialog_pairs(uc, TOP_N)
    await uc.disconnect()
    st = select_state.get(uid, {"incoming_ids": [], "outgoing_ids": []})
    select_state[uid] = {
        "mode": "outgoing", "pairs": pairs, "selected": set(),
        "incoming_ids": st.get("incoming_ids", []), "outgoing_ids": [],
    }
    await event.edit(
        "📤 Select **OUTGOING** chats (multi-select). Numbers toggle, then **Done**.\n\n"
        + numbered_list_from_pairs(pairs),
        buttons=multi_kb(len(pairs), set()),
    )

@bot.on(events.CallbackQuery(pattern=b"^msel:"))
async def cb_toggle(event):
    uid = event.sender_id
    st = select_state.get(uid)
    if not st:
        return await event.answer("Expired. Use /incoming or /outgoing.", alert=True)
    try:
        idx = int(event.data.decode().split(":")[1]) - 1
    except Exception:
        return await event.answer("Invalid.", alert=True)
    if idx < 0 or idx >= len(st["pairs"]):
        return await event.answer("Out of range.", alert=True)

    sel: Set[int] = st["selected"]
    if idx in sel: sel.remove(idx)
    else: sel.add(idx)

    if st["mode"] == "incoming": header = "📥 Select **INCOMING** chats"
    elif st["mode"] == "outgoing": header = "📤 Select **OUTGOING** chats"
    elif st["mode"] == "remove_in": header = "❌ Select **INCOMING sources** to remove"
    elif st["mode"] == "remove_out": header = "❌ Select **OUTGOING targets** to remove"
    elif st["mode"] == "remove_filter": header = "🗑️ Select **filters** to remove"
    else: header = "Select items"

    await event.edit(
        f"{header} (multi-select). Numbers toggle, then **Done**.\n\n"
        + numbered_list_from_pairs(st["pairs"]),
        buttons=multi_kb(len(st["pairs"]), sel),
    )

@bot.on(events.CallbackQuery(pattern=b"^msel_done$"))
async def cb_msel_done(event):
    uid = event.sender_id
    st = select_state.get(uid)
    if not st:
        return await event.answer("Session expired. Start again.", alert=True)

    if st["mode"] == "remove_filter":
        chosen = sorted(st["selected"])
        rows = st.get("filter_rows", [])
        to_remove = []
        for i in chosen:
            if 0 <= i < len(rows):
                fn = (rows[i].get("from_name") or "").strip()
                if fn: to_remove.append(fn)
        removed = sp_delete_filters_batch(uid, to_remove)
        select_state.pop(uid, None)
        if uid in forward_loops:
            forward_loops[uid]["filters"] = compile_filters_for_user(uid)
        pretty = "\n".join([f"- `{x}`" for x in to_remove]) or "(none)"
        return await event.edit(f"✅ Removed **{removed}** filter(s):\n{pretty}", buttons=None)

    chosen_idxs = sorted(st.get("selected", []))
    chosen_ids = [st["pairs"][i][0] for i in chosen_idxs if 0 <= i < len(st.get("pairs", []))]

    if st["mode"] == "incoming":
        st["incoming_ids"] = chosen_ids
        st["selected"] = set()
        return await event.edit("✅ **Incoming set!**\n\nNow send **/outgoing**.", buttons=None)

    elif st["mode"] == "outgoing":
        st["outgoing_ids"] = chosen_ids
        st["selected"] = set()
        return await event.edit("✅ **Outgoing set!**\n\nStart with **/work**.", buttons=None)

    elif st["mode"] == "remove_in":
        sp_delete_senders(uid, chosen_ids)
        select_state.pop(uid, None)
        return await event.edit("✅ Selected **incoming sources** removed.", buttons=None)

    elif st["mode"] == "remove_out":
        sp_remove_targets_globally(uid, chosen_ids)
        select_state.pop(uid, None)
        return await event.edit("✅ Selected **outgoing targets** removed from all mappings.", buttons=None)

@bot.on(events.CallbackQuery(pattern=b"^msel_cancel$"))
async def cb_cancel(event):
    select_state.pop(event.sender_id, None)
    await event.edit("✖ Cancelled. Use **/incoming** or **/outgoing** again.")

@bot.on(events.NewMessage(pattern=r"^/remove_incoming$"))
async def remove_incoming_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    uc = await get_user_client(uid)
    mapping = sp_load_mapping(uid)
    senders = list(mapping.keys())
    if not senders:
        await uc.disconnect()
        return await e.respond("ℹ️ No incoming sources saved.")
    names = await titles_for_ids(uc, senders)
    await uc.disconnect()
    pairs = list(zip(senders, names))
    select_state[uid] = {"mode": "remove_in", "pairs": pairs, "selected": set()}
    await e.respond(
        "❌ Select **INCOMING sources** to remove (multi-select) and press **Done**.\n\n" +
        numbered_list_from_pairs([(sid, nm) for sid, nm in pairs]),
        buttons=multi_kb(len(pairs), set())
    )

@bot.on(events.NewMessage(pattern=r"^/remove_outgoing$"))
async def remove_outgoing_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    uc = await get_user_client(uid)
    mapping = sp_load_mapping(uid)
    all_targets: List[int] = sorted({int(t) for lst in mapping.values() for t in lst})
    if not all_targets:
        await uc.disconnect()
        return await e.respond("ℹ️ No outgoing targets saved.")
    names = await titles_for_ids(uc, all_targets)
    await uc.disconnect()
    pairs = list(zip(all_targets, names))
    select_state[uid] = {"mode": "remove_out", "pairs": pairs, "selected": set()}
    await e.respond(
        "❌ Select **OUTGOING targets** to remove (multi-select) and press **Done**.\n\n" +
        numbered_list_from_pairs([(tid, nm) for tid, nm in pairs]),
        buttons=multi_kb(len(pairs), set())
    )

@bot.on(events.NewMessage(pattern=r"^/config$"))
async def cmd_config(e):
    # intentionally NOT premium-gated per your requirement
    if not await guard_or_hint(e): return
    uid = e.sender_id
    mapping = sp_load_mapping(uid)
    if not mapping:
        return await e.respond("ℹ️ No configuration yet. First set **/incoming** & **/outgoing**.")
    uc = await get_user_client(uid)
    lines = ["**This is your current configuration:**", ""]
    for src, tgts in mapping.items():
        src_name = (await titles_for_ids(uc, [src]))[0]
        tgt_names = await titles_for_ids(uc, tgts)
        lines.append(f"- **COPYING from:** `{src_name}`")
        lines.append(f"  **→ TARGETING to:** {', '.join([f'`{n}`' for n in tgt_names]) if tgt_names else '`[]`'}")
        lines.append("")
    await uc.disconnect()
    await e.respond("\n".join(lines), parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/work$"))
async def cmd_work(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return

    uid = e.sender_id
    st = select_state.get(uid, {})
    inc = st.get("incoming_ids", [])
    out = st.get("outgoing_ids", [])
    if inc and out:
        for s in inc:
            sp_upsert_mapping(uid, s, out)

    mapping = sp_load_mapping(uid)
    if not mapping:
        return await e.respond("⚠️ First complete **/incoming** and **/outgoing** commands.")

    # fresh user client
    try:
        uclient = await get_user_client(uid)
    except Exception as ex:
        return await e.respond(f"❌ {ex}")

    compiled_now = compile_filters_for_user(uid)
    user_delay    = sp_get_delay(uid) or 0

    @uclient.on(events.NewMessage)
    async def handle_forward(evt):
        try:
            # ✅ allow channel posts even if sent by same user (admin post is out=True)
            if getattr(evt, "out", False) and not evt.is_channel:
                return

            src_id = evt.chat_id if evt.chat_id is not None else evt.sender_id
            targets = mapping.get(int(src_id))
            if not targets:
                return

            msg  = evt.message
            text = msg.message or ""

            state        = forward_loops.get(uid, {})
            curr_filters = state.get("filters", compiled_now)
            curr_delay   = int(state.get("delay_seconds", user_delay) or 0)

            if text:
                text = apply_text_filters(text, curr_filters)
                # 🔄 Add user's custom start/end text
                addons = sp_get_text_addons(uid)
                if addons.get("start_text"):
                    text = f"{addons['start_text']}\n\n{text}"
                if addons.get("end_text"):
                   text = f"{text}\n\n{addons['end_text']}"


            has_media = bool(msg.media)

            for t in targets:
                if curr_delay > 0:
                    await asyncio.sleep(curr_delay)

                try:
                    if has_media:
                        # ✅ Preserve native media type (no bytes)
                        if msg.photo:
                            await uclient.send_file(int(t), msg.photo, caption=text or "")
                        elif msg.video:
                            await uclient.send_file(int(t), msg.video, caption=text or "")
                        elif msg.sticker:
                            await uclient.send_file(int(t), msg.sticker, caption=text or "")
                        elif msg.animation:  # GIF
                            await uclient.send_file(int(t), msg.animation, caption=text or "")
                        elif msg.document:
                            await uclient.send_file(int(t), msg.document, caption=text or "")
                        else:
                            await uclient.send_file(int(t), msg.media, caption=text or "")
                    else:
                        if text:
                            await uclient.send_message(int(t), text)

                except errors.FloodWaitError as fw:
                    await asyncio.sleep(fw.seconds + 1)
                except Exception as ex:
                    print("send err:", ex)

                await asyncio.sleep(FORWARD_THROTTLE)

        except Exception as ex:
            print("forward handler err:", ex)

    # ✅ just start; NO asyncio.create_task(uclient.run_until_disconnected())
    await uclient.start()

    forward_loops[uid] = {
        "client": uclient,
        "filters": compiled_now,
        "delay_seconds": user_delay
    }

    await e.respond("▶️ **Forwarding started!**\nStop anytime with **/stop**.")

@bot.on(events.NewMessage(pattern=r"^/stop$"))
async def cmd_stop(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    if uid not in forward_loops:
        return await e.respond("⏸️ Forwarding is not running. Use **/work** to start.")
    try:
        await forward_loops[uid]["client"].disconnect()
    except: pass
    forward_loops.pop(uid, None)
    await e.respond("⏹️ **Forwarding stopped.**")

# ---------------- FILTER COMMANDS (premium) ----------------
@bot.on(events.NewMessage(pattern=r"^/addfilter$"))
async def addfilter_help_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    txt = (
        "🧩 **Add a replace filter**\n"
        "Use in this format:\n"
        "`/addfilter old==new`\n"
        "`/addfilter @old==@new`\n\n"
        "**Examples:**\n"
        "`/addfilter rohit==sobhit`\n"
        "`/addfilter @anychannelname==@otherchannelname`\n\n"
        "Left side (old text) and right side (new text)."
    )
    await e.respond(txt, parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/addfilter\s+(.+)$"))
async def addfilter_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    raw = (e.pattern_match.group(1) or "").strip()
    parts = re.split(r"\s*==\s*", raw, maxsplit=1)
    if len(parts) != 2:
        return await e.respond(
            "⚠️ Format galat hai.\nUse: `/addfilter old==new` ya `/addfilter @old==@new`",
            parse_mode="md"
        )
    left, right = parts[0].strip(), parts[1].strip()
    if not left or not right:
        return await e.respond("⚠️ Dono words required hain: `old==new`.", parse_mode="md")
    if left.lower() == right.lower():
        return await e.respond("⚠️ Left aur right same nahi ho sakte.")
    ok, msg = sp_add_filter(uid, left, right)
    await e.respond(msg)
    if uid in forward_loops:
        forward_loops[uid]["filters"] = compile_filters_for_user(uid)

@bot.on(events.NewMessage(pattern=r"^/showfilter$"))
async def showfilter_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    rows = sp_list_filters(uid)
    if not rows:
        return await e.respond("ℹ️ No filters set. Add one: `/addfilter @old==@new`", parse_mode="md")
    lines = ["**Your filters:**", ""]
    for r in rows:
        lines.append(f"- `{r['from_name']}` → `{r['to_name']}`")
    lines.append("")
    lines.append("👉 **Tap to remove:** /removefilter ")
    await e.respond("\n".join(lines), parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/removefilter$"))
async def removefilter_ui_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    rows = sp_list_filters(uid)
    if not rows:
        return await e.respond("❌ No filters set yet!\n\nAdd filters with `/addfilter old==new`.", parse_mode="md")
    pairs = [(i, f"{(r.get('from_name') or '')} → {(r.get('to_name') or '')}") for i, r in enumerate(rows)]
    select_state[uid] = {"mode": "remove_filter", "pairs": pairs, "selected": set(), "filter_rows": rows}
    await e.respond(
        "🗑️ Remove Filters — select filters to remove (multi-select) and press ✅ Done.\n\n"
        + numbered_list_from_pairs(pairs),
        buttons=multi_kb(len(pairs), set())
    )

@bot.on(events.NewMessage(pattern=r"^/removefilter\s+(\S+)$"))
async def removefilter_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    left = e.pattern_match.group(1)
    ok, msg = sp_delete_filter(uid, left)
    await e.respond(msg)
    if uid in forward_loops:
        forward_loops[uid]["filters"] = compile_filters_for_user(uid)

@bot.on(events.NewMessage(pattern=r"^/deleteallfilters$"))
async def delete_all_filters_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    n = sp_delete_all_filters(uid)
    if uid in forward_loops:
        forward_loops[uid]["filters"] = compile_filters_for_user(uid)
    await e.respond(f"🗑️ Deleted **{n}** filter(s).")

# ---------------- START_TEXT / END_TEXT / REMOVE_TEXT COMMANDS ----------------
@bot.on(events.NewMessage(pattern=r"^/start_text$"))
async def start_text_help_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    txt = (
        "📝 **Add Starting Text**\n\n"
        "Use this format to set the text that appears at the *start* of every forwarded message:\n"
        "`/start_text HELLO THIS IS MY PERSONAL CHANNEL NAME SUPPORT HUB`\n\n"
        "Example output:\n"
        "`HELLO THIS IS MY PERSONAL CHANNEL NAME SUPPORT HUB`\n"
        "_(then your forwarded message content follows)_"
    )
    await e.respond(txt, parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/start_text\s+(.+)$"))
async def start_text_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    text = e.pattern_match.group(1).strip()
    sp_set_start_text(uid, text)
    await e.respond(f"✅ Starting text set:\n\n`{text}`", parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/end_text$"))
async def end_text_help_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    txt = (
        "📝 **Add Ending Text**\n\n"
        "Use this format to set the text that appears at the *end* of every forwarded message:\n"
        "`/end_text THIS IS FOR SHOWING IN THE ENDING OF THE MESSAGES`\n\n"
        "Example output:\n"
        "*(forwarded message content)*\n"
        "`THIS IS FOR SHOWING IN THE ENDING OF THE MESSAGES`"
    )
    await e.respond(txt, parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/end_text\s+(.+)$"))
async def end_text_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    text = e.pattern_match.group(1).strip()
    sp_set_end_text(uid, text)
    await e.respond(f"✅ Ending text set:\n\n`{text}`", parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/remove_text$"))
async def remove_text_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    sp_remove_texts(uid)
    await e.respond("🧹 Removed all saved start and end texts.")


@bot.on(events.NewMessage(pattern=r"^/delay\s+(\d+)$"))
async def delay_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    secs = int(e.pattern_match.group(1))
    if not (0 <= secs <= 999):
        return await e.respond("⚠️ Use: `/delay 0..999` seconds.", parse_mode="md")
    sp_set_delay(uid, secs)
    if uid in forward_loops:
        forward_loops[uid]["delay_seconds"] = secs
    await e.respond(f"⏱️ Delay set to **{secs}s**.")

@bot.on(events.NewMessage(pattern=r"^/removedelay$"))
async def remove_delay_cmd(e):
    """Removes any set delay (resets to 0 seconds)"""
    if not await guard_or_hint(e): 
        return
    if not await premium_or_hint(e): 
        return

    uid = e.sender_id

    try:
        # Reset delay_seconds back to 0
        payload = {
            "user_id": uid,
            "delay_seconds": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        supabase.table("user_settings").upsert(payload, on_conflict="user_id").execute()

        # If forwarding loop is active, update it immediately
        if uid in forward_loops:
            forward_loops[uid]["delay_seconds"] = 0

        await e.respond("⏱️ Delay removed successfully. Messages will now forward instantly.")
    except Exception as ex:
        await e.respond(f"❌ Failed to remove delay:\n`{ex}`", parse_mode="md")


# ---------------- UPGRADE COMMANDS ----------------
@bot.on(events.NewMessage(pattern=r"^/upgrade$"))
async def cmd_upgrade(e):
    txt = (
        "✨ **Auto Message Forwarder — Premium**\n\n"
        "• Unlimited auto-forwarding between your selected chats\n"
        "• Text replacement filters\n"
        "• Delay control\n"
        "• Remove / manage mappings\n\n"
        f"**Plan:** ₹{PLAN_AMOUNT_PAISE//100} for {PLAN_DURATION_DAYS} days\n"
        "_Every payment adds +30 days to your expiry._"
    )
    await e.respond(
        txt, parse_mode="md",
        buttons=[
            [Button.inline(f"💳 Pay ₹{PLAN_AMOUNT_PAISE//100}", data=b"upgrade_pay")],
            [Button.inline("❌ Cancel", data=b"upgrade_cancel")]
        ]
    )

@bot.on(events.NewMessage(pattern=r"^/upgrade_status$"))
async def cmd_upgrade_status(e):
    uid = e.sender_id
    sub = sp_get_subscription(uid)
    if sub and sp_is_sub_active(sub):
        exp = datetime.fromisoformat(str(sub["expires_at"]).replace("Z", "+00:00"))
        st  = datetime.fromisoformat(str(sub["started_at"]).replace("Z", "+00:00"))
        left_days = (exp - datetime.now(timezone.utc)).days
        await e.respond(
            "🟢 **Subscription Active**\n"
            f"Started:  `{st.strftime('%Y-%m-%d %H:%M UTC')}`\n"
            f"Expires:  `{exp.strftime('%Y-%m-%d %H:%M UTC')}`\n"
            f"Left:     **{max(left_days,0)} day(s)**\n"
            f"Cycles:   **{sub.get('total_cycles', 0)}**",
            parse_mode="md",
            buttons=[[Button.inline("💳 Renew (+30 days)", data=b"upgrade_pay")]]
        )
    else:
        await e.respond(
            "🔴 **No active subscription**\n"
            f"Get access for **₹{PLAN_AMOUNT_PAISE//100} / {PLAN_DURATION_DAYS} days**.",
            parse_mode="md",
            buttons=[[Button.inline(f"💳 Pay ₹{PLAN_AMOUNT_PAISE//100}", data=b"upgrade_pay")]]
        )

@bot.on(events.CallbackQuery(pattern=b"upgrade_open"))
async def cb_upgrade_open(event):
    await cmd_upgrade(await event.get_message())

@bot.on(events.CallbackQuery(pattern=b"upgrade_cancel"))
async def cb_upgrade_cancel(event):
    await event.edit("❌ Upgrade cancelled.")

@bot.on(events.CallbackQuery(pattern=b"upgrade_check"))
async def cb_upgrade_check(event):
    class FakeE:
        sender_id = event.sender_id
        async def respond(self, *a, **kw): return await event.reply(*a, **kw)
    await cmd_upgrade_status(FakeE())

@bot.on(events.CallbackQuery(pattern=b"upgrade_pay"))
async def cb_upgrade_pay(event):
    uid = event.sender_id
    try:
        plink_id, plink_url = await rp_create_payment_link(uid)
        await event.edit(
            f"🔗 **Payment Link Created**\n"
            f"Pay ₹{PLAN_AMOUNT_PAISE//100} securely on Razorpay.\n\n"
            f"After payment, press **Verify**.",
            parse_mode="md",
            buttons=[
                [Button.url("💳 Pay Now", plink_url)],
                [Button.inline("✅ I have paid — Verify", data=f"upgrade_verify:{plink_id}".encode())],
                [Button.inline("❌ Cancel", data=b"upgrade_cancel")]
            ]
        )
    except Exception as ex:
        await event.edit(f"❌ Could not create payment link:\n`{ex}`", parse_mode="md")

@bot.on(events.CallbackQuery(pattern=b"upgrade_verify:"))
async def cb_upgrade_verify(event):
    uid = event.sender_id
    try:
        data = event.data.decode()
        _, link_id = data.split(":")
    except Exception:
        return await event.answer("Invalid verify request.", alert=True)

    try:
        info = await rp_get_payment_link(link_id)
        status = info.get("status")
        if status == "paid":
            payments = info.get("payments") or []
            pay_id = str(payments[0]) if payments else str(info.get("id"))
            plink_url = info.get("short_url") or info.get("url") or ""
            sp_extend_subscription(uid, PLAN_DURATION_DAYS, pay_id, link_id, plink_url)
            await event.edit(
                "✅ **Payment verified!**\n"
                f"Your subscription has been extended by **{PLAN_DURATION_DAYS} days**.\n"
                "You can now use premium commands like **/incoming**, **/outgoing**, **/work**, etc.",
                parse_mode="md",
                buttons=[[Button.inline("📊 Check Status", data=b"upgrade_check")]]
            )
        else:
            await event.answer("Payment not completed yet. Finish payment and press Verify again.", alert=True)
    except Exception as ex:
        await event.edit(f"❌ Verify failed:\n`{ex}`", parse_mode="md")

# ---------------- LOGOUT ----------------
@bot.on(events.NewMessage(pattern=r"^/logout$"))
async def logout_cmd(e):
    # /logout is in ALWAYS_ALLOWED (free user ok), but needs to be logged in to actually delete session
    if not await guard_or_hint(e): return
    await e.respond(
        "⚠️ Are you sure you want to logout? After logout all your data will be deleted.\n\n"
        "Your session file and mappings will be removed permanently.\n\n"
        "Confirm to proceed:",
        buttons=[[Button.inline("✅ Yes, logout", data=b"logout_confirm"),
                  Button.inline("✖ Cancel", data=b"logout_cancel")]]
    )

@bot.on(events.CallbackQuery(pattern=b"logout_confirm"))
async def logout_confirm_cb(event):
    uid = event.sender_id
    data = sp_get_session(uid)
    if not data:
        return await event.edit("ℹ️ No session found.")
    if uid in forward_loops:
        try:
            await forward_loops[uid]["client"].disconnect()
        except: pass
        forward_loops.pop(uid, None)
    path = os.path.join(SESSION_DIR, data["session_file"])
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as ex:
            print("remove session file err:", ex)
    sp_delete_session(uid)
    await event.edit("👋 Logged out successfully. All your data removed. You can /login again anytime.")

@bot.on(events.CallbackQuery(pattern=b"logout_cancel"))
async def logout_cancel_cb(event):
    await event.edit("✖ Logout cancelled. You are still logged in.")

@bot.on(events.CallbackQuery(pattern=b"resend_otp"))
async def cb_resend_otp(event):
    uid = event.sender_id
    st = login_state.get(uid)
    if not st or not st.get("phone"):
        return await event.answer("No login in progress. Use /login.", alert=True)

    phone = st["phone"]
    local = session_path(uid, phone)
    client = TelegramClient(local, API_ID, API_HASH)
    try:
        await safe_connect(client)
        res = await client.send_code_request(phone)
        st["phone_code_hash"] = getattr(res, "phone_code_hash", None)
        await event.edit(
            "📩 OTP resent. Send like `123456` or `HELLO123456`.\n\n"
            "If still not received, try again after a minute.",
            buttons=[[Button.inline("🔁 Resend OTP", data=b"resend_otp")]]
        )
    except Exception as ex:
        print("resend_code_request error:", ex)
        await event.edit(f"❌ Resend failed: {ex}\nStart /login again.")
    finally:
        try: await client.disconnect()
        except: pass

async def safe_connect(client, retries=3, delay=2):
    try:
        if hasattr(client, "is_connected") and callable(client.is_connected) and client.is_connected():
            return
    except Exception:
        pass
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            await client.connect()
            try:
                if hasattr(client, "is_connected") and callable(client.is_connected) and client.is_connected():
                    return
            except Exception:
                return
            return
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(delay * attempt)
    raise last_exc or RuntimeError("safe_connect: failed to connect")

# ---------------- RUN ----------------
if __name__ == "__main__":
    print("🤖 Auto-Forward Login Bot ready!")
    try:
        asyncio.get_event_loop().run_until_complete(setup_bot_profile())
    except:
        pass
    bot.run_until_disconnected()

