# -*- coding: utf-8 -*-
"""
push_to_merchant_api.py — Завантаження товарів в Google Merchant Center через API
BusAuto → Merchant Center 163800443

Схема роботи:
  oDoo (120к товарів) → цей скрипт → Google Merchant Center API → Google Shopping

Переваги перед XML/FTP:
  - Не потрібен FTP і хостинг для файлу
  - Оновлення в реальному часі (не чекати поки Google "забере" файл)
  - Легко перевірити статус кожного товару

Перед запуском (один раз):
  1. console.cloud.google.com → Новий проект → увімкнути "Content API for Shopping"
  2. IAM → Сервісні акаунти → Створити → завантажити JSON ключ
  3. Зберегти JSON ключ як: google_merchant_key.json (в цій же папці)
  4. Merchant Center → Налаштування → Доступ до акаунту
     → Додати користувача → вставити email сервісного акаунту → Стандартний доступ

Детальна інструкція: ІНСТРУКЦІЯ_MERCHANT_API.txt (в цій же папці)

Запуск:
  python push_to_merchant_api.py
  або двічі клікнути: ЗАПУСТИТИ_MERCHANT_API.bat
"""

import configparser
import xmlrpc.client as xc
import json
import os
import sys
import time
import re
import argparse
from datetime import datetime, timedelta

# Google auth — встановлюється один раз: pip install google-auth requests
try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleRequest
    import requests as req_lib
except ImportError:
    print("=" * 60)
    print("ПОМИЛКА: Потрібно встановити бібліотеки.")
    print("Відкрийте командний рядок і виконайте:")
    print("  pip install google-auth requests")
    print("=" * 60)
    sys.exit(1)

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Налаштування ─────────────────────────────────────────────────────
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE   = os.path.join(SCRIPT_DIR, "odoo_config.txt")
CREDS_FILE    = os.path.join(SCRIPT_DIR, "google_merchant_key.json")
LOG_FILE      = os.path.join(SCRIPT_DIR, "merchant_api.log")
PROGRESS_FILE = os.path.join(SCRIPT_DIR, "merchant_api_progress.json")

MERCHANT_ID   = "163800443"
SHOP_URL      = "https://busauto.ua"
# Для batch endpoint merchant ID НЕ входить в URL — він в тілі кожного запиту
API_BASE      = "https://shoppingcontent.googleapis.com/content/v2.1"
API_SCOPES    = ["https://www.googleapis.com/auth/content"]

ODOO_CHUNK    = 500    # скільки товарів брати з Odoo за раз (було 200, збільшено для швидкості)
API_BATCH     = 1000   # скільки товарів відправляти в Google за раз (max 1000)
PAUSE_SEC     = 0.2    # мінімальна пауза між пакетами

# Google product category: 5765 = Vehicle Parts & Accessories
GOOGLE_CATEGORY = "5765"


# ── Логування ────────────────────────────────────────────────────────

def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Прогрес (щоб при повторному запуску не дублювати) ────────────────

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"uploaded": {}, "last_run": None, "total_uploaded": 0}


def save_progress(prog):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2)


# ── Конфіг ───────────────────────────────────────────────────────────

def read_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    s = cfg["odoo"]
    return (s["url"].strip().rstrip("/"),
            s["database"].strip(),
            s["username"].strip(),
            s["api_key"].strip())


# ── Google Auth ───────────────────────────────────────────────────────

def get_google_token():
    """Отримуємо OAuth токен через сервісний акаунт"""
    if not os.path.exists(CREDS_FILE):
        log(f"ПОМИЛКА: Файл ключа не знайдено: {CREDS_FILE}")
        log("Дивіться інструкцію: ІНСТРУКЦІЯ_MERCHANT_API.txt")
        raise SystemExit(1)

    creds = service_account.Credentials.from_service_account_file(
        CREDS_FILE, scopes=API_SCOPES)
    creds.refresh(GoogleRequest())
    log("Google API авторизація успішна")
    return creds


def api_headers(creds):
    """HTTP заголовки з актуальним токеном"""
    creds.refresh(GoogleRequest())
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json; charset=utf-8",
    }


# ── Odoo ─────────────────────────────────────────────────────────────

