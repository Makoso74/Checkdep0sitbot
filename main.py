import telebot
from telebot import types
import sqlite3
from datetime import datetime, date
import os, time, threading, logging
import csv
import io

# ================= CONFIG =================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN required")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
logging.basicConfig(level=logging.INFO)

DB_PATH = "saas.db"
state = {}
lock = threading.Lock()
TIMEOUT = 600

# ================= DB =================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS deposits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            fio TEXT,
            bank TEXT,
            amount REAL,
            rate REAL,
            currency TEXT,
            end_date TEXT,
            start_date TEXT,
            status TEXT DEFAULT 'active'
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS notified(
            deposit_id INTEGER,
            notified_date TEXT,
            PRIMARY KEY (deposit_id, notified_date)
        )
        """)
        conn.commit()

init_db()

# ================= STATE =================
def safe_get(uid):
    with lock:
        s = state.get(uid)
        if not s:
            return None
        if time.time() - s.get("ts", 0) > TIMEOUT:
            state.pop(uid, None)
            return None
        return s


def safe_set(uid, data):
    with lock:
        data["ts"] = time.time()
        state[uid] = data


def safe_pop(uid):
    with lock:
        state.pop(uid, None)


def touch(uid):
    with lock:
        if uid in state:
            state[uid]["ts"] = time.time()


def force_cleanup(uid):
    with lock:
        state.pop(uid, None)


def reset_session(uid, msg=None, chat_id=None):
    force_cleanup(uid)
    if msg and chat_id:
        bot.send_message(chat_id, msg, reply_markup=menu())

# ================= SAFE PARSERS =================
def safe_parse_number(x):
    try:
        return float(str(x).replace(",", "."))
    except:
        return None


def safe_parse_date(x):
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(x.strip(), fmt).date().isoformat()
        except:
            pass
    return None

# ================= CLEANER =================
def cleaner():
    while True:
        now = time.time()
        with lock:
            for uid in list(state.keys()):
                if now - state[uid].get("ts", 0) > TIMEOUT:
                    state.pop(uid, None)
        time.sleep(30)

threading.Thread(target=cleaner, daemon=True).start()

# ================= UI =================
def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Добавить", "📋 Мои")
    kb.add("✏️ Редактировать", "🗑 Удалить")
    kb.add("📤 Экспорт CSV", "📊 Статистика")
    return kb


def edit_kb(dep_id):
    kb = types.InlineKeyboardMarkup()
    fields = ["fio", "bank", "amount", "rate", "currency", "end_date"]
    names = ["ФИО", "Банк", "Сумма", "Ставка", "Валюта", "Дата"]

    for f, n in zip(fields, names):
        kb.add(types.InlineKeyboardButton(n, callback_data=f"edit|{dep_id}|{f}"))
    return kb

# ================= START =================
@bot.message_handler(commands=["start"])
def start(m):
    reset_session(m.from_user.id)
    bot.send_message(m.chat.id, "🚀 Бот депозитов запущен", reply_markup=menu())


@bot.message_handler(commands=["cancel"])
def cancel(m):
    reset_session(m.from_user.id, "❌ Отменено", m.chat.id)

# ================= SHOW =================
def show(uid, chat_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM deposits WHERE user_id=? AND status='active' ORDER BY end_date ASC",
            (uid,)
        ).fetchall()

    if not rows:
        return bot.send_message(chat_id, "Нет активных депозитов.")

    today = datetime.today().date()

    grouped = {}
    for r in rows:
        cur = r["currency"]
        if cur not in grouped:
            grouped[cur] = []
        grouped[cur].append(r)

    msg = "<b>📋 Депозиты:</b>\n\n"

    for currency, deposits in grouped.items():
        msg += f"<b>{currency}</b>\n"
        for r in deposits:
            try:
                end = datetime.fromisoformat(r["end_date"]).date()
                days = (end - today).days
            except:
                days = None

            if days is None:
                icon = "📅"
            elif days < 0:
                icon = "🔴"
            elif days == 0:
                icon = "🔴"
            elif days <= 7:
                icon = "🟡"
            else:
                icon = "🟢"

            try:
                fmt_date = datetime.fromisoformat(r["end_date"]).strftime("%d.%m.%y")
            except:
                fmt_date = r["end_date"]
            msg += f"{icon} #{r['id']} {r['fio']} • {r['amount']} • {r['rate']}% • {fmt_date}\n"

    bot.send_message(chat_id, msg)

# ================= ROUTER =================
COMMANDS = {
    "➕ Добавить": lambda m: add_start(m),
    "📋 Мои": lambda m: show(m.from_user.id, m.chat.id),
    "✏️ Редактировать": lambda m: edit_start(m),
    "🗑 Удалить": lambda m: delete_start(m),
    "📤 Экспорт CSV": lambda m: csv_export(m),
    "📊 Статистика": lambda m: stats(m)
}


@bot.message_handler(content_types=["text"])
def router(m):
    uid = m.from_user.id
    text = m.text.strip()

    if text.startswith("/"):
        reset_session(uid)
        return

    if text in COMMANDS:
        reset_session(uid)
        return COMMANDS[text](m)

    s = safe_get(uid)
    if not s:
        return

    flow(m, s)

# ================= FLOW =================
def flow(m, s):
    try:
        if s.get("action") not in ["add", "edit", "delete"]:
            return reset_session(m.from_user.id, "⚠️ Сессия сброшена", m.chat.id)

        if s["action"] == "add":
            return handle_add(m, s)
        elif s["action"] == "edit":
            return handle_edit(m, s)
        elif s["action"] == "delete":
            return handle_delete(m, s)

    except Exception as e:
        logging.exception(e)
        reset_session(m.from_user.id, "❌ Ошибка", m.chat.id)
    finally:
        touch(m.from_user.id)

# ================= ADD =================
def add_start(m):
    safe_set(m.from_user.id, {"action": "add", "step": 1, "data": {}})
    bot.send_message(m.chat.id, "👤 Введите ФИО:")


def handle_add(m, s):
    uid = m.from_user.id
    d = s["data"]

    if s["step"] == 1:
        d["fio"] = m.text.strip()
        s["step"] = 2
        return bot.send_message(m.chat.id, "🏦 Банк:")

    if s["step"] == 2:
        d["bank"] = m.text.strip()
        s["step"] = 3
        return bot.send_message(m.chat.id, "💰 Сумма:")

    if s["step"] == 3:
        v = safe_parse_number(m.text)
        if v is None:
            return bot.send_message(m.chat.id, "❌ Неверный формат суммы")
        d["amount"] = v
        s["step"] = 4
        return bot.send_message(m.chat.id, "📈 Ставка (%):")

    if s["step"] == 4:
        v = safe_parse_number(m.text)
        if v is None:
            return bot.send_message(m.chat.id, "❌ Неверный формат ставки")
        d["rate"] = v
        s["step"] = 5
        return bot.send_message(m.chat.id, "💱 Валюта (USD, RUB, EUR...):")

    if s["step"] == 5:
        d["currency"] = m.text.strip().upper()
        s["step"] = 6
        return bot.send_message(m.chat.id, "📅 Дата окончания (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД):")

    if s["step"] == 6:
        end = safe_parse_date(m.text)
        if not end:
            return bot.send_message(m.chat.id, "❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ")

        d["end_date"] = end
        d["start_date"] = datetime.today().date().isoformat()

        with get_db() as conn:
            conn.execute("""
                INSERT INTO deposits
                (user_id, fio, bank, amount, rate, currency, end_date, start_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (uid, d["fio"], d["bank"], d["amount"], d["rate"], d["currency"], d["end_date"], d["start_date"]))
            conn.commit()

        force_cleanup(uid)
        reset_session(uid, "✅ Депозит добавлен!", m.chat.id)

