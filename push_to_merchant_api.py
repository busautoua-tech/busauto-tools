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
from datetime import datetime

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
    input("Натисніть Enter для виходу...")
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
API_BASE      = f"https://shoppingcontent.googleapis.com/content/v2.1/{MERCHANT_ID}"
API_SCOPES    = ["https://www.googleapis.com/auth/content"]

ODOO_CHUNK    = 200    # скільки товарів брати з Odoo за раз
API_BATCH     = 500    # скільки товарів відправляти в Google за раз (max 1000)
PAUSE_SEC     = 1.0    # пауза між пакетами (щоб не перевантажити API)

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


def load_products(models, db, uid, apikey):
    domain = [
        ["active",        "=", True],
        ["sale_ok",       "=", True],
        ["is_published",  "=", True],
        ["qty_available", ">", 0],
        ["image_1920",    "!=", False],
        ["list_price",    ">", 0],
        ["default_code",  "!=", False],
        ["default_code",  "!=", ""],
    ]
    fields = [
        "id", "name", "default_code", "list_price",
        "product_brand_id", "categ_id",
        "description_sale", "website_url",
        "qty_available",
    ]
    products = []
    offset = 0
    page   = 1
    log("Завантажуємо товари з Odoo...")
    while True:
        chunk = models.execute_kw(db, uid, apikey,
            "product.template", "search_read", [domain],
            {"fields": fields, "limit": ODOO_CHUNK, "offset": offset,
             "context": {"lang": "uk_UA"}})
        if not chunk:
            break
        products.extend(chunk)
        log(f"  Пакет {page}: {len(chunk)} товарів (всього: {len(products)})")
        if len(chunk) < ODOO_CHUNK:
            break
        offset += ODOO_CHUNK
        page   += 1
        time.sleep(0.1)
    log(f"Завантажено з Odoo: {len(products)} товарів")
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
    price = p.get("list_price") or 0.0
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

def main():
    log("=" * 60)
    log("  Google Merchant Center API — BusAuto (163800443)")
    log(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
    products    = load_products(models, db, uid, apikey)
    if not products:
        log("Товарів не знайдено!")
        raise SystemExit(1)

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
        if not code or (p.get("list_price") or 0) <= 0:
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
