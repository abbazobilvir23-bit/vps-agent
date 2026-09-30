#!/usr/bin/env python3
"""Telegram-мост к агенту OpenCode на этом же VPS.

Только стандартная библиотека Python — ничего доустанавливать не нужно.

Что умеет:
  * главное меню кнопками (полностью на русском);
  * два режима: «Поговорить» (агент ничего не пишет) и «Задача по коду»;
  * очередь: один запуск агента за раз, остальные ждут;
  * частичный вывод: сообщение в Telegram обновляется по мере работы агента;
  * состояние диалога хранится на диске, диалог продолжается после перезапуска.

Запуск:  python3 bot.py            (обычная работа)
         python3 bot.py --dry-run   (прогон тестов без сети)
         python3 bot.py --check     (проверка конфигурации и меню)
"""
from __future__ import annotations

import fcntl
import json
import os
import random
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.environ.get("AGENT_ROOT", "/root/agent")
APP = os.path.join(ROOT, "app")
WS = os.path.join(ROOT, "workspace")
ENV_FILE = os.path.join(ROOT, "env")
AGENT_ENV = os.path.join(ROOT, "agent.env")
STATE_FILE = os.path.join(ROOT, "bridge_state.json")
LOG_FILE = os.path.join(ROOT, "bridge.log")
LOCK_FILE = os.path.join(ROOT, "bridge.lock")
TELEGRAM = "https://api.telegram.org"
MAX_TG = 4000
RUN_TIMEOUT = 1800
UPDATE_EVERY = 8          # как часто обновлять сообщение в Telegram, сек
QUEUE_LIMIT = 20

MODE_CHAT = "chat"
MODE_TASK = "task"

# ----------------------------------------------------------------------------- конфиг


def load_env() -> dict:
    env = dict(os.environ)
    for path in (ENV_FILE, AGENT_ENV):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env.setdefault(key.strip(), value.strip().strip("'\""))
    return env


ENV = load_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "").strip()
OC_BIN = ENV.get("OC") or os.path.expanduser("~/.opencode/bin/opencode")
MODEL = ENV.get("MODEL") or "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
# Бесплатные модели перегружены (503 provider_overloaded). Основная + запасные.
_DEFAULT_MODELS = [
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/inclusionai/ling-3.0-flash-sante:free",
    "openrouter/nvidia/nemotron-3.5-lightning:free",
    "openrouter/stealth/space-bunny-alpha",
]


def agent_models() -> list[str]:
    raw = ENV.get("MODELS", "").strip()
    models = [m.strip() for m in raw.split(",") if m.strip()] if raw else []
    if MODEL and MODEL not in models:
        models.insert(0, MODEL)
    for m in _DEFAULT_MODELS:
        if m not in models:
            models.append(m)
    return models
OPENCODE_API_KEY = ENV.get("OPENROUTER_API_KEY", "").strip()


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------- состояние


