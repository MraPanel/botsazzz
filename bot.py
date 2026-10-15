import asyncio
import os
import re
import sqlite3
import time
import secrets

import httpx
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH", "bot.db")

GB_OPTIONS = [10, 20, 50, 100]
DAY_OPTIONS = [30, 60, 90]
USER_OPTIONS = [1, 2, 3]

# ----------------------------------------------------------------------------
# Defaults for settings, texts and button names (all editable from admin panel)
# ----------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "panel_url": "https://panel.example.com",
    "panel_user": "admin",
    "panel_pass": "password",
    "panel_group_id": "1",
    "usd_rate": "100000",          # Toman per 1 USD
    "price_gb": "0.10",            # USD per GB
    "price_day": "0.05",           # USD per day
    "price_user": "0.50",          # USD per user
    "card_info": "6037-0000-0000-0000\nبه نام: نام صاحب کارت",
}

# key: (label shown in admin panel, default value)
TEXTS = {
    "txt_start": ("متن خوش‌آمدگویی", "سلام 👋\nبه ربات فروش کانفیگ خوش آمدید.\nیکی از گزینه‌ها را انتخاب کنید:"),
    "txt_choose_gb": ("متن انتخاب حجم", "📦 حجم مورد نظر را انتخاب کنید:"),
    "txt_choose_days": ("متن انتخاب مدت", "📅 مدت اعتبار را انتخاب کنید:"),
    "txt_choose_users": ("متن انتخاب تعداد کاربر", "👥 تعداد کاربر را انتخاب کنید:"),
    "txt_summary": ("متن فاکتور", "🧾 فاکتور شما\n\nحجم: {gb} گیگ\nمدت: {days} روز\nکاربر: {users}\n\nقیمت: {usd}$\nمبلغ قابل پرداخت: {toman} تومان"),
    "txt_pay": ("متن پرداخت", "💳 مبلغ {toman} تومان را به کارت زیر واریز کنید و سپس عکس رسید را همینجا ارسال کنید:\n\n{card}"),
    "txt_receipt_ok": ("متن دریافت رسید", "✅ رسید دریافت شد. پس از تایید مدیر، کانفیگ برای شما ارسال می‌شود."),
    "txt_approved": ("متن پس از تایید", "🎉 سفارش شما تایید شد.\n\nلینک اشتراک:\n{link}"),
    "txt_rejected": ("متن رد پرداخت", "❌ پرداخت شما تایید نشد. در صورت اشتباه با پشتیبانی تماس بگیرید."),
    "txt_no_configs": ("متن نبودن کانفیگ", "شما هنوز کانفیگی ندارید."),
    "txt_support": ("متن پشتیبانی", "برای پشتیبانی به آیدی @support پیام دهید."),
}

BUTTONS = {
    "btn_buy": ("دکمه خرید", "🛒 خرید کانفیگ"),
    "btn_my": ("دکمه کانفیگ‌های من", "📂 کانفیگ‌های من"),
    "btn_support": ("دکمه پشتیبانی", "💬 پشتیبانی"),
    "btn_back": ("دکمه بازگشت", "🔙 بازگشت"),
    "btn_confirm": ("دکمه تایید خرید", "✅ تایید و پرداخت"),
    "btn_cancel": ("دکمه انصراف", "✖️ انصراف"),
}

LABELS = {k: v[0] for k, v in {**TEXTS, **BUTTONS}.items()}
LABELS.update({
    "panel_url": "آدرس پنل پاسارگاد",
    "panel_user": "یوزرنیم پنل",
    "panel_pass": "پسورد پنل",
    "panel_group_id": "شناسه گروه (Group ID) در پنل",
    "usd_rate": "نرخ دلار (تومان)",
    "price_gb": "قیمت هر گیگ (دلار)",
    "price_day": "قیمت هر روز (دلار)",
    "price_user": "قیمت هر کاربر (دلار)",
    "card_info": "اطلاعات کارت",
})

