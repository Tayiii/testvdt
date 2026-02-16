# main.py (версия: приветствие+вопрос; подсказки; опечатки; приоритет; + админ-команды /add /edit /delete /list)

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path
import json
from typing import Optional, List, Dict, Any, Tuple

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
FAQ_PATH = BASE_DIR / "faq.json"
CHAT_PATH = BASE_DIR / "chat.html"
MASCOT_PATH = BASE_DIR / "mascot.png"
ANNOUNCEMENTS_PATH = BASE_DIR / "announcements.txt"

# ----------------------------
# Админ-доступ
# ----------------------------
# Рекомендуется поменять токен на свой и НЕ публиковать его.
# Можно также задать токен через переменную окружения ADMIN_TOKEN.
import os
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")

# Простая сессия по IP (для локального бота этого обычно достаточно).
_ADMIN_IPS: set[str] = set()


class ChatIn(BaseModel):
    message: Optional[str] = None
    payload: Optional[str] = None


def load_faq() -> List[Dict[str, Any]]:
    with FAQ_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
        return data if isinstance(data, list) else []


def save_faq(faq: List[Dict[str, Any]]) -> None:
    # Сохраняем красиво и стабильно
    with FAQ_PATH.open("w", encoding="utf-8") as f:
        json.dump(faq, f, ensure_ascii=False, indent=2)


def normalize_ru(text: str) -> str:
    """
    Нормализация запроса:
    - нижний регистр
    - ё -> е
    - пунктуация/символы -> пробел
    - типовые опечатки/варианты написания -> каноническая форма
    """
    t = (text or "").lower().replace("ё", "е")

    typo_map = {
        "коэфициент": "коэффициент",
        "коефициент": "коэффициент",
        "коэффицент": "коэффициент",
        "кофицент": "коэффициент",
        "коэфицент": "коэффициент",
        "коэфициэнт": "коэффициент",
        "коефициэнт": "коэффициент",
        "коеф": "коэфф",
        "счот": "счет",
        "счёт": "счет",
    }
    for bad, good in typo_map.items():
        t = t.replace(bad, good)

    out = []
    for ch in t:
        if ch.isalnum() or ch.isspace():
            out.append(ch)
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def tokenize(text: str) -> List[str]:
    return [t for t in normalize_ru(text).split() if len(t) > 1]


def strip_greeting_prefix(text: str) -> str:
    t = normalize_ru(text)
    greetings = [
        "здравствуйте",
        "здравствуй",
        "добрый день",
        "доброе утро",
        "добрый вечер",
        "доброго дня",
        "доброго утра",
        "доброго вечера",
        "привет",
        "хай",
        "йо",
        "здарова",
        "здорово",
    ]
    for g in sorted(greetings, key=len, reverse=True):
        if t.startswith(g):
            return t[len(g):].strip()
    return t


def ngrams(words: List[str], n: int) -> List[str]:
    return [" ".join(words[i:i + n]) for i in range(0, max(0, len(words) - n + 1))]


def levenshtein_1(a: str, b: str) -> int:
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return 2
    if la == lb:
        diff = 0
        for ca, cb in zip(a, b):
            if ca != cb:
                diff += 1
                if diff > 1:
                    return 2
        return diff
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = 0
    edits = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            edits += 1
            if edits > 1:
                return 2
            j += 1
    return 1


PRIORITY_BOOST = {
    "water_bill_increased_no_meter": 3.0,
    "payment_not_applied": 2.0,
    "debt_no_readings": 1.5,
    "meters": 1.0,
}


