import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ChatJoinRequest, Message, ReplyParameters
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
from config import *

router = Router()


class Form(StatesGroup):
    anketa = State()


class Report(StatesGroup):
    waiting = State()


class Goal(StatesGroup):
    custom = State()


class Prof(StatesGroup):
    value = State()


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{html.escape(name or str(uid))}</a>'


def kb(*rows):
    b = InlineKeyboardBuilder()
    for text, data in rows:
        if data.startswith("http"):
            b.button(text=text, url=data)
        else:
            b.button(text=text, callback_data=data)
    b.adjust(1)
    return b.as_markup()


def is_admin(uid):
    """Если в .env задан ADMIN_IDS — кнопки жмут только они, иначе любой участник админ-группы."""
    return not ADMIN_IDS or uid in ADMIN_IDS


async def safe_send(bot: Bot, chat_id, text, **kw):
    """Отправка, которая не роняет обработчик, если человек заблокировал бота."""
    try:
        return await bot.send_message(chat_id, text, **kw)
    except Exception:
        logging.exception("не удалось написать %s", chat_id)


def pair_kb(ok_text, ok_data, no_text, no_data):
    b = InlineKeyboardBuilder()
    b.button(text=ok_text, callback_data=ok_data)
    b.button(text=no_text, callback_data=no_data)
    b.adjust(2)
    return b.as_markup()


# ───────────── /start и анкета ─────────────

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    w = db.get_worker(m.from_user.id)
    if w and w["status"] == "approved":
        await m.answer("С возвращением! 👋", reply_markup=kb(("👤 Личный кабинет", "cab")))
    else:
        await m.answer("Привет! Хочешь работать в нашем шопе? 👇",
                       reply_markup=kb(("📝 Подать анкету", "apply")))


@router.callback_query(F.data == "apply")
async def apply(cb: CallbackQuery, state: FSMContext):
    w = db.get_worker(cb.from_user.id)
    if w and w["status"] == "pending":
        return await cb.answer("Твоя анкета уже на рассмотрении ⏳", show_alert=True)
    if w and w["status"] == "approved":
        return await cb.answer("Ты уже в команде!", show_alert=True)
    await state.set_state(Form.anketa)
    await cb.message.answer(ANKETA_TEMPLATE)  # анкета новым сообщением
    await cb.answer()


@router.message(Form.anketa, F.text)
async def got_anketa(m: Message, state: FSMContext, bot: Bot):
    u = m.from_user
    db.upsert_application(u.id, u.username, u.full_name, m.text)
    await state.clear()
    uname = f"@{u.username}" if u.username else "—"
    await bot.send_message(
        ADMIN_CHAT_ID,
        f"📨 <b>Новая анкета</b>\nОт: {mention(u.id, u.full_name)} ({uname}, <code>{u.id}</code>)\n\n"
        f"{html.escape(m.text)}",
        reply_markup=pair_kb("✅ Принять", f"rv:ok:{u.id}", "❌ Отклонить", f"rv:no:{u.id}"),
        message_thread_id=ADMIN_TOPIC_ID,
    )
    await m.answer("✅ Анкета отправлена! Ожидай ответа администрации.")


# ───────────── решение по анкете ─────────────

