"""Проверка настроек перед запуском:  python check.py"""
import httpx
from config import *


def api(method, **params):
    try:
        return httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                          json=params, timeout=15).json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


def ok(t): print("✅", t)
def bad(t): print("❌", t)


me = api("getMe")
if not me.get("ok"):
    bad("BOT_TOKEN неверный — скопируйте токен из @BotFather заново")
    raise SystemExit(1)
ok(f"Бот найден: @{me['result']['username']}")

chats = [
    ("ADMIN_CHAT_ID (админ-группа)", ADMIN_CHAT_ID, False),
    ("WORKERS_CHAT_ID (группа работников)", WORKERS_CHAT_ID, True),
    ("REPORTS_CHAT_ID (группа отчётов)", REPORTS_CHAT_ID, False),
    ("PAYOUT_CHAT_ID (чат выплат)", PAYOUT_CHAT_ID, False),
]
for name, cid, need_admin in chats:
    if not cid:
        bad(f"{name}: не заполнен в .env")
        continue
    if cid > 0:
        bad(f"{name}: ID группы должен быть отрицательным (вида -100...)")
        continue
    r = api("getChat", chat_id=cid)
    if not r.get("ok"):
        bad(f"{name}: бот не видит чат — добавьте бота в группу или проверьте ID ({r.get('description')})")
        continue
    m = api("getChatMember", chat_id=cid, user_id=BOT_ID).get("result", {})
    status = m.get("status")
    if status in ("left", "kicked", None):
        bad(f"{name}: бота нет в группе")
    elif need_admin and status != "creator" and not (status == "administrator" and m.get("can_invite_users")):
        bad(f"{name}: сделайте бота админом с правом «Приглашать пользователей»")
    else:
        ok(f"{name}: «{r['result'].get('title')}» — ок")
    if cid == REPORTS_CHAT_ID and REPORTS_TOPIC_ID and not r["result"].get("is_forum"):
        bad("REPORTS_TOPIC_ID задан, но в группе отчётов не включены «Темы»")

if not API_ID or not API_HASH:
    bad("API_ID / API_HASH не заполнены (берутся на my.telegram.org)")
else:
    ok("API_ID и API_HASH заполнены")
print("\nГотово. Если везде ✅ — можно запускать.")