def odoo_connect(url, db, user, apikey):
    uid = xc.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, user, apikey, {})
    if not uid:
        log("ПОМИЛКА авторизації Odoo!")
        raise SystemExit(1)
    log(f"Odoo авторизація успішна (uid={uid})")
    return uid, xc.ServerProxy(f"{url}/xmlrpc/2/object")


# ── Таблиця надбавок для прайс-листа "Розниця" (правило 881, global) ──
# Формат: (від_UAH, до_UAH, відсоток_надбавки)
MARKUP_TABLE = [
    (0,      250,    60.0),
    (250,    500,    50.0),
    (500,    1000,   40.0),
    (1000,   2000,   30.0),
    (2000,   3000,   25.0),
    (3000,   10000,  20.0),
    (10000,  20000,  15.0),
    (20000,  100000, 10.0),
]
LBS_MIN_MARGIN = 100.0   # мінімальний абсолютний прибуток в UAH

# Категорійні правила: categ_id → фіксований % надбавки (замість таблиці)
# categ "50 %" id=6 | categ "100 %" id=7 | categ "200 %" id=9
CATEG_MARKUP = {
    6: 50.0,
    7: 100.0,
    9: 200.0,
}


def apply_markup(supplier_price_uah, categ_id=None, categ_ancestors=None):
    """
    Розраховуємо роздрібну ціну.
    Пріоритет:
      1. Категорійне правило (categ_id або батьківська категорія = 6/7/9)
      2. Глобальна таблиця надбавок (правило 881)
    """
    if supplier_price_uah <= 0:
        return 0.0

    # Перевіряємо категорію і всіх батьків
    markup_pct = None
    for cid in ([categ_id] if categ_id else []) + (categ_ancestors or []):
        if cid in CATEG_MARKUP:
            markup_pct = CATEG_MARKUP[cid]
            break

    # Якщо категорійне правило не знайдено — глобальна таблиця
    if markup_pct is None:
        markup_pct = 10.0   # за замовчуванням якщо ціна > 100000
        for from_p, to_p, pct in MARKUP_TABLE:
            if from_p <= supplier_price_uah < to_p:
                markup_pct = pct
                break

    retail = supplier_price_uah * (1 + markup_pct / 100.0)
    # Мінімальна маржа: ціна не менше ніж собівартість + LBS_MIN_MARGIN
    retail = max(retail, supplier_price_uah + LBS_MIN_MARGIN)
    return round(retail, 2)


def load_categ_ancestors(models, db, uid, apikey):
    """
    Завантажуємо всі категорії і будуємо карту:
    {categ_id: [categ_id, parent_id, grandparent_id, ...]}
    Щоб перевіряти чи продукт належить до категорії 6/7/9 або її підкатегорії.
    """
    try:
        cats = models.execute_kw(db, uid, apikey,
            "product.category", "search_read",
            [[]],
            {"fields": ["id", "parent_id"], "limit": 2000})
        # Будуємо словник parent: {id: parent_id}
        parent_map = {}
        for c in cats:
            pid = c["parent_id"][0] if c.get("parent_id") else None
            parent_map[c["id"]] = pid

        # Для кожної категорії будуємо повний ланцюг предків
        ancestors = {}
        for cid in parent_map:
            chain = []
            cur = cid
            while cur is not None:
                chain.append(cur)
                cur = parent_map.get(cur)
            ancestors[cid] = chain

        log(f"  Завантажено {len(ancestors)} категорій товарів")
        return ancestors
    except Exception as e:
        log(f"  ⚠ Помилка завантаження категорій: {e}")
        return {}


def load_currency_rates(models, db, uid, apikey):
    """Завантажуємо курси валют з Odoo (кількість UAH за 1 одиницю валюти)"""
    rates = {"UAH": 1.0}
    try:
        currencies = models.execute_kw(db, uid, apikey,
            "res.currency", "search_read",
            [[["active", "=", True]]],
            {"fields": ["name", "rate"]})
        for c in currencies:
            if c.get("name") and c.get("rate"):
                rates[c["name"]] = float(c["rate"])
        log(f"  Курси валют: EUR={rates.get('EUR',0):.2f}  USD={rates.get('USD',0):.2f}  UAH=1.0")
    except Exception as e:
        log(f"  ⚠ Помилка читання курсів, використовуємо запасні: {e}")
        rates.update({"EUR": 52.0, "USD": 44.0})
    return rates