def score_item(query: str, item: Dict[str, Any]) -> float:
    q = normalize_ru(query)
    words = tokenize(q)
    qset = set(words)
    bi = set(ngrams(words, 2))
    tri = set(ngrams(words, 3))

    score = 0.0
    fid = str(item.get("id") or "")
    score += PRIORITY_BOOST.get(fid, 0.0)

    qwords = words

    for raw_kw in item.get("keywords", []) or []:
        kw = normalize_ru(str(raw_kw))
        if not kw or len(kw) < 3:
            continue

        is_phrase = " " in kw
        len_bonus = min(3.0, len(kw) / 6.0)

        if is_phrase:
            wcount = len(kw.split())
            if (wcount == 2 and kw in bi) or (wcount == 3 and kw in tri) or (kw in q):
                score += 6.0 + len_bonus
        else:
            if kw in qset:
                score += 3.0 + len_bonus
            elif kw in q:
                score += 1.5 + len_bonus
            else:
                if len(kw) >= 4:
                    for w in qwords:
                        if len(w) >= 4 and levenshtein_1(w, kw) <= 1:
                            score += 1.0 + len_bonus
                            break

    title = normalize_ru(item.get("title", ""))
    if title and title in q:
        score += 2.0

    return score


def find_best(query: str, faq: List[Dict[str, Any]], min_score=7.0, min_gap=2.2, topk=4):
    scored = [{"item": it, "score": score_item(query, it)} for it in faq]
    scored.sort(key=lambda x: x["score"], reverse=True)
    best = scored[0] if scored else None
    second = scored[1] if len(scored) > 1 else None

    if not best or best["score"] < min_score:
        return {"ok": False, "reason": "low_score", "candidates": scored[:topk]}
    if second and (best["score"] - second["score"]) < min_gap:
        return {"ok": False, "reason": "ambiguous", "candidates": scored[:topk]}
    return {"ok": True, "best": best, "candidates": scored[:topk]}


def faq_by_id(faq: List[Dict[str, Any]], fid: str) -> Optional[Dict[str, Any]]:
    for it in faq:
        if it.get("id") == fid:
            return it
    return None


CATEGORIES = [
    {
        "key": "lk",
        "title": "Личный кабинет",
        "items": [
            {"title": "Вход в личный кабинет", "payload": "faq:lk"},
            {"title": "Передача показаний через личный кабинет", "payload": "faq:meters"},
            {"title": "Получение квитанции в электронном виде", "payload": "faq:electronic_receipt"},
        ],
    },
    {
        "key": "readings",
        "title": "Показания счетчиков",
        "items": [
            {"title": "Как передать показания", "payload": "faq:meters"},
            {"title": "Не принимаются показания (истёк срок поверки)", "payload": "faq:cannot_submit_readings"},
            {"title": "Откуда образовалась задолженность", "payload": "faq:debt_no_readings"},
        ],
    },
    {
        "key": "payments",
        "title": "Оплата и квитанции",
        "items": [
            {"title": "Способы оплаты услуг", "payload": "faq:payment_methods"},
            {"title": "Реквизиты для оплаты", "payload": "faq:payment_details"},
            {"title": "Оплата произведена, но отображается долг", "payload": "faq:payment_not_applied"},
            {"title": "Расшифровка начислений", "payload": "faq:charges_explanation"},
            {"title": "Почему сильно выросла оплата за воду", "payload": "faq:water_bill_increased_no_meter"},
        ],
    },
    {
        "key": "meters",
        "title": "Приборы учета (поверка/пломбы)",
        "items": [
            {"title": "Кто выполняет поверку счетчика", "payload": "faq:meter_verification_service"},
            {"title": "Распломбировка прибора учета", "payload": "faq:unseal_meter"},
            {"title": "Опломбировка прибора учета", "payload": "faq:seal_meter"},
            {"title": "Какие документы нужно принести", "payload": "faq:documents_required"},
        ],
    },
    {
        "key": "reception",
        "title": "Обращения и приём",
        "items": [
            {"title": "График работы абонентского отдела", "payload": "faq:grafik_abonotd"},
            {"title": "Загруженность отдела сбыта", "payload": "faq:queue_load_time"},
            {"title": "Переоформление лицевого счета", "payload": "faq:pereof_lschet_flat"},
            {"title": "Изменение количества зарегистрированных", "payload": "faq:izmen_kolvo_grazhdan"},
        ],
    },
    {
        "key": "emergency",
        "title": "Аварии и отключения",
        "items": [
            {"title": "Отсутствие воды / отключение", "payload": "faq:no_water"},
            {"title": "Контакты и аварийная служба", "payload": "faq:contacts"},
        ],
    }

,
{
    "key": "tariffs",
    "title": "Тарифы",
    "items": [
        {"title": "Все тарифы (списком)", "payload": "faq:tariffs_all_2026_0101_0930"},
        {"title": "Население г. Таганрога (с НДС)", "payload": "faq:tariffs_population_taganrog_2026"},
        {"title": "Население Новобессергеневского с/п (с НДС)", "payload": "faq:tariffs_population_novobessergenevskoe_2026"},
        {"title": "Население Мясниковского и Неклиновского районов: тех. вода (с НДС)", "payload": "faq:tariffs_population_myasnikov_neklin_tech_2026"},
        {"title": "Юрлица (прочие потребители) (без НДС)", "payload": "faq:tariffs_legal_entities_general_2026"},
        {"title": "Юрлица для населения г. Таганрога (без НДС)", "payload": "faq:tariffs_legal_entities_for_population_taganrog_2026"},
        {"title": "Юрлица для населения Новобессергеневского с/п (без НДС)", "payload": "faq:tariffs_legal_entities_for_population_novobessergenevskoe_2026"},
        {"title": "Юрлица для населения Мясниковского и Неклиновского районов: тех. вода (без НДС)", "payload": "faq:tariffs_legal_entities_for_population_myasnikov_neklin_tech_2026"},
    ],
}]


