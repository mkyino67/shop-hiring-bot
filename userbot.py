"""Юзербот: дарит подарки за звёзды с аккаунта шопа.

Как он находит работу:
  1) мгновенно — по команде бота в PAYOUT_CHAT_ID:  «зарплата <user_id> <звёзды> <id>»
  2) каждые POLL_SECONDS секунд — сам просматривает базу и берёт все выплаты со статусом queued
     (так подхватываются выплаты, пришедшие пока юзербот был выключен, и повторы после ошибок).

Защита от двойной выплаты: статус queued -> processing меняется атомарно, а в базе
хранится, сколько звёзд уже выдано (sent) — повтор доплачивает только остаток.
"""
import asyncio
import logging
import re
import time

import httpx
from pyrogram import Client, filters, idle
from pyrogram.errors import PeerIdInvalid
from pyrogram.handlers import MessageHandler

import db
from config import *

logging.basicConfig(level=logging.INFO)
app = None               # клиент создаётся внутри main(), чтобы быть в том же event loop

PAY_RE = re.compile(r"^зарплата\s+(\d+)\s+(\d+)\s+(\d+)$", re.I)
pay_lock = None          # создаётся в main(): выплаты идут строго по одной
warned = set()           # о каких выплатах уже предупредили «нет звёзд» (чтобы не спамить)


# ───────────── уведомления через Bot API ─────────────

async def bot_api(chat_id, text, buttons=None):
    data = {"chat_id": chat_id, "text": text}
    if chat_id == ADMIN_CHAT_ID and ADMIN_TOPIC_ID:
        data["message_thread_id"] = ADMIN_TOPIC_ID
    if buttons:  # кнопки обработает bot.py (pr: = повторить, pc: = отменить)
        data["reply_markup"] = {"inline_keyboard": [
            [{"text": t, "callback_data": d} for t, d in buttons]]}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=data)
    except Exception:
        logging.exception("Не удалось отправить уведомление")


# ───────────── подарки и баланс ─────────────

balance_warned = False


async def get_balance():
    """Баланс звёзд аккаунта. Если у версии библиотеки нет такого метода — None (проверка пропускается)."""
    global balance_warned
    try:
        return await app.get_stars_balance()
    except Exception as e:
        if not balance_warned:   # не спамим в лог каждые 30 секунд
            logging.warning("Не удалось проверить баланс звёзд: %s", e)
            balance_warned = True
        return None


def gift_price(g):
    """Цена подарка в звёздах. В разных версиях библиотеки поле называется по-разному."""
    for name in ("star_count", "price", "stars"):
        v = getattr(g, name, None)
        if isinstance(v, int):
            return v
    fields = [a for a in dir(g) if not a.startswith("_")]
    raise AttributeError(f"не нашёл цену у подарка, поля объекта: {fields}")


async def split_into_gifts(stars):
    """Раскладывает сумму на обычные (не лимитированные) подарки: [(gift_id, цена), ...]."""
    gifts = await app.get_available_gifts()
    by_price = {}
    for g in gifts:
        if getattr(g, "is_limited", False):
            continue
        by_price.setdefault(gift_price(g), g.id)
    left, result = stars, []
    for price in sorted(by_price, reverse=True):
        while left >= price:
            result.append((by_price[price], price))
            left -= price
    if left:
        raise ValueError(f"Сумму {stars}⭐ нельзя набрать подарками (доступные цены: {sorted(by_price)})")
    return result


async def send_gift_safe(user_id, gift_id):
    try:
        await app.send_gift(user_id, gift_id)
    except PeerIdInvalid:
        # юзербот ещё не «видел» человека — подтягиваем участников группы работников
        async for _m in app.get_chat_members(WORKERS_CHAT_ID):
            pass
        await app.send_gift(user_id, gift_id)


# ───────────── сама выплата ─────────────