DEFAULTS = dict(DEFAULT_SETTINGS)
DEFAULTS.update({k: v[1] for k, v in TEXTS.items()})
DEFAULTS.update({k: v[1] for k, v in BUTTONS.items()})

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, username TEXT,
    gb INTEGER, days INTEGER, users INTEGER,
    usd REAL, toman INTEGER,
    status TEXT DEFAULT 'pending',
    panel_username TEXT, sub_link TEXT,
    created INTEGER
);
""")
db.commit()


def get(key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS[key]


def put(key: str, value: str):
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    db.commit()


def calc_price(gb: int, days: int, users: int):
    usd = gb * float(get("price_gb")) + days * float(get("price_day")) + users * float(get("price_user"))
    usd = round(usd, 2)
    toman = int(round(usd * float(get("usd_rate")) / 1000.0)) * 1000  # round to nearest 1000
    return usd, toman


def fmt(n: int) -> str:
    return f"{n:,}"


# ----------------------------------------------------------------------------
# PasarGuard API
# ----------------------------------------------------------------------------
class PanelError(Exception):
    pass


async def panel_token(client: httpx.AsyncClient) -> str:
    r = await client.post(
        f"{get('panel_url').rstrip('/')}/api/admin/token",
        data={"username": get("panel_user"), "password": get("panel_pass")},
    )
    if r.status_code != 200:
        raise PanelError(f"login failed: {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


async def panel_create_user(order) -> tuple[str, str]:
    username = f"u{order['user_id']}_{secrets.token_hex(3)}"
    expire = int(time.time()) + order["days"] * 86400
    payload = {
        "username": username,
        "status": "active",
        "expire": expire,
        "data_limit": order["gb"] * 1024 ** 3,
        "data_limit_reset_strategy": "no_reset",
        "group_ids": [int(get("panel_group_id"))],
        "note": f"tg:{order['user_id']} users:{order['users']} order:{order['id']}",
    }
    async with httpx.AsyncClient(timeout=30, verify=True) as client:
        token = await panel_token(client)
        r = await client.post(
            f"{get('panel_url').rstrip('/')}/api/user",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code not in (200, 201):
            raise PanelError(f"create failed: {r.status_code} {r.text[:300]}")
        data = r.json()
    link = data.get("subscription_url", "")
    if link.startswith("/"):
        link = get("panel_url").rstrip("/") + link
    return username, link


# ----------------------------------------------------------------------------
# Keyboards (all inline / glass)
# ----------------------------------------------------------------------------
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def main_menu(is_admin=False):
    rows = [
        [(get("btn_buy"), "buy")],
        [(get("btn_my"), "my"), (get("btn_support"), "support")],
    ]
    if is_admin:
        rows.append([("⚙️ پنل مدیریت", "adm")])
    return kb(rows)


def back_row(target="home"):
    return [(get("btn_back"), target)]


# ----------------------------------------------------------------------------
# FSM
# ----------------------------------------------------------------------------
class Buy(StatesGroup):
    receipt = State()


class Admin(StatesGroup):
    value = State()


router = Router()


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ----------------------------- user side ------------------------------------
@router.message(Command("start"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(get("txt_start"), reply_markup=main_menu(is_admin(m.from_user.id)))


@router.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(get("txt_start"), reply_markup=main_menu(is_admin(c.from_user.id)))


@router.callback_query(F.data == "support")
async def support(c: CallbackQuery):
    await c.message.edit_text(get("txt_support"), reply_markup=kb([back_row()]))


@router.callback_query(F.data == "buy")
async def buy(c: CallbackQuery):
    rows = [[(f"{g} گیگ", f"g:{g}") for g in GB_OPTIONS[i:i + 2]] for i in range(0, len(GB_OPTIONS), 2)]
    rows.append(back_row())
    await c.message.edit_text(get("txt_choose_gb"), reply_markup=kb(rows))


@router.callback_query(F.data.startswith("g:"))
async def pick_gb(c: CallbackQuery):
    gb = c.data.split(":")[1]
    rows = [[(f"{d} روز", f"d:{gb}:{d}") for d in DAY_OPTIONS]]
    rows.append(back_row("buy"))
    await c.message.edit_text(get("txt_choose_days"), reply_markup=kb(rows))


@router.callback_query(F.data.startswith("d:"))
async def pick_days(c: CallbackQuery):
    _, gb, days = c.data.split(":")
    rows = [[(f"{u} کاربر", f"u:{gb}:{days}:{u}") for u in USER_OPTIONS]]
    rows.append(back_row(f"g:{gb}"))
    await c.message.edit_text(get("txt_choose_users"), reply_markup=kb(rows))


@router.callback_query(F.data.startswith("u:"))
async def pick_users(c: CallbackQuery):
    _, gb, days, users = c.data.split(":")
    gb, days, users = int(gb), int(days), int(users)
    usd, toman = calc_price(gb, days, users)
    text = get("txt_summary").format(gb=gb, days=days, users=users, usd=usd, toman=fmt(toman))
    rows = [
        [(get("btn_confirm"), f"ok:{gb}:{days}:{users}")],
        [(get("btn_cancel"), "home")],
    ]
    await c.message.edit_text(text, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("ok:"))
async def confirm(c: CallbackQuery, state: FSMContext):
    _, gb, days, users = c.data.split(":")
    gb, days, users = int(gb), int(days), int(users)
    usd, toman = calc_price(gb, days, users)
    cur = db.execute(
        "INSERT INTO orders(user_id,username,gb,days,users,usd,toman,created) VALUES(?,?,?,?,?,?,?,?)",
        (c.from_user.id, c.from_user.username or "", gb, days, users, usd, toman, int(time.time())),
    )
    db.commit()
    await state.set_state(Buy.receipt)
    await state.update_data(order_id=cur.lastrowid)
    text = get("txt_pay").format(toman=fmt(toman), card=get("card_info"))
    await c.message.edit_text(text, reply_markup=kb([[(get("btn_cancel"), "home")]]))


@router.message(Buy.receipt, F.photo)
async def receipt(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    oid = data["order_id"]
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    await state.clear()
    await m.answer(get("txt_receipt_ok"), reply_markup=main_menu(is_admin(m.from_user.id)))
    caption = (
        f"🧾 سفارش #{oid}\n"
        f"کاربر: {m.from_user.full_name} (@{m.from_user.username or '-'}) [{m.from_user.id}]\n"
        f"{o['gb']} گیگ / {o['days']} روز / {o['users']} کاربر\n"
        f"{o['usd']}$ = {fmt(o['toman'])} تومان"
    )
    markup = kb([[("✅ تایید", f"ap:{oid}"), ("❌ رد", f"rj:{oid}")]])
    for admin in ADMIN_IDS:
        try:
            await bot.send_photo(admin, m.photo[-1].file_id, caption=caption, reply_markup=markup)
        except Exception:
            pass


@router.message(Buy.receipt)
async def receipt_wrong(m: Message):
    await m.answer("لطفاً عکس رسید پرداخت را ارسال کنید.")


@router.callback_query(F.data == "my")
async def my_configs(c: CallbackQuery):
    rows = db.execute(
        "SELECT * FROM orders WHERE user_id=? AND status='approved' ORDER BY id DESC LIMIT 10",
        (c.from_user.id,),
    ).fetchall()
    if not rows:
        text = get("txt_no_configs")
    else:
        text = "\n\n".join(f"#{r['id']} — {r['gb']}GB / {r['days']}d\n{r['sub_link']}" for r in rows)
    await c.message.edit_text(text, reply_markup=kb([back_row()]), disable_web_page_preview=True)


# ----------------------------- admin: orders --------------------------------
@router.callback_query(F.data.startswith("ap:"))
async def approve(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        return await c.answer("دسترسی ندارید", show_alert=True)
    oid = int(c.data.split(":")[1])
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "pending":
        return await c.answer("این سفارش قبلاً بررسی شده", show_alert=True)
    try:
        uname, link = await panel_create_user(o)
    except Exception as e:
        return await c.answer(f"خطا در ساخت کانفیگ: {e}"[:200], show_alert=True)
    db.execute(
        "UPDATE orders SET status='approved', panel_username=?, sub_link=? WHERE id=?",
        (uname, link, oid),
    )
    db.commit()
    await bot.send_message(o["user_id"], get("txt_approved").format(link=link))
    await c.message.edit_caption(caption=(c.message.caption or "") + "\n\n✅ تایید و کانفیگ ساخته شد")


@router.callback_query(F.data.startswith("rj:"))
async def reject(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        return await c.answer("دسترسی ندارید", show_alert=True)
    oid = int(c.data.split(":")[1])
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "pending":
        return await c.answer("این سفارش قبلاً بررسی شده", show_alert=True)
    db.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
    db.commit()
    await bot.send_message(o["user_id"], get("txt_rejected"))
    await c.message.edit_caption(caption=(c.message.caption or "") + "\n\n❌ رد شد")


# ----------------------------- admin: panel ---------------------------------
def adm_menu():
    return kb([
        [("🌐 تنظیمات پنل پاسارگاد", "adm_panel")],
        [("💵 نرخ دلار", "adm_set:usd_rate"), ("💰 قیمت‌ها", "adm_prices")],
        [("💳 اطلاعات کارت", "adm_set:card_info")],
        [("✏️ ویرایش متن‌ها", "adm_texts"), ("🔘 نام دکمه‌ها", "adm_btns")],
        [("🔙 بازگشت", "home")],
    ])


@router.message(Command("admin"))
async def admin_cmd(m: Message):
    if is_admin(m.from_user.id):
        await m.answer("⚙️ پنل مدیریت", reply_markup=adm_menu())


@router.callback_query(F.data == "adm")
async def adm(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("دسترسی ندارید", show_alert=True)
    await state.clear()
    await c.message.edit_text("⚙️ پنل مدیریت", reply_markup=adm_menu())


@router.callback_query(F.data == "adm_panel")
async def adm_panel(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return
    text = (
        f"🌐 آدرس: {get('panel_url')}\n"
        f"👤 یوزرنیم: {get('panel_user')}\n"
        f"🔑 پسورد: {'*' * len(get('panel_pass'))}\n"
        f"🆔 Group ID: {get('panel_group_id')}"
    )
    await c.message.edit_text(text, reply_markup=kb([
        [("آدرس پنل", "adm_set:panel_url")],
        [("یوزرنیم", "adm_set:panel_user"), ("پسورد", "adm_set:panel_pass")],
        [("Group ID", "adm_set:panel_group_id")],
        [("🔙 بازگشت", "adm")],
    ]))


@router.callback_query(F.data == "adm_prices")
async def adm_prices(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return
    text = (
        f"💵 نرخ دلار: {fmt(int(float(get('usd_rate'))))} تومان\n"
        f"هر گیگ: {get('price_gb')}$\nهر روز: {get('price_day')}$\nهر کاربر: {get('price_user')}$"
    )
    await c.message.edit_text(text, reply_markup=kb([
        [("هر گیگ", "adm_set:price_gb"), ("هر روز", "adm_set:price_day")],
        [("هر کاربر", "adm_set:price_user")],
        [("🔙 بازگشت", "adm")],
    ]))


def list_menu(keys, back="adm"):
    rows = [[(LABELS[k], f"adm_set:{k}")] for k in keys]
    rows.append([("🔙 بازگشت", back)])
    return kb(rows)


@router.callback_query(F.data == "adm_texts")
async def adm_texts(c: CallbackQuery):
    if is_admin(c.from_user.id):
        await c.message.edit_text("کدام متن را ویرایش می‌کنید؟", reply_markup=list_menu(TEXTS.keys()))


@router.callback_query(F.data == "adm_btns")
async def adm_btns(c: CallbackQuery):
    if is_admin(c.from_user.id):
        await c.message.edit_text("نام کدام دکمه را تغییر می‌دهید؟", reply_markup=list_menu(BUTTONS.keys()))


@router.callback_query(F.data.startswith("adm_set:"))
async def adm_set(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return
    key = c.data.split(":", 1)[1]
    await state.set_state(Admin.value)
    await state.update_data(key=key)
    shown = get(key) if key != "panel_pass" else "***"
    hint = ""
    if key.startswith("txt_summary") or key == "txt_summary":
        hint = "\nمتغیرها: {gb} {days} {users} {usd} {toman}"
    elif key == "txt_pay":
        hint = "\nمتغیرها: {toman} {card}"
    elif key == "txt_approved":
        hint = "\nمتغیر: {link}"
    await c.message.edit_text(
        f"✏️ {LABELS[key]}\n\nمقدار فعلی:\n{shown}{hint}\n\nمقدار جدید را ارسال کنید:",
        reply_markup=kb([[("✖️ انصراف", "adm")]]),
    )


@router.message(Admin.value, F.text)
async def adm_value(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    key = (await state.get_data())["key"]
    val = m.text.strip()
    if key in ("usd_rate", "price_gb", "price_day", "price_user"):
        try:
            float(val.replace(",", ""))
        except ValueError:
            return await m.answer("⚠️ فقط عدد وارد کنید.")
        val = val.replace(",", "")
    if key == "panel_group_id" and not re.fullmatch(r"\d+", val):
        return await m.answer("⚠️ فقط عدد وارد کنید.")
    if key == "panel_url":
        if not val.startswith(("http://", "https://")):
            val = "https://" + val
        val = val.rstrip("/")
    put(key, val)
    await state.clear()
    await m.answer("✅ ذخیره شد.", reply_markup=adm_menu())


async def main():
    if not BOT_TOKEN or not ADMIN_IDS:
        raise SystemExit("BOT_TOKEN و ADMIN_IDS را در فایل .env تنظیم کنید.")
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