@app.get("/")
def index():
    return FileResponse(str(CHAT_PATH))


@app.get("/api/hello")
def hello():
    greeting = "Здравствуйте. Выберите категорию вопросов или напишите ваш вопрос."

    # Если файла нет — возвращаем только приветствие
    if not ANNOUNCEMENTS_PATH.exists():
        return {"text": greeting}

    # Если файл есть — читаем
    try:
        ann = ANNOUNCEMENTS_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        # если файл есть, но прочитать не удалось — не показываем объявление
        return {"text": greeting}

    if not ann:
        return {"text": greeting}

    # Показываем одним сообщением: приветствие + объявление
    return {"text": f"{greeting}\n\nОбъявление:\n{ann}"}

@app.get("/mascot.png")
def mascot():
    return FileResponse(str(MASCOT_PATH))


def build_suggestions(candidates: List[Dict[str, Any]], best_id: Optional[str], limit: int = 3):
    quicks = []
    for c in candidates:
        it = c.get("item") or {}
        fid = it.get("id")
        title = it.get("title")
        if not fid or not title:
            continue
        if best_id and fid == best_id:
            continue
        quicks.append({"title": title, "payload": f"faq:{fid}"})
        if len(quicks) >= limit:
            break
    return quicks



def is_tariff_query(text: str) -> bool:
    t = normalize_ru(text)
    if not t:
        return False

    markers = [
        "тариф",
        "тарифы",
        "цена",
        "стоимость",
        "сколько стоит",
        "руб за куб",
        "рублей за куб",
        "за куб",
        "за кубометр",
        "за 1 куб",
        "за м3",
        "за м 3",
    ]
    for m in markers:
        if m in t:
            return True

    tokens = set(tokenize(t))
    if ("цена" in tokens or "стоимость" in tokens) and ("вода" in tokens or "водоотведение" in tokens):
        return True

    return False