# ================= EDIT =================
def edit_start(m):
    safe_set(m.from_user.id, {"action": "edit", "step": 1, "data": {}})

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, fio FROM deposits WHERE user_id=? AND status='active'",
            (m.from_user.id,)
        ).fetchall()

    if not rows:
        return bot.send_message(m.chat.id, "Нет активных депозитов для редактирования.")

    kb = types.InlineKeyboardMarkup()
    for r in rows:
        kb.add(types.InlineKeyboardButton(
            f"#{r['id']} {r['fio']}",
            callback_data=f"edit_pick|{r['id']}"
        ))

    bot.send_message(m.chat.id, "Выберите депозит:", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("edit_pick|"))
def edit_pick(c):
    try:
        _, dep_id = c.data.split("|")
        uid = c.from_user.id

        if not safe_get(uid):
            return bot.answer_callback_query(c.id, "Сессия истекла, начните заново")

        safe_set(uid, {"action": "edit", "step": 2, "data": {"id": int(dep_id)}})

        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "Выберите поле для редактирования:", reply_markup=edit_kb(dep_id))

    except:
        bot.answer_callback_query(c.id, "Ошибка")


@bot.callback_query_handler(func=lambda c: c.data.startswith("edit|"))
def edit_callback(c):
    try:
        _, dep_id, field = c.data.split("|")
        uid = c.from_user.id

        if not safe_get(uid):
            return bot.answer_callback_query(c.id, "Сессия истекла, начните заново")

        safe_set(uid, {
            "action": "edit",
            "step": 3,
            "data": {"id": int(dep_id), "field": field}
        })

        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "Введите новое значение:")

    except:
        bot.answer_callback_query(c.id, "Ошибка")