def load_supplier_prices(models, db, uid, apikey, tmpl_ids, currency_rates):
    """
    Завантажуємо ціни постачальників для списку template IDs.
    Повертає {tmpl_id: min_price_uah}
    """
    if not tmpl_ids:
        return {}

    min_price_map = {}
    chunk_size = 2000
    total_loaded = 0

    for i in range(0, len(tmpl_ids), chunk_size):
        chunk_ids = tmpl_ids[i:i + chunk_size]
        try:
            records = models.execute_kw(db, uid, apikey,
                "product.supplierinfo", "search_read",
                [[["product_tmpl_id", "in", chunk_ids],
                  ["price", ">", 0]]],
                {"fields": ["product_tmpl_id", "price", "currency_id", "sequence"],
                 "limit": 50000})
            total_loaded += len(records)
            for rec in records:
                tmpl_id = rec.get("product_tmpl_id")
                if not tmpl_id:
                    continue
                tmpl_id = tmpl_id[0] if isinstance(tmpl_id, (list, tuple)) else tmpl_id
                cur_name = "UAH"
                if rec.get("currency_id"):
                    cur_name = rec["currency_id"][1] if isinstance(rec["currency_id"], (list, tuple)) else "UAH"
                price = float(rec.get("price") or 0)
                rate  = currency_rates.get(cur_name, 1.0)
                price_uah = price * rate
                if price_uah > 0:
                    if tmpl_id not in min_price_map or price_uah < min_price_map[tmpl_id]:
                        min_price_map[tmpl_id] = price_uah
        except Exception as e:
            log(f"  ⚠ Помилка читання supplierinfo пакет {i//chunk_size+1}: {e}")

    log(f"  Завантажено {total_loaded} записів supplierinfo → {len(min_price_map)} шаблонів з мінімальною ціною")
    return min_price_map