def build_tariff_quick(text: str) -> List[Dict[str, str]]:
    t = normalize_ru(text)
    tokens = set(tokenize(t))

    # Признаки "юрлица"
    is_legal = ("юрид" in t) or ("юрлиц" in t) or ("юрлица" in t) or ("юр" in tokens) or ("без ндс" in t)

    # Локации/группы
    is_taganrog = ("таганрог" in t)
    is_novo = ("новобессергенев" in t)
    is_neklin = ("неклинов" in t)
    is_myas = ("мясников" in t)

    # Тип воды
    is_tech = ("техничес" in t) or ("тех" in tokens)

    # Чем конкретнее запрос, тем меньше кнопок:
    # 1) Если явно определился ровно один вариант — вернём одну кнопку (а в chat() можно сразу ответить).
    # 2) Если определилось направление — вернём 2–3 наиболее подходящих.
    quick: List[Dict[str, str]] = []

    if is_legal:
        if is_taganrog:
            quick.append({"title": "Юрлица для населения г. Таганрога (без НДС)", "payload": "faq:tariffs_legal_entities_for_population_taganrog_2026"})
            return quick
        if is_novo:
            quick.append({"title": "Юрлица для населения Новобессергеневского с/п (без НДС)", "payload": "faq:tariffs_legal_entities_for_population_novobessergenevskoe_2026"})
            return quick
        if (is_neklin or is_myas) or is_tech:
            quick.append({"title": "Юрлица для населения Мясниковского и Неклиновского районов: тех. вода (без НДС)", "payload": "faq:tariffs_legal_entities_for_population_myasnikov_neklin_tech_2026"})
            return quick

        # Юрлица без уточнения — 2 кнопки: общий + список
        quick.append({"title": "Юрлица (прочие потребители) (без НДС)", "payload": "faq:tariffs_legal_entities_general_2026"})
        quick.append({"title": "Все тарифы (списком)", "payload": "faq:tariffs_all_2026_0101_0930"})
        return quick

    # Население
    if is_taganrog:
        quick.append({"title": "Население г. Таганрога (с НДС)", "payload": "faq:tariffs_population_taganrog_2026"})
        return quick
    if is_novo:
        quick.append({"title": "Население Новобессергеневского с/п (с НДС)", "payload": "faq:tariffs_population_novobessergenevskoe_2026"})
        return quick
    if (is_neklin or is_myas) or is_tech:
        quick.append({"title": "Население Мясниковского и Неклиновского районов: тех. вода (с НДС)", "payload": "faq:tariffs_population_myasnikov_neklin_tech_2026"})
        return quick

    # Общий вопрос о цене/тарифах — показываем полный набор (без лишнего "основания")
    quick.append({"title": "Все тарифы (списком)", "payload": "faq:tariffs_all_2026_0101_0930"})
    quick.append({"title": "Население г. Таганрога (с НДС)", "payload": "faq:tariffs_population_taganrog_2026"})
    quick.append({"title": "Население Новобессергеневского с/п (с НДС)", "payload": "faq:tariffs_population_novobessergenevskoe_2026"})
    quick.append({"title": "Население Мясниковского и Неклиновского районов: тех. вода (с НДС)", "payload": "faq:tariffs_population_myasnikov_neklin_tech_2026"})
    quick.append({"title": "Юрлица (прочие потребители) (без НДС)", "payload": "faq:tariffs_legal_entities_general_2026"})
    quick.append({"title": "Юрлица для населения г. Таганрога (без НДС)", "payload": "faq:tariffs_legal_entities_for_population_taganrog_2026"})
    quick.append({"title": "Юрлица для населения Новобессергеневского с/п (без НДС)", "payload": "faq:tariffs_legal_entities_for_population_novobessergenevskoe_2026"})
    quick.append({"title": "Юрлица для населения Мясниковского и Неклиновского районов: тех. вода (без НДС)", "payload": "faq:tariffs_legal_entities_for_population_myasnikov_neklin_tech_2026"})
    return quick