def handle_edit(m, s):
    uid = m.from_user.id
    d = s["data"]

    if s.get("action") != "edit" or s.get("step") != 3:
        return reset_session(uid, "⚠️ Некорректный контекст", m.chat.id)

    column = d.get("field")
    if not column:
        return reset_session(uid, "❌ Поле не выбрано", m.chat.id)

    val = m.text

    if d["field"] in ["amount", "rate"]:
        val = safe_parse_number(val)
    elif d["field"] == "end_date":
        val = safe_parse_date(val)

    if val is None:
        return bot.send_message(m.chat.id, "❌ Неверный формат значения")

    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE deposits SET {column}=? WHERE id=? AND user_id=?",
            (val, d["id"], uid)
        )

        if cur.rowcount == 0:
            return reset_session(uid, "❌ Запись не найдена", m.chat.id)

        conn.commit()

    force_cleanup(uid)
    reset_session(uid, "✅ Обновлено!", m.chat.id)

# ================= DELETE =================
def delete_start(m):
    safe_set(m.from_user.id, {"action": "delete", "step": 1, "data": {}})

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, fio, amount, currency FROM deposits WHERE user_id=? AND status='active'",
            (m.from_user.id,)
        ).fetchall()

    if not rows:
        force_cleanup(m.from_user.id)
        return bot.send_message(m.chat.id, "Нет активных депозитов.", reply_markup=menu())

    msg = "Введите ID депозита для удаления:\n\n"
    for r in rows:
        msg += f"#{r['id']} — {r['fio']} ({r['amount']} {r['currency']})\n"

    bot.send_message(m.chat.id, msg)


def handle_delete(m, s):
    uid = m.from_user.id

    if s["step"] == 1:
        dep_id = safe_parse_number(m.text)
        if dep_id is None:
            return bot.send_message(m.chat.id, "❌ Введите числовой ID")

        s["data"]["id"] = int(dep_id)
        s["step"] = 2
        return bot.send_message(m.chat.id, f"Удалить депозит #{int(dep_id)}? Подтвердите: <b>да</b> / <b>нет</b>")

    if s["step"] == 2:
        if m.text.lower() != "да":
            return reset_session(uid, "❌ Отменено", m.chat.id)

        with get_db() as conn:
            cur = conn.execute(
                "UPDATE deposits SET status='deleted' WHERE id=? AND user_id=? AND status='active'",
                (s["data"]["id"], uid)
            )

            if cur.rowcount == 0:
                return reset_session(uid, "❌ Запись не найдена", m.chat.id)

            conn.commit()

        force_cleanup(uid)
        reset_session(uid, "🗑 Депозит удалён", m.chat.id)

