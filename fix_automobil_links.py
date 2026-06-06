#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_automobil_links.py — видаляє посилання на automobil.in.ua з поля
"Опис продажу" (description_sale) товарів в Odoo 17.

Використання:
  python fix_automobil_links.py            → тест: 1 товар, без збереження
  python fix_automobil_links.py --full     → повне виправлення всіх товарів
"""

import xmlrpc.client
import configparser
import re
import sys
import os

# ─── Конфіг ───────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, 'odoo_config.txt'))

URL      = config['odoo']['url']
DB       = config['odoo']['database']
USERNAME = config['odoo']['username']
API_KEY  = config['odoo']['api_key']

SEARCH_TERM = 'automobil.in.ua'
LANGS = ['uk_UA', 'ru_RU', None]   # None = мова за замовчуванням (зазвичай uk)

# ─── Функції ──────────────────────────────────────────────────────────────────

def connect():
    common = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/common')
    uid = common.authenticate(DB, USERNAME, API_KEY, {})
    if not uid:
        raise RuntimeError("❌ Не вдалось автентифікуватись в Odoo!")
    models = xmlrpc.client.ServerProxy(f'{URL}/xmlrpc/2/object')
    return uid, models


def clean_description(text: str) -> str:
    """
    Видаляє automobil.in.ua з HTML-тексту:
      1. <a href="...automobil.in.ua...">Текст</a>  →  Текст  (зберігаємо текст)
      2. https://automobil.in.ua/...                →  (порожньо)
      3. automobil.in.ua (без http)                 →  (порожньо)
      4. Порожні <p></p> або <p> </p> після чистки  →  видаляємо
    """
    if not text or SEARCH_TERM not in text.lower():
        return text

    result = text

    # 1. HTML-посилання з automobil.in.ua → лишаємо тільки текст між тегами
    result = re.sub(
        r'<a[^>]*automobil\.in\.ua[^>]*>(.*?)</a>',
        r'\1',
        result,
        flags=re.IGNORECASE | re.DOTALL
    )

    # 2. Голий URL (з http/https та без)
    result = re.sub(
        r'(https?://)?(?:www\.)?automobil\.in\.ua[^\s<>"\']*',
        '',
        result,
        flags=re.IGNORECASE
    )

    # 3. Порожні абзаци після чистки: <p></p> або <p><br></p> тощо
    result = re.sub(
        r'<p[^>]*>\s*(?:<br\s*/?>)?\s*</p>',
        '',
        result,
        flags=re.IGNORECASE
    )

    # 4. Зайві пробіли
    result = re.sub(r'[ \t]{2,}', ' ', result).strip()

    return result


def search_products(uid, models, lang=None):
    """Повертає список ID product.template де description_sale містить SEARCH_TERM."""
    ctx = {'lang': lang} if lang else {}
    ids = models.execute_kw(
        DB, uid, API_KEY,
        'product.template', 'search',
        [[['description_sale', 'ilike', SEARCH_TERM]]],
        {'context': ctx}
    )
    return ids


def read_product(uid, models, tmpl_id, lang=None):
    ctx = {'lang': lang} if lang else {}
    data = models.execute_kw(
        DB, uid, API_KEY,
        'product.template', 'read',
        [[tmpl_id]],
        {'fields': ['name', 'default_code', 'description_sale'], 'context': ctx}
    )
    return data[0] if data else None


def write_product(uid, models, tmpl_id, new_desc, lang=None):
    ctx = {'lang': lang} if lang else {}
    return models.execute_kw(
        DB, uid, API_KEY,
        'product.template', 'write',
        [[tmpl_id], {'description_sale': new_desc}],
        {'context': ctx}
    )


def process_product(uid, models, tmpl_id, save=False):
    """Обробляє один товар: перевіряє всі мови, виводить diff, опціонально зберігає."""
    print(f"\n{'─'*70}")

    any_found = False
    changes = []

    for lang in LANGS:
        lang_label = lang or 'default'
        rec = read_product(uid, models, tmpl_id, lang)
        if not rec:
            continue

        name  = rec.get('name', '')
        code  = rec.get('default_code', '')
        desc  = rec.get('description_sale') or ''

        # Перевіряємо ТІЛЬКИ якщо в цій мові є SEARCH_TERM
        if SEARCH_TERM not in desc.lower():
            continue

        any_found = True
        cleaned = clean_description(desc)

        print(f"\n  Товар ID={tmpl_id} | Артикул: {code} | Назва: {name}")
        print(f"  Мова [{lang_label}]")
        print(f"  ▼ БУЛО  ({len(desc)} симв):")
        # Показуємо тільки 600 символів щоб не засмічувати екран
        preview_was = desc[:600] + ('…' if len(desc) > 600 else '')
        print(f"    {preview_was}")
        print(f"  ▼ СТАНЕ ({len(cleaned)} симв):")
        preview_new = cleaned[:600] + ('…' if len(cleaned) > 600 else '')
        print(f"    {preview_new}")

        changes.append((lang, cleaned))

    if not any_found:
        print(f"  ID={tmpl_id}: SEARCH_TERM відсутній у всіх мовах (пропускаємо)")
        return False

    if save:
        for lang, cleaned in changes:
            write_product(uid, models, tmpl_id, cleaned, lang)
        print(f"\n  ✅ Збережено ({len(changes)} мов(и))")
    else:
        print(f"\n  🔍 DRY RUN — зміни НЕ збережено. Запустіть --full щоб зберегти.")

    return True


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    full_mode = '--full' in sys.argv

    print("=" * 70)
    print("fix_automobil_links.py")
    print(f"Режим: {'ПОВНЕ ВИПРАВЛЕННЯ' if full_mode else 'ТЕСТ (1 товар, без збереження)'}")
    print("=" * 70)

    print("\n🔌 Підключення до Odoo...")
    uid, models = connect()
    print(f"✅ Підключено (uid={uid}, url={URL})")

    # Збираємо ID з усіх мов
    print(f"\n🔍 Пошук товарів з '{SEARCH_TERM}' в description_sale...")
    all_ids = set()
    for lang in LANGS:
        found = search_products(uid, models, lang)
        label = lang or 'default'
        print(f"  [{label}]: {len(found)} товарів")
        all_ids.update(found)

    all_ids = list(all_ids)
    print(f"\n📦 Всього унікальних товарів: {len(all_ids)}")

    if not all_ids:
        print("\n✅ Нічого не знайдено. Все чисто!")
        return

    if not full_mode:
        # Тест — перший товар
        ids_to_process = all_ids[:1]
        print(f"\n>>> ТЕСТОВИЙ РЕЖИМ: обробляємо лише ID={ids_to_process[0]}")
    else:
        ids_to_process = all_ids
        print(f"\n>>> ПОВНИЙ РЕЖИМ: обробляємо {len(ids_to_process)} товарів")

    # Обробка
    processed = 0
    for i, tmpl_id in enumerate(ids_to_process, 1):
        if full_mode:
            print(f"\n[{i}/{len(ids_to_process)}] Обробка ID={tmpl_id}...")
        ok = process_product(uid, models, tmpl_id, save=full_mode)
        if ok:
            processed += 1

    print(f"\n{'='*70}")
    if full_mode:
        print(f"✅ Готово! Оброблено та збережено: {processed} товарів.")
    else:
        print(f"🔍 Тест завершено. Перевірте результат вище.")
        print(f"\nЯкщо все виглядає правильно — запустіть повне виправлення:")
        print(f"  python fix_automobil_links.py --full")
        print(f"\nАбо двічі клацніть FIX_AUTOMOBIL_LINKS.bat")


if __name__ == '__main__':
    main()