# ----------------------------
# Админ-команды: парсер
# ----------------------------
def _parse_admin_block(text: str) -> Tuple[Optional[str], Optional[str], List[str], str, List[str]]:
    """
    Парсит блок для /add и /edit.

    Поддерживаются варианты:
    1) Многострочный:
       id: ...
       title: ...
       keywords: ...
       answer: ... (может быть многострочным)

    2) Однострочный (всё в одной строке/сообщении):
       id: ... title: ... keywords: ... answer: ...

    Возвращает: (fid, title, keywords_list, answer, errors)
    """
    raw = text or ""
    lines = raw.splitlines()

    fid = None
    title = None
    keywords: List[str] = []
    answer_lines: List[str] = []
    errors: List[str] = []

    # --- Вариант 1: многострочный разбор ---
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        low = line.lower()
        if low.startswith("id:"):
            fid = line.split(":", 1)[1].strip()
        elif low.startswith("title:"):
            title = line.split(":", 1)[1].strip()
        elif low.startswith("keywords:"):
            raw_kw = line.split(":", 1)[1].strip()
            keywords = [k.strip() for k in raw_kw.split(",") if k.strip()]
        elif low.startswith("answer:"):
            first = line.split(":", 1)[1].lstrip()
            if first:
                answer_lines.append(first)
            i += 1
            while i < len(lines):
                answer_lines.append(lines[i].rstrip("\n"))
                i += 1
            break
        i += 1

    answer = "\n".join(answer_lines).strip()

    # Если в многострочном варианте не нашли обязательные поля — пробуем вариант 2
    missing = (not fid) or (not title) or (not keywords) or (not answer)
    if missing:
        fid, title, keywords, answer, errors = _parse_admin_markers(raw)
        return fid, title, keywords, answer, errors

    return fid, title, keywords, answer, errors


def _parse_admin_markers(raw: str) -> Tuple[Optional[str], Optional[str], List[str], str, List[str]]:
    """
    Разбор по маркерам 'id:'/'title:'/'keywords:'/'answer:' независимо от переносов строк.
    """
    text = (raw or "").strip()
    low = text.lower()

    keys = ["id:", "title:", "keywords:", "answer:"]
    pos = {k: low.find(k) for k in keys}

    errors: List[str] = []
    for k in keys:
        if pos[k] == -1:
            errors.append(f"Не найден маркер '{k}'")

    if errors:
        # Сформируем ошибки в привычном виде
        out_errors = []
        if "Не найден маркер 'id:'" in errors:
            out_errors.append("Не указан id (строка 'id: ...').")
        if "Не найден маркер 'title:'" in errors:
            out_errors.append("Не указан title (строка 'title: ...').")
        if "Не найден маркер 'keywords:'" in errors:
            out_errors.append("Не указаны keywords (строка 'keywords: ...').")
        if "Не найден маркер 'answer:'" in errors:
            out_errors.append("Не указан answer (строка 'answer: ...').")
        return None, None, [], "", out_errors

    # Сортируем маркеры по позиции и вырезаем значения между ними
    order = sorted(keys, key=lambda k: pos[k])
    values: Dict[str, str] = {}
    for i, k in enumerate(order):
        start = pos[k] + len(k)
        end = pos[order[i + 1]] if i + 1 < len(order) else len(text)
        values[k] = text[start:end].strip()

    fid = values["id:"].strip()
    title = values["title:"].strip()
    kw_raw = values["keywords:"].strip()
    answer = values["answer:"].strip()

    keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]

    out_errors = []
    if not fid:
        out_errors.append("Не указан id (строка 'id: ...').")
    if not title:
        out_errors.append("Не указан title (строка 'title: ...').")
    if not keywords:
        out_errors.append("Не указаны keywords (строка 'keywords: ...').")
    if not answer:
        out_errors.append("Не указан answer (строка 'answer: ...').")

    return fid or None, title or None, keywords, answer, out_errors


def _is_admin(request: Request) -> bool:
    ip = request.client.host if request.client else ""
    return ip in _ADMIN_IPS


def _help_admin() -> str:
    return (
        "Админ-команды:\n"
        "/admin <токен> — войти в админ-режим\n"
        "/logout — выйти из админ-режима\n"
        "/list — список вопросов (id — title)\n"
        "/show <id> — показать вопрос\n"
        "/delete <id> — удалить вопрос\n"
        "/add — добавить вопрос (блоком)\n"
        "/edit <id> — изменить вопрос (блоком)\n\n"
        "Формат блока для /add и /edit:\n"
        "id: <id>\n"
        "title: <заголовок>\n"
        "keywords: ключ1, ключ2, ключ3\n"
        "answer: текст ответа (может быть многострочным)"
    )


