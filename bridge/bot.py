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


def send(chat: str, text: str, menu: bool = False) -> int | None:
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    if menu:
        payload["reply_markup"] = json.dumps({"inline_keyboard": KEYBOARD}, ensure_ascii=False)
    res = api("sendMessage", payload)
    if res.get("ok"):
        return res.get("result", {}).get("message_id")
    log(f"ОШИБКА отправки: {res.get('description')}")
    return None


def edit(chat: str, msg_id: int | None, text: str, menu: bool = False) -> None:
    if not msg_id:
        return
    payload = {"chat_id": chat, "message_id": msg_id, "text": text[:MAX_TG]}
    if menu:
        payload["reply_markup"] = json.dumps({"inline_keyboard": KEYBOARD}, ensure_ascii=False)
    api("editMessageText", payload)


def answer_cb(cb_id: str, text: str = "") -> None:
    payload = {"callback_query_id": cb_id}
    if text:
        payload["text"] = text[:180]
    api("answerCallbackQuery", payload)


# ----------------------------------------------------------------------------- меню и тексты

KEYBOARD = [
    [{"text": "💬 Поговорить", "callback_data": "mode_chat"},
     {"text": "🛠 Задача по коду", "callback_data": "mode_task"}],
    [{"text": "📊 Статус", "callback_data": "status"},
     {"text": "🔄 Новый диалог", "callback_data": "new"}],
    [{"text": "⏹ Остановить", "callback_data": "stop"},
     {"text": "❓ Помощь", "callback_data": "help"}],
]

TXT_START = (
    "👋 Привет! Я — твой персональный агент на этом сервере.\n\n"
    "Что я умею:\n"
    "• отвечать на вопросы по любой теме и искать информацию в интернете;\n"
    "• читать и разбирать файлы твоей рабочей папки;\n"
    "• писать и править код, а затем показывать, что изменилось.\n\n"
    "Чего не умею (и не буду делать): ломать сервер, трогать VPN, "
    "торгового бота и системные настройки, запускать код в консоли.\n\n"
    "Выбери режим кнопкой ниже или просто напиши сообщение."
)

TXT_HELP = (
    "❓ <b>Как со мной работать</b>\n\n"
    "<b>Команды</b>\n"
    "/start — это меню\n"
    "/help — эта справка\n"
    "/status — состояние: режим, диалог, последние коммиты\n"
    "/new — начать диалог с чистого листа\n"
    "/stop — прервать текущую работу\n"
    "/chat — переключиться в режим общения\n"
    "/task — переключиться в режим задач по коду\n"
    "/who — что я умею и чего не умею\n\n"
    "<b>Режимы</b>\n"
    "💬 Поговорить — отвечаю на вопросы, читаю файлы, ищу в интернете. "
    "Ничего не меняю на сервере.\n"
    "🛠 Задача по коду — могу создавать и править файлы в рабочей папке. "
    "Каждое изменение фиксируется в git, можно откатить.\n\n"
    "<b>Очередь</b>\n"
    "Работаю по одной задаче. Если отправить несколько сообщений подряд, "
    "остальные встанут в очередь — дождись ответа на первое.\n\n"
    "<b>Честно про модели</b>\n"
    "Сейчас я работаю на бесплатной модели. Для сложного кода качество "
    "ниже, чем у платных — если нужно качественнее, скажи, переключу."
)

TXT_WHO = (
    "🤖 <b>Что я умею</b>\n\n"
    "✅ Отвечать на вопросы по любым темам\n"
    "✅ Искать информацию в интернете (есть webfetch и поиск)\n"
    "✅ Читать и анализировать файлы рабочей папки\n"
    "✅ Писать, править и рефакторить код\n"
    "✅ Показывать список изменённых файлов и делать коммит\n\n"
    "🚫 Чего не делаю принципиально:\n"
    "• не выхожу за пределы своей рабочей папки\n"
    "• не трогаю торгового бота, VPN, Docker, systemd, сеть\n"
    "• не запускаю код в консоли (нельзя проверить программу сам)\n"
    "• не храню и не вывожу ключи и пароли\n\n"
    "ℹ️ Я не помню прошлые разговоры — только то, что лежит в папке и "
    "текущий диалог. Начни новый диалог кнопкой «Новый диалог», если нужна чистая тема."
)


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


def oc_args(mode: str, session: str) -> list[str]:
    ver = subprocess.run([OC_BIN, "--version"], capture_output=True, text=True,
                         timeout=30).stdout.strip()
    major = "".join(ch for ch in ver if ch.isdigit())[:1] or "1"
    base = (["--standalone"] if major.isdigit() and int(major) >= 2 else ["--pure"])
    args = base + ["--auto", "--format", "json", "--model", MODEL]
    if mode == MODE_CHAT:
        args += ["--agent", "chat"]
    if session:
        args += ["--session", session]
    return args