async def process_payout(pid):
    async with pay_lock:
        if not db.claim_payout(pid):      # уже обработана или не в очереди
            return
        p = db.get_payout(pid)
        uid, total = p["user_id"], p["stars"]
        need = total - p["sent"]          # при повторе — только остаток

        # 1) хватает ли звёзд? Если нет — выплата остаётся в очереди и пройдёт сама после пополнения
        balance = await get_balance()
        if balance is not None and balance < need:
            db.set_payout_status(pid, "queued")
            if pid not in warned:
                warned.add(pid)
                await bot_api(ADMIN_CHAT_ID,
                              f"⚠️ Выплата #{pid}: не хватает звёзд (нужно {need}⭐, на аккаунте {balance}⭐).\n"
                              f"Пополните баланс юзербота — выплата пройдёт автоматически.")
            return
        warned.discard(pid)

        # 2) дарим подарки по одному, запоминая каждый успешный
        try:
            for gift_id, price in await split_into_gifts(need):
                await send_gift_safe(uid, gift_id)
                db.add_sent(pid, price)
        except Exception as e:
            logging.exception("Выплата %s не прошла", pid)
            db.set_payout_status(pid, "failed")
            sent = db.get_payout(pid)["sent"]
            text = f"❌ Выплата #{pid} не прошла: {e}\nУже выдано: {sent}⭐ из {total}⭐."
            buttons = [("🔁 Повторить", f"pr:{pid}")]
            if sent:
                text += "\nПовтор доплатит только остаток."
            else:
                buttons.append(("↩️ Отменить", f"pc:{pid}"))
            await bot_api(ADMIN_CHAT_ID, text, buttons)
            return

        # 3) успех
        db.mark_paid(pid)
        db.add_paid(uid, total)
        await bot_api(uid, f"💸 Зарплата выдана: {total}⭐ подарками. Проверь профиль!")
        await bot_api(ADMIN_CHAT_ID, f"✅ Выплата #{pid} ({total}⭐) выдана пользователю {uid}")


async def on_command(_, m):
    if not m.from_user or m.from_user.id != BOT_ID:   # слушаем только своего бота
        return
    mt = PAY_RE.match(m.text.strip())
    if not mt:
        return
    uid, stars, pid = map(int, mt.groups())
    p = db.get_payout(pid)
    if p and p["user_id"] == uid and p["stars"] == stars:   # данные в команде должны совпасть с базой
        await process_payout(pid)


# ───────────── подхват зависших и очередь ─────────────

async def recover_after_crash():
    """Выплаты в статусе processing остались от аварийной остановки — автоматически НЕ повторяем:
    возможно, подарок уже ушёл. Отдаём решение админу."""
    for p in db.list_payouts("processing"):
        db.set_payout_status(p["id"], "failed")
        await bot_api(ADMIN_CHAT_ID,
                      f"⚠️ Выплата #{p['id']} прервалась из-за остановки юзербота.\n"
                      f"По базе выдано {p['sent']}⭐ из {p['stars']}⭐.\n"
                      f"Сначала проверьте отправленные подарки на аккаунте, потом повторяйте.",
                      [("🔁 Повторить", f"pr:{p['id']}")])


async def poller():
    while True:
        try:
            db.kv_set("userbot_seen", int(time.time()))     # «пульс» для /stats в боте
            bal = await get_balance()
            if bal is not None:
                db.kv_set("userbot_balance", bal)
            for p in db.list_payouts("queued"):
                await process_payout(p["id"])
        except Exception:
            logging.exception("Ошибка в очереди выплат")
        await asyncio.sleep(POLL_SECONDS)


async def main():
    global app, pay_lock
    pay_lock = asyncio.Lock()
    db.init()
    app = Client("shop_userbot", api_id=API_ID, api_hash=API_HASH)
    app.add_handler(MessageHandler(on_command, filters.chat(PAYOUT_CHAT_ID) & filters.text))
    async with app:
        me = await app.get_me()
        logging.info("Юзербот запущен как %s (id %s)", me.first_name, me.id)
        await recover_after_crash()
        task = asyncio.create_task(poller())
        await idle()
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