def load_products(models, db, uid, apikey, mode="full", days_back=2):
    """
    mode='full'  — всі товари (перший запуск, ~2.5 год)
    mode='daily' — тільки змінені за останні days_back днів (~5-15 хв)
    """
    domain = [
        ["active",        "=", True],
        ["sale_ok",       "=", True],
        ["is_published",  "=", True],
        ["qty_available", ">", 0],
        ["image_1920",    "!=", False],
        ["default_code",  "!=", False],
        ["default_code",  "!=", ""],
    ]

    if mode == "daily":
        since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d %H:%M:%S")
        domain.append(["write_date", ">=", since])
        log(f"Режим DAILY — тільки змінені після {since} UTC")
    else:
        log("Режим FULL — всі товари (перший/повний синк)")

    fields = [
        "id", "name", "default_code", "list_price",
        "product_variant_ids",
        "product_brand_id", "categ_id",
        "description_sale", "website_url",
        "qty_available",
    ]
    products  = []
    offset    = 0
    page      = 1
    chunk_size = ODOO_CHUNK   # починаємо з великого, при помилці зменшуємо
    log(f"Завантажуємо товари з Odoo (пакет: {chunk_size})...")

    while True:
        try:
            chunk = models.execute_kw(db, uid, apikey,
                "product.template", "search_read", [domain],
                {"fields": fields, "limit": chunk_size, "offset": offset,
                 "context": {"lang": "uk_UA"}})
        except Exception as e:
            if chunk_size > 100:
                chunk_size = chunk_size // 2
                log(f"  ⚠ Помилка читання, зменшуємо пакет до {chunk_size}: {e}")
                time.sleep(2)
                continue
            else:
                log(f"  ❌ Критична помилка Odoo: {e}")
                break

        if not chunk:
            break
        products.extend(chunk)
        log(f"  Пакет {page}: {len(chunk)} товарів (всього: {len(products)})")
        if len(chunk) < chunk_size:
            break
        offset += chunk_size
        page   += 1
        time.sleep(0.05)   # мінімальна пауза

    log(f"Завантажено з Odoo: {len(products)} товарів")

    if not products:
        return products

    tmpl_ids = [p["id"] for p in products]

    # ── Крок 1: курси валют ──────────────────────────────────────────
    log("Завантажуємо курси валют...")
    currency_rates = load_currency_rates(models, db, uid, apikey)

    # ── Крок 2: ціни постачальників → мінімальна ціна в UAH ─────────
    log("Завантажуємо ціни постачальників...")
    min_supp_map = load_supplier_prices(models, db, uid, apikey, tmpl_ids, currency_rates)

    # ── Крок 3: ієрархія категорій (для застосування categ правил) ───
    log("Завантажуємо ієрархію категорій...")
    categ_ancestors_map = load_categ_ancestors(models, db, uid, apikey)

    # ── Крок 4: фіксовані ціни прайс-листа (423 варіанти, пріоритет) ─
    RETAIL_PRICELIST_ID = 1
    log("Завантажуємо фіксовані ціни прайс-листа 'Розниця'...")
    variant_price_map = {}
    try:
        pl_offset = 0
        while True:
            items = models.execute_kw(db, uid, apikey,
                "product.pricelist.item", "search_read",
                [[["pricelist_id", "=", RETAIL_PRICELIST_ID],
                  ["applied_on",   "=", "0_product_variant"],
                  ["compute_price","=", "fixed"]]],
                {"fields": ["product_id", "fixed_price"],
                 "limit": 5000, "offset": pl_offset})
            if not items:
                break
            for item in items:
                if item.get("product_id") and item.get("fixed_price", 0) > 1:
                    variant_price_map[item["product_id"][0]] = item["fixed_price"]
            if len(items) < 5000:
                break
            pl_offset += 5000
        log(f"  Фіксованих цін: {len(variant_price_map)}")
    except Exception as e:
        log(f"  ⚠ Помилка читання фіксованих цін: {e}")

    # ── Крок 5: обчислюємо retail_price для кожного шаблону ─────────
    matched_fixed  = 0
    matched_supp   = 0
    no_price       = 0

    for p in products:
        tmpl_id = p["id"]
        retail  = 0.0

        # Пріоритет 1: фіксована ціна варіанта в прайс-листі
        variants = p.get("product_variant_ids") or []
        for vid in variants:
            if vid in variant_price_map:
                retail = variant_price_map[vid]
                matched_fixed += 1
                break

        # Пріоритет 2: обчислення з ціни постачальника + надбавка за категорією
        if retail <= 1:
            min_supp = min_supp_map.get(tmpl_id, 0.0)
            if min_supp > 0:
                # Визначаємо категорію товару та її батьків
                categ_raw = p.get("categ_id")
                categ_id  = categ_raw[0] if isinstance(categ_raw, (list, tuple)) else categ_raw
                ancestors = categ_ancestors_map.get(categ_id, [categ_id] if categ_id else [])
                retail = apply_markup(min_supp, categ_id=categ_id, categ_ancestors=ancestors)
                matched_supp += 1

        # Пріоритет 3: list_price (рідко — тільки для особливих товарів)
        if retail <= 1:
            lp = p.get("list_price") or 0.0
            if lp > 1:
                retail = lp
            no_price += 1

        p["retail_price"] = retail

    log(f"  Цін: фіксовані={matched_fixed} | з постачальника={matched_supp} | без ціни={no_price}")

    # Приклади для перевірки
    examples = [p for p in products if p.get("retail_price", 0) > 1][:5]
    for ex in examples:
        supp = min_supp_map.get(ex["id"], 0)
        categ_raw = ex.get("categ_id")
        categ_id  = categ_raw[0] if isinstance(categ_raw, (list, tuple)) else categ_raw
        ancestors = categ_ancestors_map.get(categ_id, [])
        used_categ = next((c for c in ancestors if c in CATEG_MARKUP), None)
        rule_info = f"categ_rule={used_categ}({CATEG_MARKUP[used_categ]}%)" if used_categ else "global_table"
        log(f"  ✓ {ex.get('default_code')} → retail={ex.get('retail_price')} supp_min={supp:.2f} [{rule_info}]")
    no_ex = [p for p in products if p.get("retail_price", 0) <= 1][:3]
    for ex in no_ex:
        log(f"  ✗ {ex.get('default_code')} → БЕЗ ЦІНИ (list_price={ex.get('list_price')})")

    return products


# ── Підготовка товару для Google API ─────────────────────────────────