@app.post("/api/chat")
def chat(data: ChatIn, request: Request):
    faq = load_faq()

    payload = (data.payload or "").strip()
    text = (data.message or "").strip()

    # 1) payload from buttons
    if payload:
        if payload == "category:__root__":
            quick = [{"title": c["title"], "payload": f"category:{c['key']}"} for c in CATEGORIES]
            return {"answer": "Выберите категорию:", "quickTitle": "Категории", "quickReplies": quick, "navPush": False}

        if payload.startswith("category:"):
            key = payload.split(":", 1)[1]
            cat = next((c for c in CATEGORIES if c["key"] == key), None)
            if not cat:
                quick = [{"title": c["title"], "payload": f"category:{c['key']}"} for c in CATEGORIES]
                return {"answer": "Категория не найдена. Выберите категорию:", "quickTitle": "Категории", "quickReplies": quick, "navPush": False}
            return {
                "answer": f"{cat['title']}. Выберите уточняющий вопрос:",
                "quickTitle": cat["title"],
                "quickReplies": cat["items"],
                "navPush": True
            }

        if payload.startswith("faq:"):
            fid = payload.split(":", 1)[1]
            item = faq_by_id(faq, fid)
            if not item:
                return {"answer": "Ответ по выбранному пункту не найден в базе FAQ.", "quickReplies": [], "quickTitle": ""}
            return {"answer": item.get("answer", ""), "quickReplies": [], "quickTitle": ""}

        # Otherwise treat payload as text query
        text = payload

    # 2) Normal text input
    if not text:
        quick = [{"title": c["title"], "payload": f"category:{c['key']}"} for c in CATEGORIES]
        return {"answer": "Пожалуйста, выберите категорию или напишите вопрос.", "quickTitle": "Категории", "quickReplies": quick, "navPush": False}

    # ----------------------------
    # Админ-команды (только из текстового ввода)
    # ----------------------------
    norm = normalize_ru(text)

    # Вход в админ-режим
    if norm.startswith("admin "):
        token = text.split(" ", 1)[1].strip()
        if token == ADMIN_TOKEN:
            ip = request.client.host if request.client else ""
            if ip:
                _ADMIN_IPS.add(ip)
            return {"answer": "Админ-режим включён. " + _help_admin(), "quickReplies": [], "quickTitle": ""}
        return {"answer": "Неверный токен. Для входа: /admin <токен>.", "quickReplies": [], "quickTitle": ""}

    # Все команды начинаются с "/"
    if text.strip().startswith("/"):
        # /admin <token>
        if text.lower().startswith("/admin"):
            parts = text.split(None, 1)
            token = parts[1].strip() if len(parts) > 1 else ""
            if token == ADMIN_TOKEN and token:
                ip = request.client.host if request.client else ""
                if ip:
                    _ADMIN_IPS.add(ip)
                return {"answer": "Админ-режим включён.\n\n" + _help_admin(), "quickReplies": [], "quickTitle": ""}
            return {"answer": "Неверный формат. Используй: /admin <токен>", "quickReplies": [], "quickTitle": ""}

        # далее команды требуют админ-доступ
        if not _is_admin(request):
            return {"answer": "Недостаточно прав. Сначала: /admin <токен>", "quickReplies": [], "quickTitle": ""}

        # /logout
        if text.lower().startswith("/logout"):
            ip = request.client.host if request.client else ""
            if ip and ip in _ADMIN_IPS:
                _ADMIN_IPS.remove(ip)
            return {"answer": "Админ-режим выключен.", "quickReplies": [], "quickTitle": ""}

        # /help
        if text.lower().startswith("/help"):
            return {"answer": _help_admin(), "quickReplies": [], "quickTitle": ""}

        # /list
        if text.lower().startswith("/list"):
            lines = [f"{it.get('id','')} — {it.get('title','')}".strip(" —") for it in faq]
            lines = [l for l in lines if l.strip()]
            if not lines:
                return {"answer": "FAQ пуст.", "quickReplies": [], "quickTitle": ""}
            return {"answer": "Список FAQ:\n" + "\n".join(lines), "quickReplies": [], "quickTitle": ""}

        # /show <id>
        if text.lower().startswith("/show"):
            parts = text.split(None, 1)
            fid = parts[1].strip() if len(parts) > 1 else ""
            it = faq_by_id(faq, fid) if fid else None
            if not it:
                return {"answer": "Не найдено. Используй: /show <id>", "quickReplies": [], "quickTitle": ""}
            kw = ", ".join([str(k) for k in (it.get("keywords") or [])])
            return {
                "answer": f"id: {it.get('id')}\n"
                          f"title: {it.get('title')}\n"
                          f"keywords: {kw}\n"
                          f"answer: {it.get('answer','')}",
                "quickReplies": [],
                "quickTitle": ""
            }

        # /delete <id>
        if text.lower().startswith("/delete"):
            parts = text.split(None, 1)
            fid = parts[1].strip() if len(parts) > 1 else ""
            if not fid:
                return {"answer": "Используй: /delete <id>", "quickReplies": [], "quickTitle": ""}
            before = len(faq)
            faq = [it for it in faq if it.get("id") != fid]
            after = len(faq)
            if after == before:
                return {"answer": f"Не найден id: {fid}", "quickReplies": [], "quickTitle": ""}
            save_faq(faq)
            return {"answer": f"Удалено: {fid}", "quickReplies": [], "quickTitle": ""}

        # /add (блоком)
        if text.lower().startswith("/add"):
            # Поддержка двух форматов:
            # 1) /add\n... (многострочно)
            # 2) /add id: ... title: ... keywords: ... answer: ... (в одной строке)
            parts = text.split("\n", 1)
            if len(parts) == 1:
                inline = text[len("/add"):].strip()
                if not inline:
                    return {"answer": "Пришли одним сообщением:\n/add\nid: ...\ntitle: ...\nkeywords: ...\nanswer: ...\n\nМожно и в одну строку:\n/add id: ... title: ... keywords: ... answer: ...", "quickReplies": [], "quickTitle": ""}
                block = inline
            else:
                block = parts[1]

            fid, title, keywords, answer, errs = _parse_admin_block(block)
            if errs:
                return {"answer": "Не могу добавить:\n- " + "\n- ".join(errs), "quickReplies": [], "quickTitle": ""}
            if faq_by_id(faq, fid):
                return {"answer": f"Такой id уже существует: {fid}. Используй /edit {fid}", "quickReplies": [], "quickTitle": ""}
            faq.append({"id": fid, "title": title, "keywords": keywords, "answer": answer})
            save_faq(faq)
            return {"answer": f"Добавлено: {fid}", "quickReplies": [], "quickTitle": ""}

        # /edit <id> (блоком)
        if text.lower().startswith("/edit"):
            # Поддержка двух форматов:
            # 1) /edit <id>\n... (многострочно)
            # 2) /edit <id> id: ... title: ... keywords: ... answer: ... (в одной строке)
            header_and_rest = text.split("\n", 1)
            header = header_and_rest[0]

            # Выделяем <id> из команды
            header_parts = header.split(None, 2)
            fid_target = header_parts[1].strip() if len(header_parts) > 1 else ""
            if not fid_target:
                return {"answer": "Используй: /edit <id> (и ниже блок с title/keywords/answer)", "quickReplies": [], "quickTitle": ""}

            it = faq_by_id(faq, fid_target)
            if not it:
                return {"answer": f"Не найден id: {fid_target}", "quickReplies": [], "quickTitle": ""}

            # Inline-формат: всё после '/edit <id>' на той же строке
            inline = ""
            if len(header_parts) > 2:
                inline = header_parts[2].strip()

            if len(header_and_rest) == 1 and not inline:
                # Подсказка текущего шаблона
                kw = ", ".join([str(k) for k in (it.get("keywords") or [])])
                return {
                    "answer": "Пришли одним сообщением:\n"
                              f"/edit {fid_target}\n"
                              f"id: {fid_target}\n"
                              f"title: {it.get('title','')}\n"
                              f"keywords: {kw}\n"
                              f"answer: {it.get('answer','')}",
                    "quickReplies": [],
                    "quickTitle": ""
                }

            block = header_and_rest[1] if len(header_and_rest) > 1 else inline
            fid, title, keywords, answer, errs = _parse_admin_block(block)
            if errs:
                return {"answer": "Не могу изменить:\n- " + "\n- ".join(errs), "quickReplies": [], "quickTitle": ""}

            # id в блоке должен совпадать
            if fid != fid_target:
                return {"answer": f"id в блоке должен быть '{fid_target}', а не '{fid}'.", "quickReplies": [], "quickTitle": ""}

            it["title"] = title
            it["keywords"] = keywords
            it["answer"] = answer
            save_faq(faq)
            return {"answer": f"Обновлено: {fid_target}", "quickReplies": [], "quickTitle": ""}

        return {"answer": "Неизвестная команда. /help", "quickReplies": [], "quickTitle": ""}

    # ----------------------------
    # Приветствие + вопрос: убираем приветствие и продолжаем
    # ----------------------------
    g_stripped = strip_greeting_prefix(text)
    if g_stripped != normalize_ru(text):
        if len(tokenize(g_stripped)) == 0:
            quick = [{"title": c["title"], "payload": f"category:{c['key']}"} for c in CATEGORIES]
            return {
                "answer": "Здравствуйте. Выберите категорию вопросов или напишите ваш вопрос.",
                "quickTitle": "Категории",
                "quickReplies": quick,
                "navPush": False
            }
        text = g_stripped