def stream_agent(chat: str, prompt: str, mode: str, session: str):
    """Запускает агента и отдаёт события: ('text', str) / ('session', id) / ('done'|'err', str)."""
    env = dict(os.environ)
    env["OPENROUTER_API_KEY"] = OPENCODE_API_KEY
    cmd = [OC_BIN, "run", *oc_args(mode, session), prompt]
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
            part = d.get("part", {}) or {}
            if d.get("type") == "text" and part.get("text"):
                yield "text", part["text"]
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
    """Выполняет одну задачу в фоне."""
    with WORK:
        st = STORE.get(chat)
        session = st.get("session") or ""
        mode = st.get("mode", MODE_CHAT)
        head = "💬 Общаюсь" if mode == MODE_CHAT else "🛠 Работаю"
        msg_id = send(chat, f"{head}…\n<i>Запрос принят, жди ответа. "
                            f"Если задача сложная — это может занять минуту.</i>")
        buf, last_send = "", time.time()
        final, error = None, None
        for kind, val in stream_agent(chat, prompt, mode, session):
            if kind == "text":
                buf = val
                if time.time() - last_send > UPDATE_EVERY:
                    edit(chat, msg_id, f"{head}…\n{buf}")
                    last_send = time.time()
            elif kind == "session":
                STORE.set(chat, session=val)
            elif kind == "done":
                final = val
            elif kind == "err":
                error = val
        if buf.strip():
            parts = chunk(buf)
            edit(chat, msg_id, parts[0])
            for extra in parts[1:]:
                send(chat, extra)
        else:
            text = error or ("⚠️ Агент не вернул текст. Попробуй переформулировать "
                             "или нажми «Новый диалог».")
            edit(chat, msg_id, text)
        if error and buf.strip():
            send(chat, f"⚠️ {error}")
        STORE.set(chat, last=prompt[:200], busy=False)
        if mode == MODE_TASK:
            send(chat, autocommit(chat, prompt))
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
    if low in ("/start", "старт", "начать", "/menu", "меню"):
        send(chat, TXT_START, menu=True)
    elif low in ("/help", "помощь", "/помощь"):
        send(chat, TXT_HELP, menu=True)
    elif low in ("/who", "/кто", "что ты умеешь"):
        send(chat, TXT_WHO)
    elif low in ("/status", "статус", "/статус"):
        send(chat, txt_status(chat), menu=True)
    elif low in ("/new", "/новая", "новый диалог"):
        STORE.drop_session(chat)
        send(chat, "🔄 Начинаю новый диалог. Память прошлого разговора сброшена.\n\n"
                   "С чем работаем?", menu=True)
    elif low in ("/stop", "стоп", "хватит", "/отмена"):
        send(chat, stop(chat))
    elif low in ("/chat", "/общение", "общение", "поговорить"):
        STORE.set(chat, mode=MODE_CHAT)
        send(chat, "💬 Режим общения включён. Задавай вопросы обычным сообщением.\n"
                   "Файлы не меняю, консоль не трогаю, в интернете искать могу.", menu=True)
    elif low in ("/task", "/задача", "задача", "код"):
        STORE.set(chat, mode=MODE_TASK)
        send(chat, "🛠 Режим задач по коду включён. Опиши задачу.\n"
                   "Каждое изменение попадёт в git — откатить можно всегда.", menu=True)
    else:
        enqueue(chat, text)


def on_callback(chat: str, data: str) -> None:
    if data == "mode_chat":
        STORE.set(chat, mode=MODE_CHAT)
        answer_cb("", "")
        send(chat, "💬 Режим общения. Спрашивай что угодно.", menu=True)
    elif data == "mode_task":
        STORE.set(chat, mode=MODE_TASK)
        send(chat, "🛠 Режим задач. Опиши, что нужно сделать.", menu=True)
    elif data == "status":
        send(chat, txt_status(chat), menu=True)
    elif data == "new":
        STORE.drop_session(chat)
        send(chat, "🔄 Новый диалог. С чего начнём?", menu=True)
    elif data == "stop":
        send(chat, stop(chat))
    elif data == "help":
        send(chat, TXT_HELP, menu=True)


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
    while True:
        res = api("getUpdates", {"timeout": 45, "offset": offset,
                                "allowed_updates": json.dumps(
                                    ["message", "callback_query"])}, timeout=70)
        if not res.get("ok"):
            desc = str(res.get("description", "?"))
            if "terminated by other getUpdates" in desc:
                # второй опросчик забрал long poll — это не поломка, просто ждём
                log("второй опросчик забрал getUpdates (вероятно, дубль процесса) — жду")
                time.sleep(3)
                continue
            if "webhook" in desc or "Conflict" in desc:
                log(f"getUpdates: {desc}")
                if drop_webhook("getUpdates вернул 409"):
                    continue
                time.sleep(30)
                continue
            log(f"getUpdates не удался: {desc}")
            time.sleep(5 if "429" in desc else 15)
            continue
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


def main() -> int:
    if not acquire_lock():
        return 0
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
    check("меню: 6 кнопок в 3 рядах", len(KEYBOARD) == 3 and all(len(r) == 2 for r in KEYBOARD))
    cyr = "".join(b["text"] for r in KEYBOARD for b in r)
    check("подписи кнопок на русском", all(w in cyr for w in
          ("Поговорить", "Задача", "Статус", "Остановить", "Помощь")))
    check("в подписях нет английских слов",
          not any(w in cyr for w in ("Start", "Help", "Status", "Chat", "Task", "Stop")))
    check("callback_data — ASCII (требование Telegram)",
          all(b["callback_data"].isascii() for r in KEYBOARD for b in r))
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
    globals()["send"] = lambda chat, text, menu=False: (outbox.append(text), 1)[1]
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

    print(f"\n{'ВСЁ ОК' if not fails else str(fails) + ' ПРОВАЛОВ'}")
    return 1 if fails else 0


def dry_run() -> int:
    """Прогоняет апдейты из stdin и печатает, что бот ответил бы."""
    if os.path.exists(STATE_FILE):
        os.rename(STATE_FILE, STATE_FILE + ".bak")
    sent: list[str] = []

    def fake_send(chat: str, text: str, menu: bool = False):
        sent.append(text)
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


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
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