def clean_html(text, max_len=None):
    if not text:
        return ""
    t = re.sub(r'<[^>]+>', ' ', str(text))
    t = t.replace('&nbsp;', ' ').replace('&amp;', '&') \
         .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    t = re.sub(r'\s+', ' ', t).strip()
    if max_len and len(t) > max_len:
        t = t[:max_len - 3] + "..."
    return t


def get_brand(product):
    b = product.get("product_brand_id")
    if isinstance(b, (list, tuple)) and len(b) > 1:
        return str(b[1]).strip()
    return ""


def build_product_payload(p):
    """
    Формуємо об'єкт товару у форматі Google Content API
    Документація: https://developers.google.com/shopping-content/reference/rest/v2.1/products
    """
    pid   = p.get("id")
    code  = (p.get("default_code") or "").strip()
    # retail_price — ціна з прайс-листа "Розниця", list_price — запасний варіант
    price = p.get("retail_price") or p.get("list_price") or 0.0
    qty   = p.get("qty_available") or 0
    brand = get_brand(p) or "BusAuto"

    name  = clean_html(p.get("name") or "")
    if brand and brand.lower() not in name.lower():
        title = f"{brand} {name}"
    else:
        title = name
    title = clean_html(title, 150)

    desc = clean_html(p.get("description_sale") or "", 5000)
    if len(desc) < 20:
        desc = (f"Автозапчастина {brand} {name}. "
                "В наявності. Доставка по всій Україні 1-2 дні.")

    # URL сторінки товару
    wu = (p.get("website_url") or "").strip()
    if wu and wu not in ("/", ""):
        link = SHOP_URL + wu
    else:
        link = f"{SHOP_URL}/shop?search={code.replace(' ', '+')}"

    # URL зображення через oDoo endpoint
    image_url = f"{SHOP_URL}/web/image/product.template/{pid}/image_1920"

    product_payload = {
        "offerId":      code,          # унікальний ID товару
        "title":        title,
        "description":  desc,
        "link":         link,
        "imageLink":    image_url,
        "contentLanguage": "uk",       # українська мова
        "targetCountry":   "UA",       # Україна
        "channel":         "online",
        "availability":    "in_stock" if qty > 0 else "out_of_stock",
        "condition":       "new",
        "brand":           brand,
        "mpn":             code,       # для запчастин артикул = MPN
        "price": {
            "value":    f"{price:.2f}",
            "currency": "UAH",
        },
        "googleProductCategory": GOOGLE_CATEGORY,
    }

    # Категорія товару з Odoo
    categ = p.get("categ_id")
    if isinstance(categ, (list, tuple)) and len(categ) > 1:
        product_payload["productTypes"] = [str(categ[1])]

    return product_payload


# ── Відправка в Google API ────────────────────────────────────────────

def send_batch(creds, batch_entries):
    """
    Відправляємо пакет товарів через customBatch endpoint
    Повертає (ok_count, err_count, errors_list)
    """
    payload = {"entries": batch_entries}
    url     = f"{API_BASE}/products/batch"

    resp = req_lib.post(
        url,
        headers=api_headers(creds),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=120,
    )

    if resp.status_code == 401:
        log("  Токен прострочений, оновлюємо...")
        creds.refresh(GoogleRequest())
        resp = req_lib.post(
            url,
            headers=api_headers(creds),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=120,
        )

    if resp.status_code not in (200, 207):
        log(f"  HTTP помилка: {resp.status_code} — {resp.text[:300]}")
        return 0, len(batch_entries), [resp.text[:200]]

    result   = resp.json()
    ok_count  = 0
    err_count = 0
    errors    = []

    for entry in result.get("entries", []):
        errs = entry.get("errors", {}).get("errors", [])
        if errs:
            err_count += 1
            # Логуємо тільки перші 3 помилки щоб не засмічувати лог
            if len(errors) < 3:
                offer = entry.get("product", {}).get("offerId", "?")
                errors.append(f"{offer}: {errs[0].get('message','?')}")
        else:
            ok_count += 1

    return ok_count, err_count, errors