# ================= CSV =================
def csv_export(m):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM deposits WHERE user_id=? AND status='active'",
            (m.from_user.id,)
        ).fetchall()

    if not rows:
        return bot.send_message(m.chat.id, "Нет данных для экспорта.")

    data = [dict(r) for r in rows]
    fieldnames = list(data[0].keys())

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for row in data:
        writer.writerow(row)

    file = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    file.name = "deposits.csv"

    bot.send_document(m.chat.id, file, caption="📤 Экспорт депозитов")

# ================= STATS =================
def stats(m):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT amount, rate, currency, end_date FROM deposits WHERE user_id=? AND status='active' ORDER BY end_date ASC",
            (m.from_user.id,)
        ).fetchall()

    if not rows:
        return bot.send_message(m.chat.id, "Нет активных депозитов.")

    today = date.today()

    grouped = {}
    for r in rows:
        cur = r["currency"]
        if cur not in grouped:
            grouped[cur] = []
        grouped[cur].append(r)

    expired = sum(1 for r in rows if r["end_date"] and date.fromisoformat(r["end_date"]) < today)
    expiring_soon = sum(1 for r in rows if r["end_date"] and 0 <= (date.fromisoformat(r["end_date"]) - today).days <= 7)
    nearest = rows[0]["end_date"] if rows else "—"

    msg = f"📊 <b>Статистика депозитов</b>\n\n"

    for currency, deps in grouped.items():
        total = sum(float(r["amount"] or 0) for r in deps)
        avg_rate = sum(float(r["rate"] or 0) for r in deps) / len(deps)
        msg += f"<b>{currency}</b>\n"
        msg += f"📁 Депозитов: {len(deps)}\n"
        msg += f"💰 Сумма: {total:,.2f}\n"
        msg += f"📈 Средняя ставка: {avg_rate:.2f}%\n"

    msg += f"📅 Ближайшая дата: {nearest}\n"
    if expiring_soon:
        msg += f"🟡 Истекают в ближ. 7 дн.: {expiring_soon}\n"
    if expired:
        msg += f"🔴 Просроченных: {expired}\n"

    bot.send_message(m.chat.id, msg)

# ================= УВЕДОМЛЕНИЯ =================
NOTIFY_HOUR = 10

def send_expiry_notifications():
    today = date.today().isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT d.id, d.user_id, d.fio, d.bank, d.amount, d.currency, d.end_date
            FROM deposits d
            WHERE d.status = 'active'
              AND d.end_date < ?
              AND NOT EXISTS (
                  SELECT 1 FROM notified n
                  WHERE n.deposit_id = d.id AND n.notified_date = ?
              )
        """, (today, today)).fetchall()

        for r in rows:
            try:
                expired_days = (date.today() - date.fromisoformat(r["end_date"])).days
                msg = (
                    f"⏰ <b>Депозит истёк!</b>\n\n"
                    f"#{r['id']} {r['fio']}\n"
                    f"🏦 {r['bank']}\n"
                    f"💰 {r['amount']} {r['currency']}\n"
                    f"📅 Дата окончания: {r['end_date']}\n"
                    f"⚠️ Просрочен на {expired_days} дн.\n\n"
                    f"Используйте «🗑 Удалить» чтобы закрыть депозит."
                )
                bot.send_message(r["user_id"], msg)
                conn.execute(
                    "INSERT OR IGNORE INTO notified (deposit_id, notified_date) VALUES (?, ?)",
                    (r["id"], today)
                )
                conn.commit()
                logging.info(f"Уведомление отправлено: user={r['user_id']} deposit={r['id']}")
            except Exception as e:
                logging.warning(f"Не удалось отправить уведомление user={r['user_id']}: {e}")


def notification_scheduler():
    while True:
        now = datetime.now()
        if now.hour == NOTIFY_HOUR and now.minute == 0:
            logging.info("Запуск ежедневных уведомлений...")
            send_expiry_notifications()
            time.sleep(61)
        else:
            time.sleep(30)

threading.Thread(target=notification_scheduler, daemon=True).start()

# ================= RUN =================
print("BOT STARTED")
bot.infinity_polling(skip_pending=True)
