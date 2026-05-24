# -*- coding: utf-8 -*-
"""
Фінансовий Telegram-дайджест.

Режими (вибирається автоматично за датою або через --mode):
  weekly  - щопонеділка о 09:00, тижневі підсумки
  monthly - 1-го числа місяця о 09:00, повний P&L закритого місяця
  auto    - за поточною датою

Запуск:
  python finance_digest.py              # auto
  python finance_digest.py --mode weekly
  python finance_digest.py --mode monthly

Налаштування - telegram_config.txt (той самий, що для daily_digest.py)
"""
import json
import os
import sys
import argparse
import configparser
import urllib.request
from datetime import datetime, timedelta, timezone


def now_kyiv():
    """Поточний час Києва незалежно від часового поясу сервера."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        return datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=3)))

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_FILE = os.path.join(SCRIPT_DIR, "dashboard_data", "summary.json")
GADS_FILE    = os.path.join(SCRIPT_DIR, "dashboard_data", "gads_summary.json")
FIN_FILE     = os.path.join(SCRIPT_DIR, "dashboard_data", "finance_summary.json")
TG_CONFIG    = os.path.join(SCRIPT_DIR, "telegram_config.txt")

MONTH_NAMES_UA = ['Січень','Лютий','Березень','Квітень','Травень','Червень',
                  'Липень','Серпень','Вересень','Жовтень','Листопад','Грудень']


def fmt_uah(v):
    if v is None: return "-"
    a = abs(v)
    if a >= 1e6: return f"{v/1e6:.2f} млн ₴"
    if a >= 1e3: return f"{v/1e3:.0f} тис ₴"
    return f"{v:.0f} ₴"


def fmt_pct(v, sign=True):
    if v is None: return "-"
    s = "+" if sign and v >= 0 else ""
    return f"{s}{v:.1f}%"


def read_tg_config(mode="weekly"):
    """
    Повертає (bot_token, chat_id).
    Для mode='accountant' - спочатку шукає chat_id_accountant, інакше fallback.
    Для mode='cashflow' - chat_id_cashflow, інакше fallback.
    """
    if not os.path.exists(TG_CONFIG):
        print(f"[ERROR] Не знайдено {TG_CONFIG}")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(TG_CONFIG, encoding="utf-8")
    sect = cfg["telegram"]
    token = sect["bot_token"].strip()
    default_chat = sect["chat_id"].strip()
    if mode == "accountant" and sect.get("chat_id_accountant"):
        return token, sect["chat_id_accountant"].strip()
    if mode == "cashflow" and sect.get("chat_id_cashflow"):
        return token, sect["chat_id_cashflow"].strip()
    return token, default_chat


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True}
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    resp = urllib.request.urlopen(req, timeout=20)
    result = json.loads(resp.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API: {result}")


def load_json_safe(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Не вдалось прочитати {path}: {e}")
        return None


def build_weekly():
    """Тижневі підсумки на основі денних даних з summary.json."""
    s = load_json_safe(SUMMARY_FILE)
    fin = load_json_safe(FIN_FILE)
    if not s or not s.get("daily"):
        return "❌ Немає даних. Запустіть odoo_export.py"

    daily = s["daily"]
    # Останні 7 днів
    last7 = daily[-7:]
    prev7 = daily[-14:-7] if len(daily) >= 14 else []

    rev = sum(d["revenue"] for d in last7)
    ords = sum(d["orders"] for d in last7)
    aov = rev / ords if ords else 0

    rev_prev = sum(d["revenue"] for d in prev7) if prev7 else 0
    ords_prev = sum(d["orders"] for d in prev7) if prev7 else 0

    rev_change = ((rev / rev_prev - 1) * 100) if rev_prev else None
    ord_change = ((ords / ords_prev - 1) * 100) if ords_prev else None

    week_start = last7[0]["date"]
    week_end = last7[-1]["date"]

    lines = [
        f"📅 *BusAuto · Тиждень* {week_start[5:]} – {week_end[5:]}",
        "",
        f"💰 Виручка: *{fmt_uah(rev)}* ({fmt_pct(rev_change)} до попер.тижня)",
        f"📦 Замовлень: *{ords}* ({fmt_pct(ord_change)})",
        f"🛒 Середній чек: *{fmt_uah(aov)}*",
    ]

    kpi = s.get("kpi") or {}
    if kpi.get("margin_pct"):
        lines.append(f"💹 Валова маржа (за період): *{kpi['margin_pct']:.1f}%*")

    cohort = s.get("cohort") or {}
    if cohort.get("repeat_revenue_pct") is not None:
        lines.append(f"🔄 Повторні клієнти: *{cohort['repeat_revenue_pct']:.0f}%* виручки")

    # Cash position з фінансового export'а
    if fin:
        cash = fin.get("total_cash", 0)
        lines.append(f"💵 Грошова позиція: *{fmt_uah(cash)}*")

        # Прогрес поточного місяця проти попереднього
        m = fin.get("monthly_pnl") or []
        if len(m) >= 2:
            cur_month = m[-1]
            prev_month = m[-2]
            today = datetime.now()
            day_of_month = today.day
            days_in_month = 30  # приблизно
            pace_factor = day_of_month / days_in_month
            projected_rev = cur_month["revenue"] / pace_factor if pace_factor > 0.1 else cur_month["revenue"]
            mom = (projected_rev / prev_month["revenue"] - 1) * 100 if prev_month["revenue"] else None
            lines.append(f"📊 Місяць у progress: {fmt_uah(cur_month['revenue'])} (прогноз кінець міс ~{fmt_uah(projected_rev)})")
            if mom is not None:
                emoji = "📈" if mom >= 0 else "📉"
                lines.append(f"   {emoji} прогноз vs попер.міс: *{fmt_pct(mom)}*")

    # Алерти
    alerts = []
    if fin:
        if fin.get("negative_accounts"):
            neg = fin["negative_accounts"]
            tot_neg = sum(a["balance"] for a in neg)
            alerts.append(f"🚨 Негативні рахунки: {len(neg)} на {fmt_uah(tot_neg)}")
        wcg = fin.get("working_capital_gap", 0)
        if wcg < -200000:
            alerts.append(f"⚠ Розрив дебіт-кредит: {fmt_uah(wcg)}")

    if alerts:
        lines.append("")
        lines.append("⚡️ *Уваги:*")
        for a in alerts:
            lines.append(f"  • {a}")

    lines.append("")
    lines.append(f"_Оновлено: {now_kyiv().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(lines)


def build_monthly():
    """Повний P&L закритого місяця."""
    fin = load_json_safe(FIN_FILE)
    if not fin or not fin.get("monthly_pnl"):
        return "❌ Немає фінансових даних. Запустіть finance_export.py"

    m_list = fin["monthly_pnl"]
    today = datetime.now()
    # Місяць, що щойно закрився
    if today.day <= 5:  # на початку місяця беремо попередній
        if today.month == 1:
            target_year, target_month = today.year - 1, 12
        else:
            target_year, target_month = today.year, today.month - 1
    else:
        target_year, target_month = today.year, today.month
    target_ym = f"{target_year:04d}-{target_month:02d}"

    closed = next((m for m in m_list if m["month"] == target_ym), None)
    if not closed:
        # Беремо передостанній (бо останній - поточний неповний)
        if len(m_list) >= 2:
            closed = m_list[-2]
        else:
            return "❌ Немає закритих місяців у даних"

    # Попередній закритий місяць для порівняння
    closed_idx = m_list.index(closed)
    prev = m_list[closed_idx - 1] if closed_idx > 0 else None

    month_label = MONTH_NAMES_UA[int(closed["month"][5:7]) - 1]
    year_label = closed["month"][:4]

    def change(cur, prev_v):
        if not prev_v: return None
        return (cur / prev_v - 1) * 100

    rev_chg = change(closed["revenue"], prev["revenue"]) if prev else None
    ebit_chg = change(closed["ebit"], prev["ebit"]) if prev else None

    lines = [
        f"📅 *BusAuto · {month_label} {year_label} закрито*",
        "",
        f"💰 Виручка: *{fmt_uah(closed['revenue'])}* ({fmt_pct(rev_chg)} до попер.міс)",
        f"📦 COGS: {fmt_uah(closed['cogs'])} ({(closed['cogs']/closed['revenue']*100):.1f}%)",
        f"💹 *Валова маржа*: *{fmt_uah(closed['gross_margin'])}* ({closed['gross_margin_pct']:.1f}%)",
        "",
        f"⚙️ Адмін: {fmt_uah(closed['admin'])} ({(closed['admin']/closed['revenue']*100):.1f}%)",
        f"🚚 Збут: {fmt_uah(closed['selling'])} ({(closed['selling']/closed['revenue']*100):.1f}%)",
        f"📊 OpEx: {fmt_uah(closed['opex'])} ({(closed['opex']/closed['revenue']*100):.1f}%)",
        "",
        f"💎 *EBIT*: *{fmt_uah(closed['ebit'])}* ({closed['ebit_pct']:.1f}% маржа, {fmt_pct(ebit_chg)} до попер.міс)",
    ]

    # Cash + working capital
    lines.append("")
    lines.append(f"💵 Грошова позиція: *{fmt_uah(fin.get('total_cash', 0))}*")
    lines.append(f"📥 Дебіторка: {fmt_uah(fin.get('receivables_total', 0))}")
    lines.append(f"📤 Кредиторка: {fmt_uah(fin.get('payables_total', 0))}")
    wc = fin.get("working_capital_gap", 0)
    wc_emoji = "✅" if wc >= 0 else "⚠"
    lines.append(f"{wc_emoji} Розрив: {fmt_uah(wc)}")

    # FOP breakdown
    fops = fin.get("fops", [])
    if fops:
        lines.append("")
        lines.append("*За ФОПами (12 міс):*")
        for f in fops[:3]:
            short = f["fop"].replace("ФОП ", "").split()[0]
            lines.append(f"  • {short}: {fmt_uah(f['revenue'])} ({f['share_pct']:.0f}%)")

    # Alerts
    alerts = []
    # EBIT провал
    if closed["ebit_pct"] < 5:
        alerts.append(f"🚨 EBIT впав до {closed['ebit_pct']:.1f}% - критично низький")
    elif closed["ebit_pct"] < 8:
        alerts.append(f"⚠ EBIT нижче норми ({closed['ebit_pct']:.1f}%)")
    # Маржа
    if closed["gross_margin_pct"] < 28:
        alerts.append(f"⚠ Валова маржа впала до {closed['gross_margin_pct']:.1f}%")
    # Зростання витрат
    if prev:
        if prev["admin"] > 0 and closed["admin"] / prev["admin"] > 1.3:
            alerts.append(f"⚠ Адмінвитрати зросли на {(closed['admin']/prev['admin']-1)*100:.0f}%")
        if prev["selling"] > 0 and closed["selling"] / prev["selling"] > 1.3:
            alerts.append(f"⚠ Витрати на збут зросли на {(closed['selling']/prev['selling']-1)*100:.0f}%")

    neg = fin.get("negative_accounts", [])
    if neg:
        alerts.append(f"🚨 {len(neg)} негативних рахунків (провести узгодження)")

    if alerts:
        lines.append("")
        lines.append("*⚡️ Що потребує уваги:*")
        for a in alerts:
            lines.append(f"  • {a}")
    else:
        lines.append("")
        lines.append("✅ Все в нормі")

    lines.append("")
    lines.append(f"_Згенеровано: {now_kyiv().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(lines)


def build_cashflow():
    """Cash-flow дайджест - рух грошей за тиждень."""
    fin = load_json_safe(FIN_FILE)
    if not fin or not fin.get("cashflow"):
        return "❌ Немає cashflow-даних. Запустіть finance_export.py"
    cf = fin["cashflow"]
    weekly = cf.get("weekly", [])
    if not weekly:
        return "❌ Немає тижневих даних cashflow"

    # Останній тиждень (повний)
    last_week = weekly[-1] if weekly else None
    prev_week = weekly[-2] if len(weekly) >= 2 else None

    lines = ["💸 *BusAuto · Cash flow*", ""]
    if last_week:
        lines.append(f"📅 Тиждень {last_week['week_start']}:")
        lines.append(f"  📥 Надходження: *{fmt_uah(last_week['inflow'])}*")
        lines.append(f"  📤 Витрати: *{fmt_uah(last_week['outflow'])}*")
        net_emoji = "✅" if last_week['net'] >= 0 else "🔴"
        lines.append(f"  {net_emoji} *Чистий потік*: *{fmt_uah(last_week['net'])}*")

    if prev_week:
        lines.append("")
        lines.append(f"vs попер.тиждень ({prev_week['week_start']}):")
        delta_in = last_week['inflow'] - prev_week['inflow']
        delta_out = last_week['outflow'] - prev_week['outflow']
        lines.append(f"  Надходження: {fmt_uah(delta_in)} ({fmt_pct((last_week['inflow']/prev_week['inflow']-1)*100) if prev_week['inflow'] else '-'})")
        lines.append(f"  Витрати: {fmt_uah(delta_out)} ({fmt_pct((last_week['outflow']/prev_week['outflow']-1)*100) if prev_week['outflow'] else '-'})")

    # Всього за період
    lines.append("")
    lines.append("*За період (12 міс):*")
    lines.append(f"  📥 Притік: {fmt_uah(cf['total_inflow'])}")
    lines.append(f"  📤 Відтік: {fmt_uah(cf['total_outflow'])}")
    lines.append(f"  💰 Баланс: {fmt_uah(cf['net'])}")

    # Топ-5 джерел і отримувачів
    top_in = cf.get("top_inflows", [])[:5]
    top_out = cf.get("top_outflows", [])[:5]
    if top_in:
        lines.append("")
        lines.append("*Найбільші надходження (12 міс):*")
        for t in top_in:
            lines.append(f"  • {t['partner'][:30]}: {fmt_uah(t['amount'])}")
    if top_out:
        lines.append("")
        lines.append("*Найбільші витрати (12 міс):*")
        for t in top_out:
            lines.append(f"  • {t['partner'][:30]}: {fmt_uah(t['amount'])}")

    # Поточна позиція
    lines.append("")
    lines.append(f"💵 Грошова позиція зараз: *{fmt_uah(fin.get('total_cash', 0))}*")
    if fin.get("negative_accounts"):
        n = len(fin["negative_accounts"])
        lines.append(f"⚠ {n} рахунків в мінусі")

    lines.append("")
    lines.append(f"_Оновлено: {now_kyiv().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(lines)


def build_accountant():
    """Дайджест бухгалтера - фокус на робочих питаннях обліку."""
    fin = load_json_safe(FIN_FILE)
    s = load_json_safe(SUMMARY_FILE)
    if not fin:
        return "❌ Немає фінансових даних"

    lines = ["📋 *BusAuto · Дайджест бухгалтера*", ""]

    # Незведені рахунки
    neg = fin.get("negative_accounts") or []
    if neg:
        lines.append("🚨 *Негативні залишки на рахунках:*")
        for n in neg:
            lines.append(f"  • [{n['account_code']}] {n['account_name']}: *{fmt_uah(n['balance'])}*")
        lines.append("")
        lines.append("_Дія: запустити узгодження банковських виписок (PrivatBank API)_")
        lines.append("")

    # Дебіторка / Кредиторка
    rec = fin.get("receivables_total", 0)
    pay = fin.get("payables_total", 0)
    gap = fin.get("working_capital_gap", 0)
    lines.append("📥 Дебіторка: *" + fmt_uah(rec) + "*")
    lines.append("📤 Кредиторка: *" + fmt_uah(pay) + "*")
    gap_emoji = "✅" if gap >= 0 else "⚠"
    lines.append(f"{gap_emoji} Розрив: *{fmt_uah(gap)}*")

    # ТОП дебіторів - на кого піти
    debtors = fin.get("top_debtors", [])
    big_debtors = [d for d in debtors if d.get("balance", 0) > 50000]
    if big_debtors:
        lines.append("")
        lines.append("*Дебітори >50К ₴ (зателефонувати/нагадати):*")
        for d in big_debtors[:8]:
            lines.append(f"  • {d['partner_name'][:35]}: {fmt_uah(d['balance'])}")

    # ТОП кредиторів
    creditors = fin.get("top_creditors", [])
    big_creditors = [c for c in creditors if c.get("balance", 0) > 50000]
    if big_creditors:
        lines.append("")
        lines.append("*Кредитори >50К ₴ (запланувати оплату):*")
        for c in big_creditors[:8]:
            lines.append(f"  • {c['partner_name'][:35]}: {fmt_uah(c['balance'])}")

    # Незакриті pickings (щоб incoming_qty був чистий)
    if s and s.get("warehouse_abc"):
        wh = s["warehouse_abc"]
        stuck = wh.get("alerts", {}).get("stuck_pickings", []) or []
        if stuck:
            lines.append("")
            lines.append(f"📦 Підвислих pickings (>7 днів): *{len(stuck)}*")
            lines.append("_Дія: відкрити кожен в Inventory → Validate_")

    # Кредит-ноти за період
    if s:
        kpi = s.get("kpi", {})
        if kpi.get("refunds_count"):
            lines.append("")
            lines.append(f"↩️ Кредит-нот за 30 днів: {kpi['refunds_count']} на {fmt_uah(kpi['refunds_amount'])}")

    # Поточний місяць vs план (якщо є)
    m = fin.get("monthly_pnl") or []
    if len(m) >= 2:
        cur = m[-1]
        prev = m[-2]
        if prev["revenue"] > 0:
            today = datetime.now()
            day = today.day
            pace = day / 30
            projected = cur["revenue"] / pace if pace > 0.1 else cur["revenue"]
            mom = (projected / prev["revenue"] - 1) * 100
            emoji = "📈" if mom >= 0 else "📉"
            lines.append("")
            lines.append(f"{emoji} Поточний місяць: {fmt_uah(cur['revenue'])} (прогноз ~{fmt_uah(projected)}, vs попер. {fmt_pct(mom)})")

    lines.append("")
    lines.append(f"_Оновлено: {now_kyiv().strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(lines)


def auto_mode():
    """Визначає режим за поточною датою."""
    today = datetime.now()
    # 1-3 числа місяця → monthly
    if today.day <= 3:
        return "monthly"
    # понеділок → weekly
    if today.weekday() == 0:
        return "weekly"
    # Інакше - weekly (default)
    return "weekly"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["weekly", "monthly", "cashflow",
                                            "accountant", "auto"],
                        default="auto")
    parser.add_argument("--dry-run", action="store_true",
                        help="Тільки вивести дайджест, не відправляти")
    args = parser.parse_args()

    mode = args.mode if args.mode != "auto" else auto_mode()
    print(f"Режим: {mode}")

    if mode == "weekly":
        msg = build_weekly()
    elif mode == "monthly":
        msg = build_monthly()
    elif mode == "cashflow":
        msg = build_cashflow()
    elif mode == "accountant":
        msg = build_accountant()
    else:
        msg = build_weekly()

    print("=" * 50)
    print(msg)
    print("=" * 50)

    if args.dry_run:
        print("\n[DRY-RUN] Повідомлення не відправлено")
        return

    token, chat_id = read_tg_config(mode)
    print(f"\nВідправляю в chat_id={chat_id} (режим: {mode})...")
    try:
        send_telegram(token, chat_id, msg)
        print("[OK] Дайджест надіслано")
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