# ── Головна функція ───────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="BusAuto → Google Merchant Center")
    parser.add_argument(
        "--mode",
        choices=["full", "daily"],
        default="daily",
        help="full = всі товари, daily = тільки змінені за N днів"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=2,
        help="Кількість днів для режиму daily (default: 2)"
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Пропустити перші N товарів (для паралельних workers)"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Обробити максимум N товарів, 0 = всі (для паралельних workers)"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    log("=" * 60)
    log("  Google Merchant Center API — BusAuto (163800443)")
    log(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"  Режим: {args.mode.upper()}")
    log("=" * 60)

    # 1. Конфіг і Google авторизація
    try:
        url, db, user, apikey = read_config()
        log(f"Odoo: {url}  DB={db}")
    except Exception as e:
        log(f"ПОМИЛКА конфігу: {e}")
        raise SystemExit(1)

    creds = get_google_token()

    # 2. Odoo
    uid, models = odoo_connect(url, db, user, apikey)
    products    = load_products(models, db, uid, apikey, mode=args.mode, days_back=args.days)
    if not products:
        log("Товарів не знайдено!")
        raise SystemExit(1)

    # Slice для паралельних workers (--offset / --count)
    if args.offset > 0 or args.count > 0:
        start = args.offset
        end   = (args.offset + args.count) if args.count > 0 else len(products)
        products = products[start:end]
        log(f"Worker slice: [{start}:{end}] — {len(products)} товарів")

    # 3. Прогрес
    progress    = load_progress()
    already_done = set(progress.get("uploaded", {}).keys())
    log(f"Вже завантажено раніше: {len(already_done)} товарів")

    # 4. Відправка пакетами
    total_ok  = 0
    total_err = 0
    batch_num = 0
    batch     = []

    for p in products:
        code = (p.get("default_code") or "").strip()
        # Пропускаємо якщо немає артикулу або ціна <= 1 (заглушка в Одо)
        actual_price = p.get("retail_price") or p.get("list_price") or 0
        if not code or actual_price <= 1:
            continue

        try:
            payload = build_product_payload(p)
        except Exception as e:
            log(f"  Помилка підготовки {code}: {e}")
            continue

        entry_id = len(batch) + 1
        batch.append({
            "batchId":    entry_id,
            "merchantId": MERCHANT_ID,
            "method":     "insert",
            "product":    payload,
        })

        if len(batch) >= API_BATCH:
            batch_num += 1
            log(f"Відправляємо пакет #{batch_num} ({len(batch)} товарів)...")
            ok, err, errs = send_batch(creds, batch)
            total_ok  += ok
            total_err += err
            log(f"  OK: {ok}  Помилок: {err}")
            for e_msg in errs:
                log(f"  ⚠ {e_msg}")

            # Зберігаємо прогрес
            for entry in batch:
                oid = entry["product"]["offerId"]
                progress["uploaded"][oid] = datetime.now().strftime("%Y-%m-%d")
            progress["last_run"]      = datetime.now().strftime("%Y-%m-%d %H:%M")
            progress["total_uploaded"] = len(progress["uploaded"])
            save_progress(progress)

            batch = []
            time.sleep(PAUSE_SEC)

    # Відправляємо залишок
    if batch:
        batch_num += 1
        log(f"Відправляємо останній пакет #{batch_num} ({len(batch)} товарів)...")
        ok, err, errs = send_batch(creds, batch)
        total_ok  += ok
        total_err += err
        log(f"  OK: {ok}  Помилок: {err}")
        for e_msg in errs:
            log(f"  ⚠ {e_msg}")
        for entry in batch:
            oid = entry["product"]["offerId"]
            progress["uploaded"][oid] = datetime.now().strftime("%Y-%m-%d")
        progress["last_run"]      = datetime.now().strftime("%Y-%m-%d %H:%M")
        progress["total_uploaded"] = len(progress["uploaded"])
        save_progress(progress)

    # 5. Підсумок
    log("")
    log("=" * 60)
    log(f"✅  ЗАВАНТАЖЕННЯ ЗАВЕРШЕНО!")
    log(f"   Успішно: {total_ok} товарів")
    log(f"   Помилок: {total_err} товарів")
    log(f"   Пакетів відправлено: {batch_num}")
    log("")
    log("   Merchant Center → Товари й магазин → Товари")
    log("   Товари з'являються протягом ~30 хвилин")
    log("=" * 60)


if __name__ == "__main__":
    main()
