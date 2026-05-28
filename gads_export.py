# -*- coding: utf-8 -*-
"""
Google Ads daily export → dashboard_data/gads_summary.json

Запуск локально:
  python gads_export.py

GitHub Actions: credentials беруться з environment variables
  (GADS_DEVELOPER_TOKEN, GADS_CLIENT_ID, GADS_CLIENT_SECRET,
   GADS_REFRESH_TOKEN, GADS_LOGIN_CUSTOMER_ID)
  або з файлу mcp-google-ads/google-ads.yaml якщо є локально.
"""
import json
import os
import sys
import yaml
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE  = os.path.join(SCRIPT_DIR, "dashboard_data", "gads_summary.json")
YAML_LOCAL   = os.path.join(SCRIPT_DIR, "mcp-google-ads", "google-ads.yaml")

ACCOUNT_IDS = {
    "4884912382": "busauto.kh.ua",
    "5691399829": "automobil.in.ua",
}
LOGIN_CUSTOMER_ID = "2836486392"

# Акаунти, які доступні НАПРЯМУ (без login_customer_id).
# Якщо акаунт тут — клієнт будується без login_customer_id.
DIRECT_ACCESS_IDS = {"5691399829"}


# ── Credentials ───────────────────────────────────────────────────────────────

def build_config(direct: bool = False):
    """Повертає dict з credentials.
    direct=True — без login_customer_id (для прямодоступних акаунтів).
    """
    dev_token = os.getenv("GADS_DEVELOPER_TOKEN")
    if dev_token:
        cfg = {
            "developer_token": dev_token,
            "client_id":       os.environ["GADS_CLIENT_ID"],
            "client_secret":   os.environ["GADS_CLIENT_SECRET"],
            "refresh_token":   os.environ["GADS_REFRESH_TOKEN"],
            "use_proto_plus":  True,
        }
        if not direct:
            cfg["login_customer_id"] = os.getenv("GADS_LOGIN_CUSTOMER_ID", LOGIN_CUSTOMER_ID)
        return cfg
    # Локально — беремо з yaml файлу
    if os.path.exists(YAML_LOCAL):
        with open(YAML_LOCAL, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["use_proto_plus"] = True
        if direct:
            cfg.pop("login_customer_id", None)
        elif not cfg.get("login_customer_id"):
            cfg["login_customer_id"] = LOGIN_CUSTOMER_ID
        return cfg
    raise RuntimeError(
        "Credentials не знайдено!\n"
        "  Локально: покладіть mcp-google-ads/google-ads.yaml з refresh_token\n"
        "  Якщо токен прострочений — запустіть ОНОВИТИ_GADS_TOKEN.bat\n"
        "  GitHub Actions: задайте секрети GADS_DEVELOPER_TOKEN, GADS_CLIENT_ID,\n"
        "                  GADS_CLIENT_SECRET, GADS_REFRESH_TOKEN"
    )


# ── Query ─────────────────────────────────────────────────────────────────────

GAQL = """
    SELECT
        segments.date,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.conversions,
        metrics.all_conversions,
        metrics.conversions_value
    FROM customer
    WHERE segments.date DURING LAST_30_DAYS
    ORDER BY segments.date ASC
"""


def fetch_account(client, customer_id: str, account_name: str) -> list:
    """Повертає список денних рядків для одного акаунту."""
    from google.ads.googleads.errors import GoogleAdsException

    ga_service = client.get_service("GoogleAdsService")
    rows = []
    try:
        response = ga_service.search(customer_id=customer_id, query=GAQL)
        for row in response:
            cost_uah = row.metrics.cost_micros / 1_000_000
            rows.append({
                "date":           row.segments.date,
                "account":        account_name,
                "impressions":    row.metrics.impressions,
                "clicks":         row.metrics.clicks,
                "cost":           round(cost_uah, 2),
                "conversions":    round(row.metrics.conversions, 2),
                "all_conversions": round(row.metrics.all_conversions, 2),
                "conv_value":     round(row.metrics.conversions_value, 2),
            })
    except GoogleAdsException as ex:
        print(f"[ERROR] {account_name}: {ex.error.code().name}")
        for error in ex.failure.errors:
            print(f"  {error.message}")
    except Exception as ex:
        print(f"[ERROR] {account_name}: {ex}")
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    from google.ads.googleads.client import GoogleAdsClient

    print("[gads_export] Підключаюсь до Google Ads API...")
    os.makedirs(os.path.join(SCRIPT_DIR, "dashboard_data"), exist_ok=True)

    # Два клієнти: прямий (для акаунтів у DIRECT_ACCESS_IDS) і через MCC
    client_direct = GoogleAdsClient.load_from_dict(build_config(direct=True))
    client_mcc    = GoogleAdsClient.load_from_dict(build_config(direct=False))

    all_rows = []
    accounts_meta = []

    for account_id, account_name in ACCOUNT_IDS.items():
        print(f"[gads_export] Акаунт: {account_name} ({account_id})...")
        client = client_direct if account_id in DIRECT_ACCESS_IDS else client_mcc
        rows = fetch_account(client, account_id, account_name)
        all_rows.extend(rows)
        accounts_meta.append({
            "account_id": account_id,
            "name": account_name,
            "site": account_name,
        })
        print(f"  → {len(rows)} рядків")

    if not all_rows:
        print("[ERROR] Немає даних — перевірте credentials та права доступу")
        print("[HINT] Для busauto.kh.ua: додайте busauto.ua@gmail.com як адміна в Google Ads")
        sys.exit(1)

    now_kyiv = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    summary = {
        "generated_at":    now_kyiv,
        "period_days":     30,
        "currency":        "UAH",
        "accounts":        accounts_meta,
        "daily_by_account": sorted(all_rows, key=lambda r: (r["date"], r["account"])),
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    total_cost = sum(r["cost"] for r in all_rows)
    total_cv   = sum(r["conv_value"] for r in all_rows)
    roas = total_cv / total_cost if total_cost else 0
    print(f"\n[gads_export] Готово: {len(all_rows)} рядків")
    print(f"  Витрати: {total_cost:,.0f} UAH  |  Дохід: {total_cv:,.0f} UAH  |  ROAS: {roas:.2f}x")
    print(f"  Збережено: {OUTPUT_FILE}")
    sys.exit(0)


if __name__ == "__main__":
    main()
