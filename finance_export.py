# -*- coding: utf-8 -*-
"""
Експорт фінансових даних з oDoo для дашборду власника.

Витягує:
  - P&L по місяцях за 12 місяців
  - KPI поточного періоду (виручка, COGS, маржа, OpEx, прибуток)
  - Розподіл по ФОПах (3 окремих юр. особи)
  - Структуру витрат (топ-категорій)
  - Грошову позицію по рахунках
  - Дебіторку/кредиторку (відкриті залишки)

Зберігає в dashboard_data/finance_summary.json
"""
import configparser
import xmlrpc.client as xc
import json
import os
import sys
import socket
import traceback
from collections import defaultdict
from datetime import datetime, timedelta

# Таймаут на кожен XML-RPC запит — запобігає вічному зависанню
socket.setdefaulttimeout(300)   # 5 хв на один запит

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "odoo_config.txt")
OUTPUT_DIR  = os.path.join(SCRIPT_DIR, "dashboard_data")
LOG_FILE    = os.path.join(SCRIPT_DIR, "finance_export.log")
MONTHS_BACK = 12
CHUNK = 500


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def m2o(v): return v[1] if v and isinstance(v, list) and len(v) > 1 else None
def m2o_id(v): return v[0] if v and isinstance(v, list) and v else None


def read_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return (cfg["odoo"]["url"].strip().rstrip("/"),
            cfg["odoo"]["database"].strip(),
            cfg["odoo"]["username"].strip(),
            cfg["odoo"]["api_key"].strip())


def odoo_connect(url, db, user, apikey):
    common = xc.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, user, apikey, {})
    if not uid:
        log("[ERROR] Авторизація не вдалась")
        raise SystemExit(1)
    info = common.version()
    log(f"[OK] Підключено до Odoo {info.get('server_version', '?')}, uid={uid}")
    return uid, xc.ServerProxy(f"{url}/xmlrpc/2/object")


def fetch_lines_by_account(models, db, uid, apikey, account_ids,
                           date_from, date_to):
    """
    Агрегує move.line через read_group — замість читання кожного рядка.
    Повертає список псевдо-рядків з полями account_id, journal_id,
    date (YYYY-MM-01), debit, credit.
    В ~100x швидше ніж посторінкове читання.
    """
    if not account_ids:
        return []
    domain = [
        ("account_id", "in", account_ids),
        ("date", ">=", date_from),
        ("date", "<=", date_to),
        ("parent_state", "=", "posted"),
    ]
    # Маппінг українських назв місяців → номер
    UA_MONTHS = {
        "січ": 1, "лют": 2, "бер": 3, "квіт": 4, "трав": 5, "черв": 6,
        "лип": 7, "серп": 8, "вер": 9, "жовт": 10, "лист": 11, "груд": 12,
    }

    def normalize_month(raw, fallback):
        """Нормалізує date:month до YYYY-MM-01 незалежно від формату Odoo."""
        s = str(raw or "").strip()
        if not s:
            return fallback
        # MM/YYYY (Odoo en_US)
        if "/" in s:
            parts = s.split("/")
            if len(parts) == 2:
                try:
                    return f"{parts[1].zfill(4)}-{parts[0].zfill(2)}-01"
                except Exception:
                    pass
        # YYYY-MM або YYYY-MM-DD
        if len(s) >= 7 and s[4] == "-":
            return s[:7] + "-01"
        # Українські назви місяців: "березня 2026", "березень 2026"
        s_lower = s.lower()
        for prefix, num in UA_MONTHS.items():
            if s_lower.startswith(prefix):
                # Знаходимо рік (4 цифри)
                import re as _re
                yr = _re.search(r"\d{4}", s)
                year = yr.group() if yr else fallback[:4]
                return f"{year}-{str(num).zfill(2)}-01"
        return fallback

    try:
        groups = models.execute_kw(db, uid, apikey, "account.move.line",
            "read_group",
            [domain,
             ["account_id", "journal_id", "debit", "credit"],
             ["account_id", "journal_id", "date:month"]],
            {"lazy": False, "context": {"lang": "en_US"}})
        lines = []
        for g in groups:
            acc = g.get("account_id")
            jrn = g.get("journal_id")
            raw_date = g.get("date:month", "")
            norm_date = normalize_month(raw_date, date_from[:7] + "-01")
            lines.append({
                "account_id": acc if isinstance(acc, list) else [acc, ""],
                "journal_id": jrn if isinstance(jrn, list) else [jrn, ""],
                "date": norm_date,
                "debit": g.get("debit", 0.0),
                "credit": g.get("credit", 0.0),
                "partner_id": False,
                "name": "",
            })
        log(f"      груп після read_group: {len(groups)}")
        return lines
    except Exception as e:
        log(f"[WARN] read_group failed ({e}), fallback до посторінкового читання")
        # Fallback: старий метод з меншим лімітом
        domain2 = domain.copy()
        try:
            total = models.execute_kw(db, uid, apikey, "account.move.line",
                "search_count", [domain2])
        except Exception:
            return []
        log(f"      рядків (fallback): {total}")
        lines = []
        for offset in range(0, min(total, 50000), CHUNK):
            try:
                ids = models.execute_kw(db, uid, apikey, "account.move.line",
                    "search", [domain2],
                    {"limit": CHUNK, "offset": offset, "order": "id"})
                if not ids:
                    break
                batch = models.execute_kw(db, uid, apikey, "account.move.line",
                    "read", [ids, ["account_id", "journal_id", "date",
                                   "debit", "credit", "partner_id", "name"]])
                lines.extend(batch)
            except Exception as ex:
                log(f"[WARN] batch {offset}: {ex}")
        return lines


