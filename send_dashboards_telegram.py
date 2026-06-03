# -*- coding: utf-8 -*-
"""
Надсилає HTML-дашборди в Telegram як documents.

Запуск:
  python send_dashboards_telegram.py
  python send_dashboards_telegram.py --only owner    # тільки один
  python send_dashboards_telegram.py --target accountant  # бухгалтеру

Файли HTML самодостатні (з вшитими даними), тому користувач відкриває
прямо з Telegram, не потрібно нічого качати чи розпаковувати.
"""
import json
import os
import sys
import argparse
import configparser
import urllib.request
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TG_CONFIG  = os.path.join(SCRIPT_DIR, "telegram_config.txt")

DASHBOARDS = [
    ("busauto_owner_dashboard.html",     "owner",     "📊 *Owner dashboard* - оперативні KPI, ROAS, заказы по джерелах"),
    ("busauto_warehouse_dashboard.html", "warehouse", "🏭 *Warehouse dashboard* - ABC, dead stock, підвислі pickings"),
    ("busauto_financial_dashboard.html", "financial", "💰 *Financial dashboard* - P&L, грошова позиція, дебіторка/кредиторка"),
    ("busauto_gads_dashboard.html",      "gads",      "📈 *Google Ads dashboard* - витрати, ROAS, кліки по акаунтах (30 днів)"),
]


def read_tg_config(target="owner"):
    if not os.path.exists(TG_CONFIG):
        print(f"[ERROR] Не знайдено {TG_CONFIG}")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(TG_CONFIG, encoding="utf-8")
    sect = cfg["telegram"]
    token = sect["bot_token"].strip()
    if target == "accountant" and sect.get("chat_id_accountant"):
        return token, sect["chat_id_accountant"].strip()
    return token, sect["chat_id"].strip()


def send_document(token, chat_id, file_path, caption):
    """Надсилає файл через Telegram Bot API (multipart/form-data)."""
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    boundary = '----DashboardBoundary' + os.urandom(8).hex()
    fname = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        content = f.read()

    parts = []
    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
    parts.append(chat_id.encode() + b'\r\n')

    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
    parts.append(caption.encode('utf-8') + b'\r\n')

    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(b'Content-Disposition: form-data; name="parse_mode"\r\n\r\n')
    parts.append(b'Markdown\r\n')

    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(f'Content-Disposition: form-data; name="document"; filename="{fname}"\r\n'.encode())
    parts.append(b'Content-Type: text/html\r\n\r\n')
    parts.append(content + b'\r\n')

    parts.append(f'--{boundary}--\r\n'.encode())
    body = b''.join(parts)

    req = urllib.request.Request(url, data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API: {result}")
    return result


def kyiv_time():
    """Поточний час Києва, незалежно від TZ сервера."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        # Fallback - UTC+3 (літо). Дрібна неточність зимою на 1 год.
        from datetime import timezone, timedelta
        return datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=3)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["owner", "warehouse", "financial", "gads"],
                        help="Надіслати тільки один дашборд")
    parser.add_argument("--target", choices=["owner", "accountant"],
                        default="owner",
                        help="Кому надсилати")
    args = parser.parse_args()

    token, chat_id = read_tg_config(args.target)
    now_str = kyiv_time().strftime('%Y-%m-%d %H:%M')
    print(f"Час Київ: {now_str}")
    print(f"Надсилаю в chat_id={chat_id}\n")

    sent = 0
    skipped = 0
    for fname, key, caption in DASHBOARDS:
        if args.only and args.only != key:
            continue
        path = os.path.join(SCRIPT_DIR, fname)
        if not os.path.exists(path):
            print(f"[SKIP] {fname} не знайдено")
            skipped += 1
            continue
        size_kb = os.path.getsize(path) / 1024
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        age_h = (datetime.now() - mtime).total_seconds() / 3600
        full_caption = f"{caption}\n\n📁 {fname} · {size_kb:.0f} KB · оновлено {age_h:.1f} год тому"
        print(f"Надсилаю {fname} ({size_kb:.0f} KB, {age_h:.1f}h)...")
        try:
            send_document(token, chat_id, path, full_caption)
            print(f"  [OK]")
            sent += 1
        except Exception as e:
            print(f"  [ERROR] {e}")
    print(f"\nГотово: надіслано {sent}, пропущено {skipped}")


if __name__ == "__main__":
    main()