# Тарифы: если пользователь спрашивает о цене/тарифах — показываем варианты
if is_tariff_query(text):
    tariff_quick = build_tariff_quick(text)

    # Если удалось однозначно определить один вариант — отвечаем сразу
    if len(tariff_quick) == 1:
        only_payload = tariff_quick[0].get("payload", "")
        if only_payload.startswith("faq:"):
            fid = only_payload.split(":", 1)[1]
            item = faq_by_id(faq, fid)
            if item:
                return {"answer": item.get("answer", ""), "quickReplies": [], "quickTitle": ""}

    return {
        "answer": "Уточните, пожалуйста, какой тариф нужен:",
        "quickTitle": "Тарифы",
        "quickReplies": tariff_quick,
        "navPush": True
    }    # Exact title match
    for it in faq:
        if normalize_ru(it.get("title", "")) == normalize_ru(text):
            return {"answer": it.get("answer", ""), "quickReplies": [], "quickTitle": ""}

    tok_count = len(tokenize(text))
    if tok_count <= 4:
        min_score, min_gap = 4.5, 1.5
    else:
        min_score, min_gap = 7.0, 2.2

    res = find_best(text, faq, min_score=min_score, min_gap=min_gap, topk=4)

    if res.get("ok"):
        best = res["best"]["item"]
        best_id = best.get("id")
        answer = best.get("answer", "")
        suggestions = build_suggestions(res.get("candidates") or [], best_id, limit=3)

        if suggestions:
            return {
                "answer": answer,
                "quickTitle": "Также может быть полезно",
                "quickReplies": suggestions,
                "navPush": False
            }
        return {"answer": answer, "quickReplies": [], "quickTitle": ""}

    candidates = res.get("candidates") or []
    quicks = []
    for c in candidates:
        it = c["item"]
        fid = it.get("id")
        title = it.get("title")
        if fid and title:
            quicks.append({"title": title, "payload": f"faq:{fid}"})

    if quicks:
        return {
            "answer": "Не уверен, что понял точно. Выберите, пожалуйста, подходящий вариант:",
            "quickTitle": "Уточнение",
            "quickReplies": quicks,
            "navPush": True
        }

    quick = [{"title": c["title"], "payload": f"category:{c['key']}"} for c in CATEGORIES]
    return {"answer": "Не удалось подобрать точный ответ. Выберите категорию или уточните вопрос.", "quickTitle": "Категории", "quickReplies": quick, "navPush": False}