def main():
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    log("=" * 60)
    log(" Finance export - старт")
    log("=" * 60)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    url, db, user, apikey = read_config()
    log(f"      url={url}  db={db}  user={user}")
    uid, models = odoo_connect(url, db, user, apikey)

    today = datetime.now()
    # Початок 12 місяців тому (1-го числа)
    year = today.year
    month = today.month - MONTHS_BACK + 1
    while month <= 0:
        month += 12
        year -= 1
    period_start = f"{year:04d}-{month:02d}-01"
    period_end = today.strftime("%Y-%m-%d")
    log(f"\nПеріод: {period_start} ... {period_end}")

    # ============================================================
    # 1. Plan of Accounts - карти типів
    # ============================================================
    log(f"\n[1/7] Завантажую план рахунків...")
    accounts = []
    try:
        accs = models.execute_kw(db, uid, apikey, "account.account",
            "search_read", [[]],
            {"fields": ["id", "code", "name", "account_type"]})
        accounts = accs
    except Exception as e:
        log(f"[ERROR] {e}")
    acc_by_id = {a["id"]: a for a in accounts}
    log(f"      завантажено {len(accounts)} рахунків")

    income_acc = [a["id"] for a in accounts
                  if (a.get("account_type") or "").startswith("income")]
    expense_acc = [a["id"] for a in accounts
                   if (a.get("account_type") or "").startswith("expense")]
    cash_acc = [a["id"] for a in accounts
                if a.get("account_type") == "asset_cash"]
    receivable_acc = [a["id"] for a in accounts
                      if a.get("account_type") == "asset_receivable"]
    payable_acc = [a["id"] for a in accounts
                   if a.get("account_type") == "liability_payable"]
    log(f"      income={len(income_acc)}  expense={len(expense_acc)}  "
        f"cash={len(cash_acc)}  recv={len(receivable_acc)}  pay={len(payable_acc)}")

    # ============================================================
    # 2. Журнали - мапа ФОПов
    # ============================================================
    log(f"\n[2/7] Завантажую журнали...")
    journals = []
    try:
        journals = models.execute_kw(db, uid, apikey, "account.journal",
            "search_read", [[]],
            {"fields": ["id", "name", "code", "type"]})
    except Exception as e:
        log(f"[ERROR] {e}")
    journal_by_id = {j["id"]: j for j in journals}
    sale_journals = [j for j in journals if j.get("type") == "sale"]
    log(f"      загалом журналів: {len(journals)}, sale-журналів (ФОП): {len(sale_journals)}")
    for sj in sale_journals:
        log(f"        - [{sj.get('code')}] {sj.get('name')}")

    # ============================================================
    # 3. P&L по місяцях
    # ============================================================
    log(f"\n[3/7] P&L по місяцях за {MONTHS_BACK} міс...")

    log(f"   3.1 INCOME lines...")
    income_lines = fetch_lines_by_account(models, db, uid, apikey,
        income_acc, period_start, period_end)
    log(f"   3.2 EXPENSE lines...")
    expense_lines = fetch_lines_by_account(models, db, uid, apikey,
        expense_acc, period_start, period_end)

    # COGS = рахунки 901000-909999 (Собівартість)
    cogs_acc_ids = set()
    admin_acc_ids = set()
    selling_acc_ids = set()
    other_acc_ids = set()
    for a in accounts:
        if a["id"] not in expense_acc:
            continue
        code = (a.get("code") or "")
        if code.startswith("90"):
            cogs_acc_ids.add(a["id"])
        elif code.startswith("92"):
            admin_acc_ids.add(a["id"])
        elif code.startswith("93"):
            selling_acc_ids.add(a["id"])
        else:
            other_acc_ids.add(a["id"])
    log(f"   COGS accounts: {len(cogs_acc_ids)}, "
        f"Адмін: {len(admin_acc_ids)}, Збут: {len(selling_acc_ids)}, "
        f"Інші: {len(other_acc_ids)}")

    # Агрегація по місяцях
    monthly = defaultdict(lambda: {"revenue": 0.0, "cogs": 0.0,
                                   "admin": 0.0, "selling": 0.0,
                                   "other_exp": 0.0})
    fop_journal_ids = {j["id"] for j in sale_journals}

    by_fop = defaultdict(lambda: {"revenue": 0.0, "cogs": 0.0,
                                  "admin": 0.0, "selling": 0.0})

    for l in income_lines:
        d = (l.get("date") or "")[:7]  # YYYY-MM
        if not d:
            continue
        rev = (l.get("credit", 0) or 0) - (l.get("debit", 0) or 0)
        monthly[d]["revenue"] += rev
        # По ФОПах - тільки якщо journal це sale
        jid = m2o_id(l.get("journal_id"))
        if jid in fop_journal_ids:
            jname = (journal_by_id.get(jid) or {}).get("name", "?")
            by_fop[jname]["revenue"] += rev

    for l in expense_lines:
        d = (l.get("date") or "")[:7]
        if not d:
            continue
        amt = (l.get("debit", 0) or 0) - (l.get("credit", 0) or 0)
        aid = m2o_id(l.get("account_id"))
        if aid in cogs_acc_ids:
            monthly[d]["cogs"] += amt
        elif aid in admin_acc_ids:
            monthly[d]["admin"] += amt
        elif aid in selling_acc_ids:
            monthly[d]["selling"] += amt
        else:
            monthly[d]["other_exp"] += amt

    monthly_list = []
    for ym in sorted(monthly.keys()):
        v = monthly[ym]
        opex = v["admin"] + v["selling"] + v["other_exp"]
        gross = v["revenue"] - v["cogs"]
        ebit = gross - opex
        monthly_list.append({
            "month": ym,
            "revenue": round(v["revenue"], 2),
            "cogs": round(v["cogs"], 2),
            "gross_margin": round(gross, 2),
            "gross_margin_pct": round(gross / v["revenue"] * 100, 2)
                                if v["revenue"] else 0,
            "admin": round(v["admin"], 2),
            "selling": round(v["selling"], 2),
            "other_exp": round(v["other_exp"], 2),
            "opex": round(opex, 2),
            "ebit": round(ebit, 2),
            "ebit_pct": round(ebit / v["revenue"] * 100, 2)
                        if v["revenue"] else 0,
        })

    # KPI поточного місяця
    cur_ym = today.strftime("%Y-%m")
    cur = next((m for m in monthly_list if m["month"] == cur_ym), None)
    # Якщо поточний місяць не закрито - беремо останній наявний
    if not cur and monthly_list:
        cur = monthly_list[-1]

    # Сума за весь період
    total_period = {
        "revenue": sum(m["revenue"] for m in monthly_list),
        "cogs": sum(m["cogs"] for m in monthly_list),
        "admin": sum(m["admin"] for m in monthly_list),
        "selling": sum(m["selling"] for m in monthly_list),
        "other_exp": sum(m["other_exp"] for m in monthly_list),
    }
    total_period["gross_margin"] = total_period["revenue"] - total_period["cogs"]
    total_period["opex"] = (total_period["admin"] + total_period["selling"]
                            + total_period["other_exp"])
    total_period["ebit"] = total_period["gross_margin"] - total_period["opex"]

    # ============================================================
    # 4. По ФОПах - додамо expense (за журналом? - складно, тому пропорційно)
    # ============================================================
    log(f"\n[4/7] Розподіл по ФОПах...")
    fop_list = []
    for fop_name, vals in by_fop.items():
        # Витрати по конкретному ФОПу важко відокремити - у плані рахунків
        # витрати спільні. Пропорційно до виручки - це найкраще наближення.
        share = vals["revenue"] / total_period["revenue"] if total_period["revenue"] else 0
        cogs = total_period["cogs"] * share
        opex = total_period["opex"] * share
        gross = vals["revenue"] - cogs
        ebit = gross - opex
        fop_list.append({
            "fop": fop_name,
            "revenue": round(vals["revenue"], 2),
            "share_pct": round(share * 100, 2),
            "cogs_est": round(cogs, 2),
            "opex_est": round(opex, 2),
            "gross_margin_est": round(gross, 2),
            "ebit_est": round(ebit, 2),
        })
    fop_list.sort(key=lambda x: x["revenue"], reverse=True)

    # ============================================================
    # 5. Структура витрат по рахунках
    # ============================================================
    log(f"\n[5/7] Структура витрат по рахунках...")
    by_account = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})
    for l in expense_lines:
        aid = m2o_id(l.get("account_id"))
        if aid:
            by_account[aid]["debit"] += l.get("debit", 0) or 0
            by_account[aid]["credit"] += l.get("credit", 0) or 0
    expense_breakdown = []
    for aid, vals in by_account.items():
        net = vals["debit"] - vals["credit"]
        if abs(net) < 0.01:
            continue
        a = acc_by_id.get(aid, {})
        code = a.get("code", "")
        # Категорія
        if code.startswith("90"):
            cat = "COGS (Собівартість)"
        elif code.startswith("92"):
            cat = "Адміністративні"
        elif code.startswith("93"):
            cat = "Збут"
        else:
            cat = "Інші"
        expense_breakdown.append({
            "account_code": code,
            "account_name": a.get("name", ""),
            "category": cat,
            "amount": round(net, 2),
        })
    expense_breakdown.sort(key=lambda x: x["amount"], reverse=True)

    # Сума по категоріях (для pie chart)
    by_category = defaultdict(float)
    for e in expense_breakdown:
        by_category[e["category"]] += e["amount"]
    expense_categories = [{"category": k, "amount": round(v, 2)}
                          for k, v in by_category.items()]
    expense_categories.sort(key=lambda x: x["amount"], reverse=True)

    # ============================================================
    # 6. Грошова позиція
    # ============================================================
    log(f"\n[6/7] Грошова позиція...")
    cash_balances = []
    if cash_acc:
        # Сальдо за весь час (не лише період) - це і є поточний залишок
        for aid in cash_acc:
            domain = [("account_id", "=", aid),
                      ("parent_state", "=", "posted")]
            try:
                cnt = models.execute_kw(db, uid, apikey, "account.move.line",
                    "search_count", [domain])
                d = 0; c = 0
                for offset in range(0, min(cnt, 50000), CHUNK):
                    ids = models.execute_kw(db, uid, apikey,
                        "account.move.line", "search", [domain],
                        {"limit": CHUNK, "offset": offset})
                    if not ids:
                        break
                    batch = models.execute_kw(db, uid, apikey,
                        "account.move.line", "read",
                        [ids, ["debit", "credit"]])
                    for l in batch:
                        d += l.get("debit", 0) or 0
                        c += l.get("credit", 0) or 0
                bal = round(d - c, 2)
                if abs(bal) < 0.01:
                    continue  # пропускаємо нульові
                a = acc_by_id.get(aid, {})
                cash_balances.append({
                    "account_code": a.get("code", ""),
                    "account_name": a.get("name", ""),
                    "balance": bal,
                    "is_negative": bal < 0,
                })
            except Exception as e:
                log(f"[WARN] cash {aid}: {e}")
    cash_balances.sort(key=lambda x: x["balance"], reverse=True)
    total_cash = sum(c["balance"] for c in cash_balances)
    negative_accounts = [c for c in cash_balances if c["is_negative"]]

    # ============================================================
    # 7. Дебіторка / Кредиторка (відкриті залишки)
    # ============================================================
    log(f"\n[7/7] Дебіторка/Кредиторка...")

    def get_open_balance(account_ids, label):
        if not account_ids:
            return 0, []
        domain = [("account_id", "in", account_ids),
                  ("parent_state", "=", "posted"),
                  ("full_reconcile_id", "=", False)]
        try:
            cnt = models.execute_kw(db, uid, apikey, "account.move.line",
                "search_count", [domain])
            log(f"      {label} рядків: {cnt}")
            d = 0; c = 0
            by_partner = defaultdict(lambda: {"debit": 0, "credit": 0,
                                              "name": ""})
            # Тільки останні 12 міс - щоб не завалитись на старі імпорти
            domain_recent = list(domain) + [("date", ">=", period_start)]
            cnt_recent = models.execute_kw(db, uid, apikey,
                "account.move.line", "search_count", [domain_recent])
            log(f"      з них за останні 12 міс: {cnt_recent}")
            for offset in range(0, min(cnt_recent, 30000), CHUNK):
                ids = models.execute_kw(db, uid, apikey,
                    "account.move.line", "search", [domain_recent],
                    {"limit": CHUNK, "offset": offset})
                if not ids:
                    break
                batch = models.execute_kw(db, uid, apikey,
                    "account.move.line", "read",
                    [ids, ["debit", "credit", "partner_id", "date"]])
                for l in batch:
                    d += l.get("debit", 0) or 0
                    c += l.get("credit", 0) or 0
                    pf = l.get("partner_id")
                    pid = m2o_id(pf)
                    pname = (m2o(pf) or "")
                    if pid:
                        by_partner[pid]["debit"] += l.get("debit", 0) or 0
                        by_partner[pid]["credit"] += l.get("credit", 0) or 0
                        by_partner[pid]["name"] = pname
            top = []
            for pid, v in by_partner.items():
                net = v["debit"] - v["credit"]
                if abs(net) < 0.01:
                    continue
                top.append({
                    "partner_id": pid,
                    "partner_name": v["name"],
                    "balance": round(net, 2),
                })
            top.sort(key=lambda x: abs(x["balance"]), reverse=True)
            return d - c, top[:30]
        except Exception as e:
            log(f"[WARN] {label}: {e}")
            return 0, []

    rec_balance, top_debtors = get_open_balance(receivable_acc, "RECEIVABLE")
    pay_balance, top_creditors = get_open_balance(payable_acc, "PAYABLE")

    # ============================================================
    # 8. Cash flow по тижнях (рух грошей на касових рахунках)
    # ============================================================
    log(f"\n[8/8] Cash flow по тижнях...")
    cashflow_lines = []
    if cash_acc:
        cf_lines = fetch_lines_by_account(models, db, uid, apikey,
            cash_acc, period_start, period_end)
        log(f"      cash flow рядків: {len(cf_lines)}")
        cashflow_lines = cf_lines

    # Групування по тижнях
    from datetime import datetime as _dt2, timedelta as _td2
    def week_key(date_str):
        try:
            d = _dt2.strptime(date_str[:10], "%Y-%m-%d")
            # Понеділок цього тижня
            mon = d - _td2(days=d.weekday())
            return mon.strftime("%Y-%m-%d")
        except Exception:
            return None

    by_week = defaultdict(lambda: {"inflow": 0.0, "outflow": 0.0, "count": 0})
    by_partner_in = defaultdict(float)
    by_partner_out = defaultdict(float)
    for l in cashflow_lines:
        wk = week_key(l.get("date") or "")
        if not wk:
            continue
        d = l.get("debit", 0) or 0
        c = l.get("credit", 0) or 0
        by_week[wk]["inflow"] += d
        by_week[wk]["outflow"] += c
        by_week[wk]["count"] += 1
        pf = l.get("partner_id")
        pname = m2o(pf) if pf else None
        if pname:
            if d > 0:
                by_partner_in[pname] += d
            if c > 0:
                by_partner_out[pname] += c

    cashflow_weekly = sorted(
        [{"week_start": w,
          "inflow": round(v["inflow"], 2),
          "outflow": round(v["outflow"], 2),
          "net": round(v["inflow"] - v["outflow"], 2),
          "count": v["count"]}
         for w, v in by_week.items()],
        key=lambda x: x["week_start"])

    # ТОП-15 джерел надходжень / витрат
    top_inflows = sorted(
        [{"partner": k, "amount": round(v, 2)} for k, v in by_partner_in.items()],
        key=lambda x: x["amount"], reverse=True)[:15]
    top_outflows = sorted(
        [{"partner": k, "amount": round(v, 2)} for k, v in by_partner_out.items()],
        key=lambda x: x["amount"], reverse=True)[:15]

    cashflow = {
        "weekly": cashflow_weekly[-12:],  # останні 12 тижнів для дайджесту
        "weekly_full": cashflow_weekly,   # всі для дашборду
        "top_inflows": top_inflows,
        "top_outflows": top_outflows,
        "total_inflow": round(sum(w["inflow"] for w in cashflow_weekly), 2),
        "total_outflow": round(sum(w["outflow"] for w in cashflow_weekly), 2),
        "net": round(sum(w["net"] for w in cashflow_weekly), 2),
    }

    # ============================================================
    # 9. Зарплата (рах. 661) та ЄСВ (рах. 65*)
    # ============================================================
    log(f"\n[9/9] Зарплата та ЄСВ...")
    payroll_acc_ids = [a["id"] for a in accounts
                       if (a.get("code") or "").startswith("661")]
    esv_acc_ids     = [a["id"] for a in accounts
                       if (a.get("code") or "").startswith("65")
                       and not (a.get("code") or "").startswith("651000")  # пропускаємо якщо потрібно
                       and a["id"] not in payroll_acc_ids]
    # Включаємо всі 65* (651-659 = ЄСВ та соціальне страхування)
    esv_acc_ids = [a["id"] for a in accounts
                   if (a.get("code") or "").startswith("65")
                   and a["id"] not in payroll_acc_ids]

    log(f"      661 рахунків: {len(payroll_acc_ids)},  65* рахунків: {len(esv_acc_ids)}")

    payroll_lines_raw = []
    esv_lines_raw = []
    if payroll_acc_ids:
        payroll_lines_raw = fetch_lines_by_account(
            models, db, uid, apikey, payroll_acc_ids, period_start, period_end)
    if esv_acc_ids:
        esv_lines_raw = fetch_lines_by_account(
            models, db, uid, apikey, esv_acc_ids, period_start, period_end)

    # Credit на 661/651 = нарахована зарплата/ЄСВ (= витрата)
    payroll_by_month = defaultdict(float)
    esv_by_month     = defaultdict(float)

    for l in payroll_lines_raw:
        d = (l.get("date") or "")[:7]
        if d:
            payroll_by_month[d] += (l.get("credit", 0) or 0)

    for l in esv_lines_raw:
        d = (l.get("date") or "")[:7]
        if d:
            esv_by_month[d] += (l.get("credit", 0) or 0)

    all_pay_months = sorted(set(list(payroll_by_month.keys()) + list(esv_by_month.keys())))
    payroll_monthly_list = []
    for ym in all_pay_months:
        sal  = payroll_by_month.get(ym, 0.0)
        esv  = esv_by_month.get(ym, 0.0)
        payroll_monthly_list.append({
            "month": ym,
            "salary": round(sal, 2),
            "esv":    round(esv, 2),
            "total":  round(sal + esv, 2),
        })

    payroll_total_salary = round(sum(payroll_by_month.values()), 2)
    payroll_total_esv    = round(sum(esv_by_month.values()), 2)
    months_count = max(len(all_pay_months), 1)

    # Зовнішні агенції (вручну) — фіксовані витрати на рекламні агенції
    AGENCIES = [
        {"name": "PPCexperts (automobil.in.ua)", "monthly": 10000},
        {"name": "Internetsolutions (busauto.kh.ua)", "monthly": 10000},
    ]
    agencies_monthly = sum(a["monthly"] for a in AGENCIES)
    agencies_annual  = agencies_monthly * 12

    payroll_data = {
        "monthly": payroll_monthly_list,
        "total": {
            "salary":           payroll_total_salary,
            "esv":              payroll_total_esv,
            "payroll_and_esv":  round(payroll_total_salary + payroll_total_esv, 2),
        },
        "avg_monthly": {
            "salary":           round(payroll_total_salary / months_count, 2),
            "esv":              round(payroll_total_esv / months_count, 2),
            "total":            round((payroll_total_salary + payroll_total_esv) / months_count, 2),
        },
        "agencies": AGENCIES,
        "agencies_monthly": agencies_monthly,
        "agencies_annual":  agencies_annual,
        "total_personnel_monthly": round(
            (payroll_total_salary + payroll_total_esv) / months_count + agencies_monthly, 2),
    }
    log(f"      Зарплата (12 міс): {payroll_total_salary:,.0f} ₴  |  "
        f"ЄСВ: {payroll_total_esv:,.0f} ₴  |  "
        f"Сер/міс: {payroll_data['avg_monthly']['total']:,.0f} ₴")

    # ============================================================
    # 10. Витрати на маркетплейси (631/93 по контрагентах)
    # ============================================================
    log(f"\n[10/10] Витрати на маркетплейси...")

    # Конфігурація маркетплейсів: назва, ключове слово в імені партнера, рахунок (prefix)
    MARKETPLACE_CFG = [
        {"key": "prom",    "label": "Prom.ua",   "partner_kw": "УАПРОМ",  "account_prefix": "631"},
        {"key": "rozetka", "label": "Rozetka",   "partner_kw": "ROZETKA", "account_prefix": "631"},
        {"key": "avto",    "label": "Avto.pro",  "partner_kw": "Avto.pro","account_prefix": "93"},
    ]

    def fetch_marketplace_expenses(partner_keyword, account_prefix):
        """Витрати на маркетплейс: дебет по рахунку account_prefix для партнера з keyword."""
        try:
            # Знаходимо partner_id за ключовим словом
            partners_found = models.execute_kw(db, uid, apikey,
                "res.partner", "search_read",
                [[["name", "ilike", partner_keyword]]],
                {"fields": ["id", "name"], "limit": 10})
            if not partners_found:
                log(f"      Партнер '{partner_keyword}' не знайдений")
                return [], 0.0
            partner_ids = [p["id"] for p in partners_found]
            log(f"      '{partner_keyword}' → {[p['name'] for p in partners_found]}")

            # Знаходимо рахунки за префіксом
            acc_ids = [a["id"] for a in accounts
                       if (a.get("code") or "").startswith(account_prefix)]
            if not acc_ids:
                log(f"      Рахунки {account_prefix}* не знайдені")
                return [], 0.0

            # Читаємо проводки: дебет = витрата
            domain = [
                ("account_id", "in", acc_ids),
                ("partner_id", "in", partner_ids),
                ("date", ">=", period_start),
                ("date", "<=", period_end),
                ("parent_state", "=", "posted"),
            ]
            line_ids = models.execute_kw(db, uid, apikey,
                "account.move.line", "search", [domain], {"limit": 5000})
            if not line_ids:
                return [], 0.0
            lines = models.execute_kw(db, uid, apikey,
                "account.move.line", "read",
                [line_ids, ["date", "debit", "credit"]])

            by_month = defaultdict(float)
            total = 0.0
            for l in lines:
                amt = (l.get("debit", 0) or 0) - (l.get("credit", 0) or 0)
                if amt > 0:
                    ym = (l.get("date") or "")[:7]
                    if ym:
                        by_month[ym] += amt
                    total += amt
            monthly = [{"month": k, "amount": round(v, 2)}
                       for k, v in sorted(by_month.items())]
            return monthly, round(total, 2)
        except Exception as e:
            log(f"      [WARN] marketplace '{partner_keyword}': {e}")
            return [], 0.0

    marketplaces_data = []
    for mp in MARKETPLACE_CFG:
        monthly, total = fetch_marketplace_expenses(mp["partner_kw"], mp["account_prefix"])
        months_cnt = max(len(monthly), 1)
        avg = round(total / months_cnt, 2) if monthly else 0.0
        marketplaces_data.append({
            "key":        mp["key"],
            "label":      mp["label"],
            "total":      total,
            "avg_monthly": avg,
            "monthly":    monthly,
        })
        log(f"      {mp['label']}: {total:,.0f} ₴ за {len(monthly)} міс, сер/міс {avg:,.0f} ₴")

    # ============================================================
    # Збираємо результат
    # ============================================================
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": period_start, "end": period_end,
                   "months_back": MONTHS_BACK},
        "current_month": cur,
        "total_period": {k: round(v, 2) for k, v in total_period.items()},
        "monthly_pnl": monthly_list,
        "fops": fop_list,
        "expense_breakdown": expense_breakdown[:50],
        "expense_categories": expense_categories,
        "cash_balances": cash_balances,
        "total_cash": round(total_cash, 2),
        "negative_accounts": negative_accounts,
        "receivables_total": round(rec_balance, 2),
        "payables_total": round(-pay_balance, 2),  # з мінусом - щоб було додатнім
        "working_capital_gap": round(rec_balance + pay_balance, 2),  # rec - pay (pay від'ємне)
        "top_debtors": top_debtors,
        "top_creditors": [{"partner_id": c["partner_id"],
                           "partner_name": c["partner_name"],
                           "balance": -c["balance"]}
                          for c in top_creditors],
        "cashflow": cashflow,
        "payroll": payroll_data,
        "marketplaces": marketplaces_data,
    }

    # Save
    out = os.path.join(OUTPUT_DIR, "finance_summary.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    size_kb = os.path.getsize(out) / 1024
    log(f"\n[SAVE] finance_summary.json ({size_kb:.1f} KB)")

    # Коротке зведення в лог
    log("\n" + "=" * 60)
    log(f" Період: {MONTHS_BACK} місяців ({period_start} ... {period_end})")
    log("=" * 60)
    log(f" Виручка:                  {total_period['revenue']:>15,.2f} ₴")
    log(f" COGS:                     {total_period['cogs']:>15,.2f} ₴")
    log(f" Валова маржа:             {total_period['gross_margin']:>15,.2f} ₴ "
        f"({total_period['gross_margin']/total_period['revenue']*100:.1f}%)" if total_period['revenue'] else "")
    log(f" Адмін:                    {total_period['admin']:>15,.2f} ₴")
    log(f" Збут:                     {total_period['selling']:>15,.2f} ₴")
    log(f" Інші:                     {total_period['other_exp']:>15,.2f} ₴")
    log(f" OpEx total:               {total_period['opex']:>15,.2f} ₴")
    log(f" EBIT:                     {total_period['ebit']:>15,.2f} ₴")
    log(f"\n Грошова позиція:          {total_cash:>15,.2f} ₴")
    if negative_accounts:
        log(f" ⚠ Негативних рахунків:   {len(negative_accounts)}")
        for n in negative_accounts:
            log(f"   [{n['account_code']}] {n['account_name'][:35]:35} {n['balance']:>15,.2f}")
    log(f"\n Дебіторка (нам винні):    {summary['receivables_total']:>15,.2f} ₴")
    log(f" Кредиторка (ми винні):    {summary['payables_total']:>15,.2f} ₴")
    log(f" Розрив (працює капітал):  {summary['working_capital_gap']:>15,.2f} ₴")
    log(f"\n По ФОПах (за весь період):")
    for fop in fop_list:
        log(f"   {fop['fop'][:40]:40} {fop['revenue']:>15,.2f} ({fop['share_pct']:.1f}%)")
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"\n[FATAL] {type(e).__name__}: {e}")
        log(traceback.format_exc())
        sys.exit(1)
