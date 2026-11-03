import os
from dotenv import load_dotenv

load_dotenv()

def _int(name, default=0):
    return int(os.getenv(name, default))

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_ID = int(BOT_TOKEN.split(":")[0])

ADMIN_CHAT_ID = _int("ADMIN_CHAT_ID")
WORKERS_CHAT_ID = _int("WORKERS_CHAT_ID")
REPORTS_CHAT_ID = _int("REPORTS_CHAT_ID")
REPORTS_TOPIC_ID = _int("REPORTS_TOPIC_ID") or None
ADMIN_TOPIC_ID = _int("ADMIN_TOPIC_ID") or None      # тема для анкет и заявок на ЗП
PAYOUT_TOPIC_ID = _int("PAYOUT_TOPIC_ID") or None    # тема для команд юзерботу
PAYOUT_CHAT_ID = _int("PAYOUT_CHAT_ID")

ORDERS_FOR_PAYOUT = _int("ORDERS_FOR_PAYOUT", 4)
PAY_STARS = _int("PAY_STARS", 15)
# Кто может нажимать кнопки в админ-группе (id через запятую). Пусто = любой участник группы
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
UTC_OFFSET = _int("UTC_OFFSET_HOURS", 3)  # часовой пояс для «за сегодня» в статистике (Москва = 3)
MAX_GOAL = _int("MAX_GOAL", 40)  # максимальная цель накопления (в заказах)
POLL_SECONDS = _int("POLL_SECONDS", 30)  # как часто юзербот проверяет очередь выплат

API_ID = _int("API_ID")
API_HASH = os.getenv("API_HASH", "")
DB_PATH = os.getenv("DB_PATH", "shop.db")

# Шаблон анкеты — правь как хочешь
ANKETA_TEMPLATE = (
    "📝 <b>Анкета</b>\n\n"
    "Скопируй, заполни и отправь одним сообщением:\n\n"
    "<code>Имя:\n"
    "Возраст:\n"
    "Опыт работы:\n"
    "Сколько часов в день готов уделять:\n"
    "Часовой пояс:\n"
    "Почему хочешь к нам:</code>"
)