@router.callback_query(F.data.startswith("rv:"))
async def review(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != ADMIN_CHAT_ID or not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    _, action, uid = cb.data.split(":")
    uid = int(uid)
    w = db.get_worker(uid)
    if not w or w["status"] != "pending":
        return await cb.answer("Уже обработано", show_alert=True)

    admin = html.escape(cb.from_user.full_name)
    if action == "ok":
        db.set_status(uid, "approved")
        # ссылка с заявкой на вступление — её мы автоматически одобрим
        link = await bot.create_chat_invite_link(
            WORKERS_CHAT_ID, name=f"worker {uid}", creates_join_request=True)
        db.set_invite(uid, link.invite_link)
        await bot.send_message(
            uid, "🎉 Анкета одобрена! Вступай в группу, заявка примется автоматически:",
            reply_markup=kb(("➡️ Вступить в группу", link.invite_link)))
        await cb.message.edit_text(cb.message.html_text + f"\n\n✅ Принял: {admin}")
    else:
        db.set_status(uid, "rejected")
        await bot.send_message(uid, "😔 К сожалению, анкета отклонена.")
        await cb.message.edit_text(cb.message.html_text + f"\n\n❌ Отклонил: {admin}")
    await cb.answer()


# ───────────── авто-приём в группу ─────────────

@router.chat_join_request(F.chat.id == WORKERS_CHAT_ID)
async def join_request(req: ChatJoinRequest, bot: Bot):
    w = db.get_worker(req.from_user.id)
    if w and w["status"] == "approved":
        await req.approve()
        try:
            await bot.send_message(
                req.user_chat_id, "✅ Ты в группе! Твой личный кабинет:",
                reply_markup=kb(("👤 Личный кабинет", "cab")))
        except Exception:
            logging.exception("cannot DM worker")
    # чужих не трогаем — их заявки остаются на ручное рассмотрение


# ───────────── личный кабинет ─────────────

def approved(uid):
    w = db.get_worker(uid)
    return w if w and w["status"] == "approved" else None


def goal_of(w):
    """Цель накопления в заказах. Всегда кратна ORDERS_FOR_PAYOUT (округляем вверх, если в .env число меняли)."""
    step = ORDERS_FOR_PAYOUT
    g = w.get("goal") or step
    return max(step, -(-g // step) * step)


def stars_for(goal):
    """За каждые ORDERS_FOR_PAYOUT заказов платим PAY_STARS."""
    return PAY_STARS * goal // ORDERS_FOR_PAYOUT


def display_name(w, tg_name):
    return w.get("nickname") or tg_name


def pending_line(w):
    n = db.count_reports(w["user_id"], "pending")
    return f"⏳ Отчётов на проверке: <b>{n}</b>\n" if n else ""


def progress_bar(cur, goal, cells=10):
    filled = min(cells, cur * cells // goal)
    return "▰" * filled + "▱" * (cells - filled)


async def show(cb: CallbackQuery, text, markup):
    """Обновляет текущее сообщение (экран кабинета), а не плодит новые."""
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


# ── главный экран ──

def cabinet_text(w, tg_name):
    goal = goal_of(w)
    return (
        f"👤 <b>{html.escape(display_name(w, tg_name))}</b>\n\n"
        f"🎯 Цель: <b>{goal} заказов → {stars_for(goal)}⭐</b>\n"
        f"{progress_bar(w['orders'], goal)}  <b>{w['orders']}/{goal}</b>\n"
        f"{pending_line(w)}\n"
        f"📊 Всего заказов: <b>{w['total_orders']}</b>\n"
        f"⭐ Выплачено всего: <b>{w['total_paid']}</b>\n\n"
        f"Когда наберёшь {goal}/{goal}, заявка на ЗП уйдёт админам автоматически."
    )


def cabinet_kb():
    return kb(("📦 Прислать отчёт по заказу", "report"),
              ("🎯 Моя цель", "goal"),
              ("⚙️ Профиль", "prof"))


@router.callback_query(F.data == "cab")
async def cabinet(cb: CallbackQuery, state: FSMContext):
    w = approved(cb.from_user.id)
    if not w:
        return await cb.answer("Нет доступа", show_alert=True)
    await state.clear()
    await show(cb, cabinet_text(w, cb.from_user.full_name), cabinet_kb())


# ── цель накопления ──

def goal_text(w):
    goal, step = goal_of(w), ORDERS_FOR_PAYOUT
    return (
        f"🎯 <b>Моя цель</b>\n\n"
        f"Сейчас: <b>{goal} заказов → {stars_for(goal)}⭐</b> (набрано {w['orders']})\n\n"
        f"Цель должна быть кратна {step}: {step}, {step * 2}, {step * 3}… до {MAX_GOAL}.\n"
        f"За каждые {step} заказов — {PAY_STARS}⭐. Чем больше цель, тем реже и крупнее выплата.\n"
        f"Цель нельзя поставить меньше уже набранных заказов."
    )


def goal_kb(current):
    step = ORDERS_FOR_PAYOUT
    b = InlineKeyboardBuilder()
    for goal in [step * i for i in range(1, 7) if step * i <= MAX_GOAL]:
        mark = "✅ " if goal == current else ""
        b.button(text=f"{mark}{goal} → {stars_for(goal)}⭐", callback_data=f"goal:set:{goal}")
    b.button(text="✏️ Своё число", callback_data="goal:custom")
    b.button(text="⬅️ Назад", callback_data="cab")
    b.adjust(2, 2, 2, 1, 1)
    return b.as_markup()


def apply_goal(uid, goal):
    """Возвращает текст ошибки или None, если цель сохранена."""
    w = approved(uid)
    step = ORDERS_FOR_PAYOUT
    if not w:
        return "Нет доступа"
    if goal % step or not (step <= goal <= MAX_GOAL):
        return f"Цель должна быть кратна {step}, от {step} до {MAX_GOAL}"
    if goal < w["orders"]:
        return f"У тебя уже {w['orders']} заказов — выбери цель не меньше этого числа"
    db.set_goal(uid, goal)
    return None


async def maybe_request_payout(bot: Bot, uid):
    """Если заказов набрано на цель — создаёт заявку на ЗП и шлёт админам. Возвращает число заявок."""
    created = 0
    while True:
        w = approved(uid)
        if not w:
            break
        goal = goal_of(w)
        if w["orders"] < goal:
            break
        stars = stars_for(goal)
        db.change_orders(uid, -goal)
        pid = db.create_payout(uid, stars, goal)
        uname = f"@{w['username']}" if w.get("username") else "—"
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"💰 <b>Заявка на ЗП #{pid}</b>\n"
            f"{mention(uid, display_name(w, w['full_name']))} ({uname}) набрал {goal}/{goal}\n"
            f"Сумма: <b>{stars}⭐</b>",
            reply_markup=pair_kb("✅ Выплатить", f"po:ok:{pid}", "❌ Отказать", f"po:no:{pid}"),
            message_thread_id=ADMIN_TOPIC_ID)
        await safe_send(bot, uid, f"🎯 Цель {goal}/{goal} выполнена! Заявка на {stars}⭐ отправлена админам.")
        created += 1
    return created


@router.callback_query(F.data == "goal")
async def goal_screen(cb: CallbackQuery, state: FSMContext):
    w = approved(cb.from_user.id)
    if not w:
        return await cb.answer("Нет доступа", show_alert=True)
    await state.clear()
    await show(cb, goal_text(w), goal_kb(goal_of(w)))


@router.callback_query(F.data.startswith("goal:set:"))
async def goal_set(cb: CallbackQuery, bot: Bot):
    err = apply_goal(cb.from_user.id, int(cb.data.split(":")[2]))
    if err:
        return await cb.answer(err, show_alert=True)
    await maybe_request_payout(bot, cb.from_user.id)  # вдруг заказов уже хватает
    w = approved(cb.from_user.id)
    await show(cb, goal_text(w), goal_kb(goal_of(w)))


@router.callback_query(F.data == "goal:custom")
async def goal_custom(cb: CallbackQuery, state: FSMContext):
    if not approved(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await state.set_state(Goal.custom)
    await cb.message.answer(f"Напиши число заказов, кратное {ORDERS_FOR_PAYOUT} "
                            f"(от {ORDERS_FOR_PAYOUT} до {MAX_GOAL}). Отмена — /start")
    await cb.answer()


@router.message(Goal.custom, F.text)
async def goal_custom_got(m: Message, state: FSMContext, bot: Bot):
    if not m.text.strip().isdigit():
        return await m.answer("Нужно число, например 12")
    err = apply_goal(m.from_user.id, int(m.text.strip()))
    if err:
        return await m.answer(f"❌ {err}")  # остаёмся в режиме ввода — можно попробовать ещё раз
    await state.clear()
    await maybe_request_payout(bot, m.from_user.id)
    w = approved(m.from_user.id)
    await m.answer("✅ Цель сохранена!\n\n" + goal_text(w), reply_markup=goal_kb(goal_of(w)))


# ── профиль ──

PROFILE_FIELDS = {
    "nickname": ("Псевдоним", 32, "Как к тебе обращаться (2–32 символа). Он будет в отчётах и заявках."),
    "timezone": ("Часовой пояс", 40, "Например: Москва, UTC+3"),
    "about": ("О себе", 300, "Пара слов о себе (до 300 символов)"),
}


def profile_text(w, u):
    def val(k):
        return html.escape(w[k]) if w.get(k) else "<i>не указано</i>"
    uname = f"@{u.username}" if u.username else "—"
    return (
        f"⚙️ <b>Профиль</b>\n\n"
        f"Псевдоним: {val('nickname')}\n"
        f"Часовой пояс: {val('timezone')}\n"
        f"О себе: {val('about')}\n\n"
        f"Telegram: {uname} (<code>{u.id}</code>)"
    )


def profile_kb():
    return kb(("✏️ Псевдоним", "prof:edit:nickname"),
              ("🕐 Часовой пояс", "prof:edit:timezone"),
              ("📝 О себе", "prof:edit:about"),
              ("⬅️ Назад", "cab"))


@router.callback_query(F.data == "prof")
async def profile_screen(cb: CallbackQuery, state: FSMContext):
    w = approved(cb.from_user.id)
    if not w:
        return await cb.answer("Нет доступа", show_alert=True)
    await state.clear()
    await show(cb, profile_text(w, cb.from_user), profile_kb())


@router.callback_query(F.data.startswith("prof:edit:"))
async def profile_edit(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[2]
    if field not in PROFILE_FIELDS or not approved(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    label, _, hint = PROFILE_FIELDS[field]
    await state.set_state(Prof.value)
    await state.update_data(field=field)
    await cb.message.answer(f"<b>{label}</b>\n{hint}\n\nЧтобы очистить поле, отправь «-». Отмена — /start")
    await cb.answer()


@router.message(Prof.value, F.text)
async def profile_got(m: Message, state: FSMContext):
    field = (await state.get_data()).get("field")
    if field not in PROFILE_FIELDS or not approved(m.from_user.id):
        return await state.clear()
    label, limit, _ = PROFILE_FIELDS[field]
    value = m.text.strip()
    if value == "-":
        value = None
    elif field == "nickname" and len(value) < 2:
        return await m.answer("Слишком коротко: минимум 2 символа. Попробуй ещё раз.")
    elif len(value) > limit:
        return await m.answer(f"Слишком длинно: максимум {limit} символов. Попробуй ещё раз.")
    db.update_profile(m.from_user.id, field, value)
    await state.clear()
    await m.answer("✅ Сохранено!\n\n" + profile_text(approved(m.from_user.id), m.from_user),
                   reply_markup=profile_kb())


# ── отчёты ──

@router.callback_query(F.data == "report")
async def report_start(cb: CallbackQuery, state: FSMContext):
    if not approved(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await state.set_state(Report.waiting)
    await cb.message.answer("Отправь отчёт по заказу одним сообщением (текст / фото / скрин).\n"
                            "Отмена — /start")
    await cb.answer()


@router.message(Report.waiting)
async def report_got(m: Message, state: FSMContext, bot: Bot):
    w = approved(m.from_user.id)
    if not w:
        return await state.clear()
    await state.clear()
    u = m.from_user
    uname = f"@{u.username}" if u.username else "—"

    rid = db.create_report(u.id)   # статус pending: в счётчик заказ попадёт только после одобрения
    try:
        copied = await m.copy_to(REPORTS_CHAT_ID, message_thread_id=REPORTS_TOPIC_ID)
        head = await bot.send_message(
            REPORTS_CHAT_ID,
            f"📦 <b>Отчёт #{rid}</b> — ⏳ на проверке\n"
            f"От: {mention(u.id, display_name(w, u.full_name))} ({uname})\n"
            f"Заказов у работника: {w['orders']}/{goal_of(w)}",
            reply_markup=pair_kb("✅ Принять", f"rp:ok:{rid}", "❌ Отклонить", f"rp:no:{rid}"),
            message_thread_id=REPORTS_TOPIC_ID,
            reply_parameters=ReplyParameters(message_id=copied.message_id))
        db.set_report_header(rid, head.message_id)
    except Exception:
        logging.exception("cannot post report")
        db.review_report(rid, "rejected", 0)
        return await m.answer("😔 Не удалось отправить отчёт, попробуй ещё раз чуть позже.")

    await m.answer(f"📨 Отчёт #{rid} отправлен на проверку. Заказ засчитается после одобрения.",
                   reply_markup=kb(("👤 Личный кабинет", "cab")))


# ───────────── проверка отчётов админами ─────────────

@router.callback_query(F.data.startswith("rp:"))
async def report_review(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != REPORTS_CHAT_ID or not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    _, action, rid = cb.data.split(":")
    rid = int(rid)
    r = db.get_report(rid)
    if not r:
        return await cb.answer("Отчёт не найден", show_alert=True)

    status = "approved" if action == "ok" else "rejected"
    if not db.review_report(rid, status, cb.from_user.id):   # атомарно: второй клик ничего не сделает
        return await cb.answer("Уже обработано", show_alert=True)

    uid = r["user_id"]
    admin = html.escape(cb.from_user.full_name)
    if status == "approved":
        cur = db.add_order(uid)
        w = db.get_worker(uid)
        await safe_send(bot, uid, f"✅ Отчёт #{rid} принят! Заказов: <b>{cur}/{goal_of(w)}</b>")
        new, mark = "✅ принят", f"✅ Принял: {admin}"
    else:
        await safe_send(bot, uid, f"❌ Отчёт #{rid} отклонён. Проверь данные и отправь отчёт заново.")
        new, mark = "❌ отклонён", f"❌ Отклонил: {admin}"

    await cb.message.edit_text(cb.message.html_text.replace("⏳ на проверке", new) + f"\n\n{mark}")
    await cb.answer()
    if status == "approved":
        await maybe_request_payout(bot, uid)   # вдруг цель достигнута


@router.message(F.chat.id == REPORTS_CHAT_ID, F.reply_to_message, F.text)
async def report_comment(m: Message, bot: Bot):
    """Админ отвечает (reply) на сообщение с отчётом — текст уходит работнику как комментарий."""
    if not m.from_user or not is_admin(m.from_user.id):
        return
    r = db.get_report_by_header(m.reply_to_message.message_id)
    if not r:
        return
    await safe_send(bot, r["user_id"],
                    f"💬 Комментарий администратора к отчёту #{r['id']}:\n{html.escape(m.text)}")
    await m.reply("📨 Передал работнику")


# ───────────── решение по выплате ─────────────

@router.callback_query(F.data.startswith("po:"))
async def payout_review(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != ADMIN_CHAT_ID or not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    _, action, pid = cb.data.split(":")
    p = db.get_payout(int(pid))
    if not p or p["status"] != "requested":
        return await cb.answer("Уже обработано", show_alert=True)
    admin = html.escape(cb.from_user.full_name)

    if action == "ok":
        db.set_payout_status(p["id"], "queued")
        # команда для юзербота (аналог «@юз зарплата 15 звезд»)
        await bot.send_message(PAYOUT_CHAT_ID, f"зарплата {p['user_id']} {p['stars']} {p['id']}",
                               message_thread_id=PAYOUT_TOPIC_ID)
        await bot.send_message(p["user_id"], "✅ Заявка на ЗП одобрена, выдача в процессе…")
        await cb.message.edit_text(cb.message.html_text + f"\n\n✅ Одобрил: {admin}")
    else:
        db.set_payout_status(p["id"], "rejected")
        db.change_orders(p["user_id"], p["orders_count"] or ORDERS_FOR_PAYOUT)  # возвращаем заказы
        await bot.send_message(p["user_id"], "❌ Заявка на ЗП отклонена, заказы возвращены в счётчик.")
        await cb.message.edit_text(cb.message.html_text + f"\n\n❌ Отказал: {admin}")
    await cb.answer()


# ───────────── повтор / отмена неудачной выплаты ─────────────

@router.callback_query(F.data.startswith("pr:"))
async def payout_retry(cb: CallbackQuery):
    if cb.message.chat.id != ADMIN_CHAT_ID or not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    p = db.get_payout(int(cb.data.split(":")[1]))
    if not p or p["status"] != "failed":
        return await cb.answer("Уже обработано", show_alert=True)
    db.set_payout_status(p["id"], "queued")  # юзербот подхватит из очереди сам
    await cb.message.edit_text(
        cb.message.html_text +
        f"\n\n🔁 Повтор запущен: {html.escape(cb.from_user.full_name)} (до {POLL_SECONDS} сек.)")
    await cb.answer("Поставлено в очередь")


@router.callback_query(F.data.startswith("pc:"))
async def payout_cancel(cb: CallbackQuery, bot: Bot):
    if cb.message.chat.id != ADMIN_CHAT_ID or not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    p = db.get_payout(int(cb.data.split(":")[1]))
    if not p or p["status"] != "failed":
        return await cb.answer("Уже обработано", show_alert=True)
    if p["sent"]:
        return await cb.answer("Часть уже выдана — отменить нельзя, только повторить", show_alert=True)
    db.set_payout_status(p["id"], "cancelled")
    db.change_orders(p["user_id"], p["orders_count"] or ORDERS_FOR_PAYOUT)  # заказы возвращаем работнику
    await bot.send_message(p["user_id"], "❌ Выплата отменена, заказы возвращены в счётчик.")
    await cb.message.edit_text(
        cb.message.html_text + f"\n\n↩️ Отменил: {html.escape(cb.from_user.full_name)}")
    await cb.answer()


# ───────────── статистика и служебные команды ─────────────

def fmt_ago(sec):
    if sec < 90:
        return f"{sec} сек."
    if sec < 5400:
        return f"{sec // 60} мин."
    return f"{sec // 3600} ч."


def row_name(r):
    if r.get("nickname"):
        return r["nickname"]
    if r.get("full_name"):
        return r["full_name"]
    return f"@{r['username']}" if r.get("username") else str(r["user_id"])


def userbot_lines():
    seen = db.kv_get("userbot_seen")
    if seen:
        ago = max(0, int(time.time()) - int(seen))
        if ago <= max(90, POLL_SECONDS * 3):
            status = f"🟢 онлайн (последний отклик {fmt_ago(ago)} назад)"
        else:
            status = f"🔴 не отвечает уже {fmt_ago(ago)} — проверь окно юзербота"
    else:
        status = "⚪ нет данных (юзербот ещё не запускался с новой версией)"
    bal = db.kv_get("userbot_balance")
    return [status, f"Баланс: {bal}⭐" if bal is not None else "Баланс: неизвестен"]


def stats_text():
    st = db.stats(UTC_OFFSET)
    now = datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET)

    def g(d, k):
        return d.get(k, 0)

    wk, rt, rw, ra = st["workers"], st["reports_today"], st["reports_week"], st["reports_all"]
    lines = [
        f"📊 <b>Статистика</b> · {now:%d.%m %H:%M}",
        "",
        "👥 <b>Работники</b>",
        f"Принято: {g(wk, 'approved')} · На рассмотрении: {g(wk, 'pending')} · Отклонено: {g(wk, 'rejected')}",
        "",
        "📦 <b>Отчёты</b>",
        f"Сегодня: ✅ {g(rt, 'approved')} · ⏳ {g(rt, 'pending')} · ❌ {g(rt, 'rejected')}",
        f"За 7 дней: ✅ {g(rw, 'approved')} · ❌ {g(rw, 'rejected')}",
        f"Принято за всё время: {g(ra, 'approved')} · Ждут проверки: {g(ra, 'pending')}",
        "",
        "💸 <b>Выплаты</b>",
        f"Выдано всего: {st['paid_stars']}⭐ ({st['paid_count']} шт.)",
        f"Сегодня: {st['paid_today']}⭐",
        f"В ожидании: {st['pending_count']} ({st['pending_stars']}⭐) · С ошибкой: {st['failed_count']}",
        "",
        "🤖 <b>Юзербот</b>",
        *userbot_lines(),
    ]
    if st["top"]:
        lines += ["", "🏆 <b>Топ работников</b>"]
        for i, r in enumerate(st["top"], 1):
            lines.append(f"{i}. {html.escape(row_name(r))} — {r['total_orders']} заказов, {r['total_paid']}⭐")
    return "\n".join(lines)


def stats_kb():
    return kb(("🔄 Обновить", "st"))


def can_see_stats(chat, user):
    if not user:
        return False
    if chat.id == ADMIN_CHAT_ID:
        return is_admin(user.id)
    return chat.type == "private" and user.id in ADMIN_IDS


@router.message(Command("stats"))
async def stats_cmd(m: Message):
    if can_see_stats(m.chat, m.from_user):
        await m.answer(stats_text(), reply_markup=stats_kb())


@router.callback_query(F.data == "st")
async def stats_refresh(cb: CallbackQuery):
    if not can_see_stats(cb.message.chat, cb.from_user):
        return await cb.answer("Нет прав", show_alert=True)
    await show(cb, stats_text(), stats_kb())


@router.message(Command("id"))
async def my_id(m: Message):
    """Помогает узнать свой ID (для ADMIN_IDS) и ID группы (для .env)."""
    await m.answer(f"Твой ID: <code>{m.from_user.id}</code>\nID этого чата: <code>{m.chat.id}</code>")


async def main():
    logging.basicConfig(level=logging.INFO)
    db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