class Store:
    """chat_id -> {session, mode, busy, queue}"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()  # reentrant: get() зовётся внутри set()
        self.data: dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (OSError, ValueError):
                self.data = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log(f"не удалось сохранить состояние в {self.path}: {e}")

    def get(self, chat: str) -> dict:
        with self.lock:
            item = self.data.setdefault(chat, {})
            item.setdefault("mode", MODE_CHAT)
            item.setdefault("session", "")
            item.setdefault("busy", False)
            item.setdefault("queue", [])
            item.setdefault("last", "")
            return item

    def set(self, chat: str, **kw) -> dict:
        with self.lock:
            item = self.get(chat)
            item.update(kw)
            self._save()
            return item

    def drop_session(self, chat: str) -> None:
        with self.lock:
            item = self.get(chat)
            item["session"] = ""
            item["queue"] = []
            self._save()


STORE = Store(STATE_FILE)
PROCS: dict[str, subprocess.Popen] = {}
PROCS_LOCK = threading.Lock()
WORK = threading.Semaphore(1)


# ----------------------------------------------------------------------------- телеграм


def api(method: str, payload: dict | None = None, timeout: int = 60) -> dict:
    if not TOKEN:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN пуст"}
    url = f"{TELEGRAM}/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(payload or {}).encode()
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(body)
        except ValueError:
            return {"ok": False, "description": f"HTTP {e.code}: {body[:120]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"{type(e).__name__}: {e}"}


WEBHOOK_DROPPED = False


def drop_webhook(reason: str = "") -> bool:
    """Снимает webhook. Пока он включён, getUpdates отдаёт 409 Conflict."""
    global WEBHOOK_DROPPED
    res = api("deleteWebhook", {"drop_pending_updates": "false"})
    if res.get("ok"):
        WEBHOOK_DROPPED = True
        log(f"webhook снят{' (' + reason + ')' if reason else ''} — можно читать сообщения")
        time.sleep(3)   # удаление распространяется по фронтендам Telegram не мгновенно
        return True
    log(f"не удалось снять webhook: {res.get('description')}")
    return False


def chunk(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return ["(пустой ответ)"]
    if len(text) <= MAX_TG:
        return [text]
    parts, buf = [], ""
    for line in text.splitlines():
        while len(line) > MAX_TG:
            parts.append(buf + line[:MAX_TG])
            buf, line = "", line[MAX_TG:]
        if len(buf) + len(line) + 1 > MAX_TG:
            parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}".strip("\n")
    if buf:
        parts.append(buf)
    return parts


def _markup(inline=None, reply=None) -> str | None:
    """inline — кнопки под сообщением; reply — постоянная нижняя клавиатура."""
    if inline is not None:
        return json.dumps({"inline_keyboard": inline}, ensure_ascii=False)
    if reply is not None:
        return json.dumps({"keyboard": reply, "resize_keyboard": True,
                           "is_persistent": True}, ensure_ascii=False)
    return None


def send(chat: str, text: str, inline=None, reply=None) -> int | None:
    payload = {"chat_id": chat, "text": text[:MAX_TG], "disable_web_page_preview": "true"}
    mk = _markup(inline, reply)
    if mk:
        payload["reply_markup"] = mk
    res = api("sendMessage", payload)
    if res.get("ok"):
        return res.get("result", {}).get("message_id")
    log(f"ОШИБКА отправки: {res.get('description')}")
    return None


def edit(chat: str, msg_id: int | None, text: str, inline=None) -> None:
    if not msg_id:
        return
    payload = {"chat_id": chat, "message_id": msg_id, "text": text[:MAX_TG],
               "disable_web_page_preview": "true"}
    mk = _markup(inline, None)
    if mk:
        payload["reply_markup"] = mk
    api("editMessageText", payload)


def typing(chat: str) -> None:
    api("sendChatAction", {"chat_id": chat, "action": "typing"})


def typing_loop(chat: str, stop: threading.Event) -> None:
    while not stop.is_set():
        typing(chat)
        stop.wait(5)


def answer_cb(cb_id: str, text: str = "") -> None:
    payload = {"callback_query_id": cb_id}
    if text:
        payload["text"] = text[:180]
    api("answerCallbackQuery", payload)


# ----------------------------------------------------------------------------- меню и тексты

BTN_CHAT = "💬 Общение"
BTN_TASK = "🛠 Код"
BTN_STATUS = "📊 Статус"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "❓ Помощь"

# Постоянная клавиатура внизу — основная навигация.
REPLY_KEYBOARD = [
    [{"text": BTN_CHAT}, {"text": BTN_TASK}],
    [{"text": BTN_STATUS}, {"text": BTN_SETTINGS}],
    [{"text": BTN_HELP}],
]


def inline_home(chat: str) -> list[list[dict]]:
    st = STORE.get(chat)
    mode = st.get("mode", MODE_CHAT)
    def lbl(prefix: str, m: str) -> str:
        return ("✅ " if mode == m else "") + prefix
    return [
        [{"text": lbl("💬 Общение", MODE_CHAT), "callback_data": "mode_chat"},
         {"text": lbl("🛠 Код", MODE_TASK), "callback_data": "mode_task"}],
        [{"text": "📊 Статус", "callback_data": "status"},
         {"text": "🔄 Новый диалог", "callback_data": "new"}],
        [{"text": "🩺 Диагностика", "callback_data": "diag"},
         {"text": "❓ Помощь", "callback_data": "help"}],
    ]


CMD_LIST = [
    ("start", "🏠 Главное меню"),
    ("chat", "💬 Режим общения"),
    ("task", "🛠 Режим кода"),
    ("status", "📊 Статус и текущий диалог"),
    ("new", "🔄 Начать заново"),
    ("stop", "⏹ Остановить работу"),
    ("diag", "🩺 Диагностика"),
    ("help", "❓ Помощь"),
]

BOT_DESCRIPTION = (
    "Личный ИИ-агент на твоём сервере. Отвечает на любые вопросы, ищет информацию "
    "в интернете, читает файлы и пишет код в изолированной песочнице. Каждое "
    "изменение кода фиксируется в git и откатывается одной командой."
)
BOT_SHORT_DESCRIPTION = "Личный ИИ-агент: вопросы, интернет, код в песочнице."


def register_bot() -> None:
    """Команды в меню «/», описание бота и кнопка меню — как у топовых ботов."""
    res = api("setMyCommands", {"commands": json.dumps(CMD_LIST, ensure_ascii=False)})
    log("меню команд: " + ("обновлено" if res.get("ok") else f"не вышло ({res.get('description')})"))
    api("setMyDescription", {"description": BOT_DESCRIPTION})
    api("setMyShortDescription", {"short_description": BOT_SHORT_DESCRIPTION})
    api("setChatMenuButton", {"menu_button": json.dumps({"type": "commands"})})


TXT_START = (
    "🤖 <b>Твой личный ИИ-агент</b>\n"
    "Работает прямо на сервере, никуда твои данные не уходят.\n\n"
    "<b>Что умею</b>\n"
    "🌐 Находить информацию в интернете\n"
    "📂 Читать файлы рабочей папки\n"
    "🛠 Писать и править код\n"
    "🧠 Рассуждать на любые темы\n\n"
    "<b>Два режима</b>\n"
    "💬 <b>Общение</b> — отвечаю, читаю, ищу. Ничего не меняю.\n"
    "🛠 <b>Код</b> — создаю и правлю файлы, каждое изменение в git.\n\n"
    "Выбери режим кнопкой внизу или просто напиши сообщение."
)

TXT_HELP = (
    "❓ <b>Помощь</b>\n\n"
    "<b>Режимы</b>\n"
    "💬 <b>Общение</b> — просто разговор. Могу читать файлы и искать в интернете,\n"
    "   но ничего не меняю на сервере.\n"
    "🛠 <b>Код</b> — создаю и правлю файлы в рабочей папке. После каждой задачи\n"
    "   делаю коммит, поэтому любой результат откатывается.\n\n"
    "<b>Кнопки внизу</b>\n"
    "💬 Общение · 🛠 Код — переключить режим\n"
    "📊 Статус — что сейчас происходит\n"
    "⚙️ Настройки — модель, диалог\n"
    "❓ Помощь — эта справка\n\n"
    "<b>Команды</b>\n"
    "/start — меню\n"
    "/chat — режим общения | /task — режим кода\n"
    "/status — состояние | /new — новый диалог\n"
    "/stop — остановить | /diag — диагностика\n\n"
    "<b>Полезно знать</b>\n"
    "• Работаю по одной задаче. Если прислать несколько сообщений подряд,\n"
    "  остальные встанут в очередь.\n"
    "• Сложная задача может занять минуту-две — я показываю «печатает».\n"
    "• Я не помню прошлые разговоры вне текущего диалога.\n"
    "• Работаю на бесплатной модели: иногда провайдер перегружен и я\n"
    "  переключаюсь на резервную — ответ будет, но чуть дольше."
)

TXT_WHO = (
    "🤖 <b>Что я умею и чего не делаю</b>\n\n"
    "✅ Отвечаю на вопросы по любым темам\n"
    "✅ Ищу информацию в интернете\n"
    "✅ Читаю и разбираю файлы рабочей папки\n"
    "✅ Пишу, правлю и рефакторю код\n"
    "✅ Делаю коммит и показываю, что изменилось\n\n"
    "🚫 Не выхожу за пределы своей папки\n"
    "🚫 Не трогаю торгового бота, VPN, Docker, systemd, сеть\n"
    "🚫 Не запускаю код в консоли\n"
    "🚫 Не храню и не вывожу ключи и пароли\n\n"
    "ℹ️ Я не помню прошлые разговоры вне текущего диалога.\n"
    "Кнопка «🔄 Новый диалог» сбрасывает контекст."
)


def txt_settings(chat: str) -> str:
    st = STORE.get(chat)
    mode = "💬 Общение" if st.get("mode", MODE_CHAT) == MODE_CHAT else "🛠 Код"
    models = agent_models()
    extra = f" (+{len(models) - 1} резервных)" if len(models) > 1 else ""
    sess = st.get("session") or "новый"
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"Режим: <b>{mode}</b>\n"
        f"Модель: <code>{models[0].split('/')[-1]}</code>{extra}\n"
        f"Диалог: <code>{sess}</code>\n"
        f"Очередь: {len(st.get('queue') or [])}\n\n"
        "<i>Модель меняется в файле на сервере — скажи, если нужно.</i>"
    )


def txt_diag(chat: str) -> str:
    """Факты о состоянии, а не догадки."""
    lines = ["🩺 <b>Диагностика</b>", ""]
    # opencode
    try:
        ver = subprocess.run([OC_BIN, "--version"], capture_output=True, text=True,
                             timeout=30).stdout.strip() or "нет ответа"
    except Exception as e:  # noqa: BLE001
        ver = f"ОШИБКА {type(e).__name__}"
    ok_bin = os.path.isfile(OC_BIN) and os.access(OC_BIN, os.X_OK)
    lines.append(f"opencode: <code>{OC_BIN}</code>")
    lines.append(f"  версия: {ver} | исполняемый: {'да' if ok_bin else 'НЕТ'}")
    # ключи
    lines.append(f"ключ OpenRouter: {'есть' if OPENCODE_API_KEY else 'НЕТ'}")
    lines.append(f"токен Telegram: {'есть' if TOKEN else 'НЕТ'}")
    # песочница
    cfg = os.path.join(WS, "opencode.json")
    try:
        json.load(open(cfg, encoding="utf-8"))
        cfg_ok, cfg_txt = "в порядке", ""
    except Exception as e:  # noqa: BLE001
        cfg_ok, cfg_txt = "НЕ КОРРЕКТЕН", f" ({type(e).__name__})"
    lines.append(f"opencode.json: {cfg_ok}{cfg_txt}")
    lines.append(f"рабочая папка: <code>{WS}</code>")
    # состояние чата
    st = STORE.get(chat)
    lines.append(f"чат: busy={st.get('busy')} | в очереди {len(st.get('queue') or [])}")
    lines.append(f"последний запрос: {st.get('last') or '—'}")
    # что реально доехало до Telegram
    wh = api("getWebhookInfo")
    if wh.get("ok"):
        r = wh["result"]
        lines.append(f"webhook: {r.get('url') or 'нет'} | "
                     f"в очереди Telegram: {r.get('pending_update_count')}")
    # лог: последняя строка
    try:
        tail = subprocess.run(["tail", "-n", "1", LOG_FILE], capture_output=True,
                              text=True, timeout=10).stdout.strip()
        lines.append(f"лог: <code>{tail[-120:]}</code>")
    except Exception:  # noqa: BLE001
        pass
    lines.append("")
    lines.append("Если «opencode: НЕТ» или версия пустая — сломан агент, "
                 "а не Telegram.")
    return "\n".join(lines)


def txt_status(chat: str) -> str:
    st = STORE.get(chat)
    mode = "💬 общение" if st["mode"] == MODE_CHAT else "🛠 задачи по коду"
    sess = st["session"] or "—"
    last = st.get("last") or "—"
    q = len(st.get("queue") or [])
    try:
        commit = subprocess.run(["git", "log", "-1", "--oneline"], cwd=WS,
                                capture_output=True, text=True, timeout=15).stdout.strip() or "—"
    except Exception:  # noqa: BLE001
        commit = "—"
    return (
        "📊 <b>Статус</b>\n\n"
        f"Режим: {mode}\n"
        f"Диалог: <code>{sess}</code>\n"
        f"В очереди: {q}\n"
        f"Последний коммит: <code>{commit}</code>\n"
        f"Прошлый запрос: {last}\n\n"
        f"Модель: <code>{MODEL.split('/')[-1]}</code>\n"
        f"Папка: <code>{WS}</code>"
    )


# ----------------------------------------------------------------------------- запуск агента


def oc_major() -> int:
    try:
        ver = subprocess.run([OC_BIN, "--version"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
        digits = "".join(ch for ch in ver if ch.isdigit())
        return int(digits[0]) if digits else 1
    except Exception as e:  # noqa: BLE001
        log(f"не смог определить версию opencode ({type(e).__name__}) — считаю v1")
        return 1


def oc_args(mode: str, session: str, model: str = "") -> list[str]:
    if oc_major() >= 2:
        # v2: своя сессия сервера
        base = ["--standalone"]
    else:
        # v1: --port заставляет поднять СВОЙ сервер. Без него opencode цепляется
        # к общему фоновому серверу и пишет файлы не в песочницу, а куда придётся.
        base = ["--pure", "--port", str(random.randint(40000, 60000))]
    args = base + ["--auto", "--format", "json", "--model", model or MODEL]
    if mode == MODE_CHAT:
        args += ["--agent", "chat"]
    if session:
        args += ["--session", session]
    return args


def _run_one(chat: str, prompt: str, mode: str, session: str, model: str):
    """Один запуск opencode. Отдаёт события text / edit / session / done / err."""
    model = model or MODEL
    env = dict(os.environ)
    env["OPENROUTER_API_KEY"] = OPENCODE_API_KEY
    # opencode определяет рабочий каталог по переменной PWD, а не по cwd процесса.
    # Без этого он пишет файлы в каталог запуска моста, а не в песочницу.
    env["PWD"] = WS
    env["OLDPWD"] = WS
    cmd = [OC_BIN, "run", *oc_args(mode, session, model), prompt]
    log(f"запуск агента [{mode}]: {prompt[:80]}")
    try:
        proc = subprocess.Popen(cmd, cwd=WS, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1,
                                start_new_session=True)
    except Exception as e:  # noqa: BLE001
        yield "err", f"Не удалось запустить агента: {type(e).__name__}: {e}"
        return
    with PROCS_LOCK:
        PROCS[chat] = proc
    start = time.time()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "error":
                err = d.get("error") or {}
                msg = (err.get("data") or {}).get("message") or err.get("name") or str(err)
                yield "err", f"opencode: {msg}"
                continue
            part = d.get("part", {}) or {}
            if d.get("type") == "text" and part.get("text"):
                yield "text", part["text"]
            if part.get("type") == "tool" and part.get("tool") in ("edit", "write", "patch"):
                st = part.get("state", {}) or {}
                if st.get("status") == "completed":
                    yield "edit", str(st.get("input", {}).get("filePath", ""))
            if part.get("sessionID") and not session:
                yield "session", part["sessionID"]
            if time.time() - start > RUN_TIMEOUT:
                proc.kill()
                yield "err", "Время вышло: агент работал дольше 30 минут. Остановил."
                return
        proc.wait(timeout=60)
        err = (proc.stderr.read() if proc.stderr else "") or ""
        if proc.returncode not in (0, None) and err.strip():
            yield "err", err.strip()[:800]
        else:
            yield "done", str(proc.returncode or 0)
    except subprocess.TimeoutExpired:
        proc.kill()
        yield "err", "Агент не ответил вовремя, остановил."
    except Exception as e:  # noqa: BLE001
        yield "err", f"Сбой при работе агента: {type(e).__name__}: {e}"
    finally:
        with PROCS_LOCK:
            PROCS.pop(chat, None)


OVERLOAD_WORDS = ("overload", "503", "429", "temporarily", "rate limit",
                  "unavailable", "server error", "capacity")


def stream_agent(chat: str, prompt: str, mode: str, session: str, model: str = ""):
    """Запускает агента, перебирая модели при перегрузке провайдера.

    Отдаёт: ('model', имя) при попытке, ('retry', текст) при переключении,
    затем события от рабочей модели: text / edit / session / done / err.
    """
    models = agent_models()
    if model and model in models:
        models.remove(model)
        models.insert(0, model)
    last_err = ""
    for idx, m in enumerate(models):
        yield "model", m
        wrote_text = False
        for kind, val in _run_one(chat, prompt, mode, session, m):
            if kind == "text":
                wrote_text = True
                yield kind, val
            elif kind in ("session", "edit", "done"):
                yield kind, val
            elif kind == "err":
                last_err = val
        if wrote_text:
            return
        if idx < len(models) - 1:
            over = any(w in last_err.lower() for w in OVERLOAD_WORDS)
            yield "retry", (f"{m} не ответила ({last_err or 'пусто'}) — "
                            f"пробую {models[idx + 1]}")
            time.sleep(6 if over else 2)
    if last_err:
        yield "err", f"все модели не ответили, последняя ошибка: {last_err}"


def autocommit(chat: str, prompt: str) -> str:
    try:
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=WS,
                               capture_output=True, text=True, timeout=20).stdout.strip()
        if not dirty:
            return "Файлы не менялись."
        subprocess.run(["git", "add", "-A"], cwd=WS, timeout=30,
                       capture_output=True, text=True)
        msg = f"agent: {prompt[:60]}"
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=WS, timeout=30,
                       capture_output=True, text=True)
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=WS,
                             capture_output=True, text=True, timeout=15).stdout.strip()
        stat = subprocess.run(["git", "diff", "--stat", "HEAD~1", "HEAD"], cwd=WS,
                              capture_output=True, text=True, timeout=20).stdout.strip()
        return (f"✅ Закоммичено: <code>{sha}</code>\n<pre>{stat[:900]}</pre>"
                if sha else "Не удалось закоммитить изменения.")
    except Exception as e:  # noqa: BLE001
        return f"Не смог сделать коммит: {type(e).__name__}"


# ----------------------------------------------------------------------------- обработка


def handle_prompt(chat: str, prompt: str) -> None:
    """Выполняет одну задачу. busy сбрасывается при ЛЮБОМ исходе — иначе чат
    навсегда залипнет, а сообщения уйдут в очередь, которую никто не разгребает."""
    mode = MODE_CHAT
    msg_id = None
    try:
        with WORK:
            st = STORE.get(chat)
            mode = st.get("mode", MODE_CHAT)
            session = st.get("session") or ""
            head = "\U0001f4ac Общаюсь" if mode == MODE_CHAT else "\U0001f6e0 Работаю"
            msg_id = send(chat, f"{head}…\n<i>Запрос принят. Сложная задача может "
                                f"занять минуту-две.</i>")
            typer = threading.Event()
            threading.Thread(target=typing_loop, args=(chat, typer), daemon=True).start()
            buf, error, last_send, used = "", None, time.time(), ""
            for kind, val in stream_agent(chat, prompt, mode, session):
                if kind == "text":
                    buf = val
                    if time.time() - last_send > UPDATE_EVERY:
                        edit(chat, msg_id, f"{head}…\n{buf}")
                        last_send = time.time()
                elif kind == "session":
                    STORE.set(chat, session=val)
                elif kind == "model":
                    used = val
                elif kind == "retry":
                    log(val)
                    edit(chat, msg_id, "⏳ Провайдер перегружен, пробую резервную модель…")
                elif kind == "err":
                    error = val
            if buf.strip() and used:
                log(f"ответила модель {used}")
            if mode == MODE_TASK:
                send(chat, autocommit(chat, prompt))
            STORE.set(chat, last=prompt[:200])
    except Exception as e:  # noqa: BLE001
        log(f"СБОЙ обработки запроса: {type(e).__name__}: {e}")
        try:
            send(chat, f"⚠️ Внутренняя ошибка моста: {type(e).__name__}.\n"
                       f"Попробуй ещё раз. Если повторяется — команда /diag")
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            typer.set()
        except Exception:  # noqa: BLE001
            pass
        STORE.set(chat, busy=False)
        _start_next(chat)


def _start_next(chat: str) -> None:
    nxt = None
    with STORE.lock:
        st = STORE.get(chat)
        q: list = st.setdefault("queue", [])
        if q:
            nxt = q.pop(0)
            st["busy"] = True
        else:
            st["busy"] = False
        STORE._save()
    if nxt:
        log(f"беру из очереди: {nxt[:60]}")
        threading.Thread(target=handle_prompt, args=(chat, nxt), daemon=True).start()


def enqueue(chat: str, prompt: str) -> None:
    st = STORE.get(chat)
    if not st.get("busy"):
        STORE.set(chat, busy=True)
        threading.Thread(target=handle_prompt, args=(chat, prompt), daemon=True).start()
        return
    with STORE.lock:
        q: list = st.setdefault("queue", [])
        if len(q) >= QUEUE_LIMIT:
            send(chat, f"⚠️ Очередь переполнена (максимум {QUEUE_LIMIT}). Дождись выполнения.")
            return
        q.append(prompt)
        STORE._save()
        n = len(q)
    send(chat, f"📥 В очереди {n} сообщени(й). Выполню по порядку.")


def stop(chat: str) -> str:
    with STORE.lock:
        st = STORE.get(chat)
        st["queue"] = []
        st["busy"] = False
        STORE._save()
    with PROCS_LOCK:
        proc = PROCS.get(chat)
    if not proc:
        return "⏹ Сейчас ничего не выполняется."
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        return "⏹ Остановил текущую работу. Очередь очищена."
    except Exception:  # noqa: BLE001
        return "⚠️ Не удалось остановить процесс."


def on_text(chat: str, text: str) -> None:
    low = text.strip().lower()
    # кнопки нижней клавиатуры
    if text.strip() in (BTN_CHAT,) or low in ("/start", "/menu", "меню", "🏠"):
        if text.strip() == BTN_CHAT:
            on_callback(chat, "mode_chat")
            return
        send(chat, TXT_START, reply=REPLY_KEYBOARD)
        send(chat, "Выбери режим 👇", inline=inline_home(chat))
    elif text.strip() == BTN_TASK:
        on_callback(chat, "mode_task")
    elif text.strip() == BTN_STATUS:
        on_callback(chat, "status")
    elif text.strip() == BTN_SETTINGS:
        on_callback(chat, "settings")
    elif text.strip() == BTN_HELP:
        on_callback(chat, "help")
    elif low in ("/help", "помощь", "/помощь"):
        send(chat, TXT_HELP, reply=REPLY_KEYBOARD)
    elif low in ("/who", "/кто", "что ты умеешь"):
        send(chat, TXT_WHO)
    elif low in ("/diag", "/диаг", "диагностика"):
        send(chat, txt_diag(chat))
    elif low in ("/status", "/статус"):
        on_callback(chat, "status")
    elif low in ("/settings", "/настройки", "настройки"):
        on_callback(chat, "settings")
    elif low in ("/new", "/новая", "новый диалог"):
        on_callback(chat, "new")
    elif low in ("/stop", "/отмена", "стоп", "хватит"):
        send(chat, stop(chat))
    elif low in ("/chat", "/общение"):
        on_callback(chat, "mode_chat")
    elif low in ("/task", "/задача", "/код"):
        on_callback(chat, "mode_task")
    else:
        enqueue(chat, text)


def on_callback(chat: str, data: str) -> None:
    if data == "mode_chat":
        STORE.set(chat, mode=MODE_CHAT)
        send(chat, "💬 <b>Режим общения</b>\n"
                   "Отвечаю на вопросы, читаю файлы, ищу в интернете. "
                   "Ничего на сервере не меняю.\n\nЗадавай вопрос 👇",
             inline=inline_home(chat))
    elif data == "mode_task":
        STORE.set(chat, mode=MODE_TASK)
        send(chat, "🛠 <b>Режим кода</b>\n"
                   "Создаю и правлю файлы в рабочей папке. Каждая задача — "
                   "отдельный коммит, откат всегда возможен.\n\nОпиши задачу 👇",
             inline=inline_home(chat))
    elif data == "status":
        send(chat, txt_status(chat), inline=inline_home(chat))
    elif data == "settings":
        send(chat, txt_settings(chat), inline=[
            [{"text": "🔄 Новый диалог", "callback_data": "new"},
             {"text": "🩺 Диагностика", "callback_data": "diag"}],
            [{"text": "❓ Помощь", "callback_data": "help"}],
        ])
    elif data == "new":
        STORE.drop_session(chat)
        send(chat, "🔄 <b>Новый диалог</b>\nПамять прошлого разговора сброшена.\n\n"
                   "С чего начнём?", inline=inline_home(chat))
    elif data == "stop":
        send(chat, stop(chat))
    elif data == "diag":
        send(chat, txt_diag(chat))
    elif data == "help":
        send(chat, TXT_HELP, inline=inline_home(chat))


# ----------------------------------------------------------------------------- главный цикл


def check_config() -> list[str]:
    problems = []
    if not TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN пуст в " + ENV_FILE)
    if not OPENCODE_API_KEY:
        problems.append("OPENROUTER_API_KEY пуст в " + ENV_FILE)
    if not os.path.isdir(WS):
        problems.append(f"нет рабочей папки {WS}")
    if not (os.path.isfile(OC_BIN) and os.access(OC_BIN, os.X_OK)):
        problems.append(f"не найден исполняемый opencode: {OC_BIN}")
    if not os.path.exists(os.path.join(WS, "opencode.json")):
        problems.append("в песочнице нет opencode.json — правила доступа не применятся")
    return problems


def poll_loop(offset: int) -> None:
    webhook_conflicts = 0
    while True:
        res = api("getUpdates", {"timeout": 45, "offset": offset,
                                "allowed_updates": json.dumps(
                                    ["message", "callback_query"])}, timeout=70)
        if not res.get("ok"):
            desc = str(res.get("description", "?"))
            if "terminated by getWebhook request" in desc:
                # кто-то (в т.ч. наш же deleteWebhook) дёрнул getWebhook во время опроса
                log("прервано запросом getWebhook — повторяю через 5с")
                time.sleep(5)
                continue
            if "terminated by other getUpdates request" in desc:
                # дубль процесса забрал long poll; от этого защищает flock
                log("второй опросчик забрал getUpdates — повторяю через 5с")
                time.sleep(5)
                continue
            if "webhook is active" in desc:
                webhook_conflicts += 1
                if webhook_conflicts <= 3:
                    log(f"webhook активен (попытка {webhook_conflicts}) — снимаю")
                    drop_webhook("webhook активен")
                    time.sleep(3)
                    continue
                log("webhook постоянно возвращается — его кто-то ставит заново. "
                    "Найди того, кто это делает; снять руками: "
                    "https://api.telegram.org/bot<ТОКЕН>/deleteWebhook")
                time.sleep(30)
                continue
            log(f"getUpdates не удался: {desc}")
            time.sleep(5 if "429" in desc else 15)
            continue
        webhook_conflicts = 0
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    answer_cb(cq["id"])
                    on_callback(str(cq["message"]["chat"]["id"]), cq.get("data", ""))
                elif "message" in upd:
                    msg = upd["message"]
                    chat = str(msg["chat"]["id"])
                    if msg.get("from", {}).get("is_bot"):
                        continue
                    text = (msg.get("text") or msg.get("caption") or "").strip()
                    if text:
                        on_text(chat, text)
            except Exception as e:  # noqa: BLE001
                log(f"ошибка обработки апдейта: {type(e).__name__}: {e}")


LOCK_FH = None


def acquire_lock() -> bool:
    """Один экземпляр на файл-лок. Второй молча выйдет, а не будет драться за getUpdates."""
    global LOCK_FH
    try:
        LOCK_FH = open(LOCK_FILE, "w")
        fcntl.flock(LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        LOCK_FH.write(str(os.getpid()))
        LOCK_FH.flush()
        return True
    except (OSError, IOError) as e:
        log(f"другой экземпляр моста уже работает ({type(e).__name__}) — выхожу")
        return False


def reset_stale_state() -> None:
    """После перезапуска ни одна задача не выполняется. Если busy остался True,
    сообщения копятся в очереди вечно и никто их не разгребает."""
    dirty = False
    with STORE.lock:
        for chat, st in STORE.data.items():
            if st.get("busy") or st.get("queue"):
                log(f"сбрасываю зависшую очередь чата {chat} "
                    f"(busy={st.get('busy')}, в очереди {len(st.get('queue') or [])})")
                st["busy"] = False
                st["queue"] = []
                dirty = True
        if dirty:
            STORE._save()


def main() -> int:
    if not acquire_lock():
        return 0
    reset_stale_state()
    problems = check_config()
    if problems:
        log("КОНФИГУРАЦИЯ НЕПОЛНАЯ:")
        for p in problems:
            log(f"  - {p}")
        if not TOKEN:
            return 1
        log("Продолжаю, но сообщения работать не будут без токена.")
    if not problems:
        me = api("getMe")
        if me.get("ok"):
            log(f"подключён как @{me['result'].get('username', '?')}")
        else:
            log(f"getMe не удался: {me.get('description')}")
        if me.get("ok"):
            drop_webhook("перед стартом опроса")
            register_bot()

    def shutdown(_sig, _frm):
        log("завершаюсь")
        with PROCS_LOCK:
            for proc in list(PROCS.values()):
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log("мост запущен, жду сообщений")
    poll_loop(0)
    return 0


# ----------------------------------------------------------------------------- тесты


def self_test() -> int:
    """Оффлайн-проверка: меню, русские тексты, разбиение, состояние, маршрутизация."""
    print("=== ТЕСТ МОСТА (без сети) ===")
    fails = 0
    outbox: list[str] = []

    def check(name: str, cond: bool) -> None:
        nonlocal fails
        print(f"  {'PASS' if cond else 'FAIL'} {name}")
        if not cond:
            fails += 1

    # --- меню ---
    cyr = "".join(b["text"] for r in REPLY_KEYBOARD for b in r)
    check("нижняя клавиатура: 5 кнопок", sum(len(r) for r in REPLY_KEYBOARD) == 5)
    check("подписи кнопок на русском", all(w in cyr for w in
          ("Общение", "Код", "Статус", "Настройки", "Помощь")))
    check("в подписях нет английских слов",
          not any(w in cyr for w in ("Start", "Help", "Status", "Chat", "Task", "Stop")))
    for name in ("chat", "task", "status", "new", "diag", "help"):
        rows = inline_home("0")
    check("инлайн-меню: 3 ряда по 2 кнопки",
          len(inline_home("0")) == 3 and all(len(r) == 2 for r in inline_home("0")))
    cbs = [b["callback_data"] for r in inline_home("0") for b in r]
    check("callback_data — ASCII (требование Telegram)",
          all(c.isascii() for c in cbs) and "mode_chat" in cbs and "stop" not in cbs)
    check("команды для меню «/» на русском",
          len(CMD_LIST) >= 6 and all(len(c) == 2 for c in CMD_LIST)
          and all(any(ch in d for ch in "абвгдеёжзийклмнопрстуфхцчшщыэюя") for _, d in CMD_LIST))
    check("описания бота в пределах лимитов",
          len(BOT_DESCRIPTION) <= 512 and len(BOT_SHORT_DESCRIPTION) <= 120)
    check("все тексты в пределах лимита Telegram",
          all(len(t) <= MAX_TG for t in (TXT_START, TXT_HELP, TXT_WHO)))

    # --- разбиение длинных ответов ---
    long_text = "строка\n" * 3000
    parts = chunk(long_text)
    check("длинный текст режется на куски <= 4000", all(len(c) <= MAX_TG for c in parts) and len(parts) > 1)
    check("ни одна строка не потеряна при разбиении",
          "\n".join(parts).count("строка") == long_text.count("строка"))
    check("короткий текст не режется", chunk("просто текст") == ["просто текст"])
    check("пустой ответ не падает", chunk("") == ["(пустой ответ)"])

    # --- состояние ---
    tmp_state = "/tmp/bridge_selftest.json"
    for f in (tmp_state, tmp_state + ".tmp"):
        os.path.exists(f) and os.remove(f)
    st = Store(tmp_state)
    st.set("42", mode=MODE_TASK)
    check("состояние переживает перезапуск",
          Store(tmp_state).get("42")["mode"] == MODE_TASK)
    st.get("42")["queue"].append("вторая задача")
    st.set("42", busy=True)
    check("очередь сохраняется в JSON",
          Store(tmp_state).get("42")["queue"] == ["вторая задача"])
    st.drop_session("42")
    after = Store(tmp_state).get("42")
    check("сброс диалога чистит очередь",
          after["queue"] == [] and after["session"] == "")
    check("сброс диалога НЕ трогает busy (иначе сломается очередь)",
          after["busy"] is True)
    os.path.exists(tmp_state) and os.remove(tmp_state)

    # --- маршрутизация команд и кнопок (send перехватываем) ---
    real_send = globals()["send"]
    real_store = globals()["STORE"]
    globals()["send"] = lambda chat, text, inline=None, reply=None: (outbox.append(text), 1)[1]
    globals()["STORE"] = Store(tmp_state)   # изоляция: тест не трогает /root/agent
    try:
        for cmd, expect in (("/start", "меню"), ("/help", "Как со мной работать"),
                            ("/who", "умею"), ("/status", "Статус"),
                            ("/chat", "общения"), ("/task", "задач"),
                            ("/new", "Новый диалог")):
            outbox.clear()
            on_text("42", cmd)
            got = " ".join(outbox)
            check(f"команда {cmd} -> русский ответ ({expect})",
                  bool(outbox) and any(ch in got for ch in "абвгдеёжзийклмнопрстуфхцчшщыэюя"))
        outbox.clear()
        on_text("42", "/stop")
        check("команда /stop отвечает по-русски", bool(outbox) and "Сейчас" in outbox[-1])

        for data, expect in (("mode_chat", "общения"), ("mode_task", "задач"),
                             ("status", "Статус"), ("new", "Новый диалог"),
                             ("help", "Как со мной работать")):
            outbox.clear()
            on_callback("42", data)
            check(f"кнопка {data} -> русский ответ ({expect})",
                  bool(outbox) and any(ch in outbox[-1] for ch in "абвгдеёжзийклмнопрстуфхцчшщыэюя"))

        outbox.clear()
        on_text("42", "/chat")
        check("/chat переводит в режим общения", STORE.get("42")["mode"] == MODE_CHAT)
        on_text("42", "/task")
        check("/task переводит в режим задач", STORE.get("42")["mode"] == MODE_TASK)

        # очередь: занято -> сообщение встаёт в очередь, а не теряется
        STORE.set("42", busy=True, queue=[])
        on_text("42", "ещё одна задача")
        check("занятый агент ставит сообщение в очередь",
              STORE.get("42")["queue"] == ["ещё одна задача"])
        check("очередь подтверждается по-русски",
              any("очеред" in t.lower() for t in outbox))
        STORE.set("42", busy=False, queue=[])
    finally:
        globals()["send"] = real_send
        globals()["STORE"] = real_store
        os.path.exists(tmp_state) and os.remove(tmp_state)

    # --- зависшая очередь после перезапуска ---
    globals()["STORE"] = Store(tmp_state)
    STORE.set("99", busy=True)
    STORE.get("99")["queue"].append("старая задача")
    globals()["STORE"].set("98", busy=False, queue=[])
    globals()["STORE"]._save()
    reset_stale_state()
    after = Store(tmp_state)
    check("зависший busy сбрасывается при старте",
          not after.get("99")["busy"] and after.get("99")["queue"] == [])
    check("сброс не трогает пустые чаты",
          after.get("98")["queue"] == [] and not after.get("98")["busy"])
    globals()["STORE"] = real_store
    os.path.exists(tmp_state) and os.remove(tmp_state)

    globals()["STORE"] = Store(tmp_state)
    STORE.set("7", mode=MODE_CHAT)
    home_chat = [b["text"] for r in inline_home("7") for b in r]
    check("в инлайн-меню отмечен текущий режим (общение)",
          any(t.startswith("✅") and "Общение" in t for t in home_chat))
    STORE.set("7", mode=MODE_TASK)
    home_task = [b["text"] for r in inline_home("7") for b in r]
    check("отметка переезжает на режим кода",
          any(t.startswith("✅") and "Код" in t for t in home_task))
    globals()["STORE"] = real_store
    os.path.exists(tmp_state) and os.remove(tmp_state)

    print(f"\n{'ВСЁ ОК' if not fails else str(fails) + ' ПРОВАЛОВ'}")
    return 1 if fails else 0


def dry_run() -> int:
    """Прогоняет апдейты из stdin и печатает, что бот ответил бы."""
    if os.path.exists(STATE_FILE):
        os.rename(STATE_FILE, STATE_FILE + ".bak")
    sent: list[str] = []

    def fake_send(chat: str, text: str, inline=None, reply=None):
        tag = " [нижняя клавиатура]" if reply else (" [инлайн]" if inline else "")
        sent.append(text + tag)
        return 1

    globals()["send"] = fake_send
    globals()["api"] = lambda *a, **k: {"ok": True, "result": {"message_id": 1,
                                                               "username": "dryrun"}}
    print("=== ПРОГОН (dry-run) ===")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        upd = json.loads(raw)
        if "message" in upd:
            chat = str(upd["message"]["chat"]["id"])
            text = (upd["message"].get("text") or "").strip()
            sent.append(f"[апдейт] {text}")
            if text:
                on_text(chat, text)
        elif "callback_query" in upd:
            chat = str(upd["callback_query"]["message"]["chat"]["id"])
            sent.append(f"[кнопка] {upd['callback_query'].get('data')}")
            on_callback(chat, upd["callback_query"].get("data", ""))
    print("\n--- что бот ответил бы ---")
    for s in sent:
        print(f"  {s[:110]}")
    return 0


def once(prompt: str, mode: str) -> int:
    """Прогон одной задачи без Telegram. Показывает, где именно отказ:
    конфигурация -> запуск opencode -> ответ модели."""
    print("=== ПРОГОН БЕЗ TELEGRAM ===")
    print(f"opencode: {OC_BIN}")

    def show(name: str, ok: bool, detail: str = "") -> bool:
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
        return ok

    if not show("ключ OpenRouter", bool(OPENCODE_API_KEY)):
        return 1
    if not show("opencode.json в песочнице", os.path.isfile(os.path.join(WS, "opencode.json"))):
        return 1
    try:
        ver = subprocess.run([OC_BIN, "--version"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
        show("opencode отвечает", bool(ver), ver)
    except Exception as e:  # noqa: BLE001
        show("opencode отвечает", False, f"{type(e).__name__}: {e}")
        return 1
    print(f"  режим: {mode}, модель: {MODEL}")
    print("  запускаю задачу…")
    got_text, got_err, edits = "", "", []
    for kind, val in stream_agent("once", prompt, mode, ""):
        if kind == "model":
            print(f"  пробую модель: {val.split('/')[-1]}")
        elif kind == "retry":
            print(f"  ↪ {val}")
        elif kind == "text":
            got_text = val
            print(f"  … получен текст ({len(val)} симв.)")
        elif kind == "edit":
            edits.append(val)
            print(f"  … записан файл: {val}")
        elif kind == "err":
            got_err = val
            print(f"  … ошибка: {val}")
    if edits:
        for f in edits:
            print(f"  файл на диске: {os.path.exists(f)} — {f}")
    print()
    if got_text.strip():
        print("--- ОТВЕТ АГЕНТА ---")
        print(got_text[:1500])
        print("--- ИТОГ: агент работает ---")
        return 0
    print("--- ИТОГ: агент НЕ ответил ---")
    print(got_err or "(ошибки нет, но и текста нет)")
    return 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--once":
        rest = sys.argv[2:]
        m = MODE_CHAT
        if "--mode" in rest:
            i = rest.index("--mode")
            m = rest[i + 1] if i + 1 < len(rest) else MODE_CHAT
            rest = rest[:i] + rest[i + 2:]
        sys.exit(once(" ".join(rest) or "Привет! Ответь одним предложением.", m))
    if arg == "--dry-run":
        sys.exit(dry_run())
    if arg == "--check":
        probs = check_config()
        if probs:
            print("Проблемы конфигурации:")
            for p in probs:
                print("  -", p)
            sys.exit(1)
        print("Конфигурация в порядке. Можно запускать: python3 bot.py")
        sys.exit(0)
    if arg == "--self-test":
        sys.exit(self_test())
    sys.exit(main())
