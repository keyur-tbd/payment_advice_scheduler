"""
Supabase side: one table per customer (pa_<customer>), a per-mail ledger
(pa_mail_log) and run rows in the shared workflow_logs.

Every customer table has the same columns, one row per advice line, so they can
be stacked with UNION ALL later. Loads are idempotent: an advice's rows are
replaced by (mail_id, source_file, advice_ref) inside one transaction.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
from collections import Counter
from typing import Dict, Iterable, List, Optional

from feeds import CUSTOMERS, FEEDS
from pa_common import Advice, Mail, to_jsonable

PARSED_BY = "local parser (payment_advice_pipeline)"


def table_ddl(table: str) -> str:
    return f"""
create table if not exists public.{table} (
    id               bigint generated always as identity primary key,
    row_hash         text        not null unique,
    feed             text        not null,
    customer_code    text,
    payer_name       text,
    advice_ref       text,
    utr              text,
    payment_date     date,
    payment_amount   numeric,
    line_no          integer     not null,
    invoice_no       text,
    invoice_date     date,
    doc_ref          text,
    line_type        text        not null,
    description      text,
    gross_amount     numeric,
    tds_amount       numeric,
    deduction_amount numeric,
    net_amount       numeric,
    mail_id          text        not null,
    mail_date        timestamptz,
    mail_from        text,
    mail_subject     text,
    source_file      text        not null,
    parsed_by        text        not null,
    raw_data         jsonb,
    loaded_at        timestamptz not null default now()
);
comment on table public.{table} is
    'Payment advice lines, one row per line, parsed locally (no LLM) from accounts@ / instamart@ mail. '
    'Lines that add up to the printed total load as-is; a mismatch loads with an extra ROUND_OFF line '
    '(net_amount = printed total less parsed lines, pending review); an advice with no printed total loads '
    'unchecked with payment_amount null. See pa_mail_log.status.';
comment on column public.{table}.net_amount is
    'signed amount this line adds to the payment: invoices +, TDS / GDN / PRN / holds -';
create index if not exists {table}_advice_ref_idx   on public.{table} (advice_ref);
create index if not exists {table}_invoice_no_idx   on public.{table} (invoice_no);
create index if not exists {table}_payment_date_idx on public.{table} (payment_date);
create index if not exists {table}_mail_id_idx      on public.{table} (mail_id);
alter table public.{table} enable row level security;
revoke insert, update, delete, truncate, references, trigger on public.{table} from anon, authenticated;
"""


LOG_DDL = """
create table if not exists public.pa_mail_log (
    feed           text        not null,
    mail_id        text        not null,
    source_file    text        not null default '',
    advice_idx     integer     not null default 0,
    customer       text,
    target_table   text,
    advice_ref     text,
    payment_date   date,
    payment_amount numeric,
    line_sum       numeric,
    diff           numeric,
    line_count     integer,
    status         text        not null,
    detail         text,
    mail_date      timestamptz,
    mail_subject   text,
    processed_at   timestamptz not null default now(),
    primary key (feed, mail_id, source_file, advice_idx)
);
comment on table public.pa_mail_log is
    'One row per payment advice (or skipped mail) the pipeline looked at. status: loaded / header_only = in the '
    'customer table, lines add up to the printed total; loaded_round_off = in the table with a ROUND_OFF line '
    'absorbing the difference (diff = parsed lines less printed total; review pending); loaded_no_total = in the '
    'table unchecked, payer printed no total (payment_amount null); duplicate / superseded = same advice_ref came '
    'from another copy; no_lines / error = NOT loaded, needs a parser fix; skipped = not an advice (ticket, '
    'chatter); mail_done (advice_idx -1) = every advice in the mail was handled. mismatch / no_total were the '
    'not-loaded states before 2026-10-26.';
create index if not exists pa_mail_log_status_idx on public.pa_mail_log (status);
alter table public.pa_mail_log enable row level security;
revoke insert, update, delete, truncate, references, trigger on public.pa_mail_log from anon, authenticated;
"""



def schema_sql() -> str:
    return "\n".join([LOG_DDL] + [table_ddl(c.table) for c in CUSTOMERS.values()])


# ---------------------------------------------------------------------------
def _load_env_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def connect(env_file: Optional[str] = None):
    import psycopg2
    _load_env_file(env_file or os.environ.get("PA_ENV_FILE", ""))
    dsn = os.environ["SUPABASE_DB_URL"]
    extra = []
    if "sslmode=" not in dsn:
        extra.append(f"sslmode={os.environ.get('SUPABASE_DB_SSLMODE', 'require')}")
    root = os.environ.get("SUPABASE_DB_SSLROOTCERT")
    if root and not os.path.isabs(root) and env_file:      # relative to the .env that named it
        root = os.path.join(os.path.dirname(os.path.abspath(env_file)), root)
    if root and "sslrootcert=" not in dsn:
        extra.append(f"sslrootcert={root}")
    if extra:
        dsn += ("&" if "?" in dsn else "?") + "&".join(extra)
    conn = psycopg2.connect(dsn, connect_timeout=30, keepalives=1, keepalives_idle=30)
    # Autocommit: each mail's writes go out as ONE multi-statement query (see _flush), which
    # Postgres runs as one implicit transaction - all or nothing, one round trip (~0.4-0.9 s
    # to the Sydney pooler) instead of BEGIN + statements + COMMIT.
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("set statement_timeout = '600000'")
    return conn


_PENDING: Dict[int, List[bytes]] = {}


def _queue(conn, sql) -> None:
    _PENDING.setdefault(id(conn), []).append(sql if isinstance(sql, bytes) else sql.encode())


def _flush(conn) -> None:
    sql = _PENDING.pop(id(conn), [])
    if sql:
        with conn.cursor() as cur:
            cur.execute(b";\n".join(sql))


def apply_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(schema_sql())
    conn.commit()


FAILED = ("mismatch", "no_total", "no_lines", "error")      # mismatch / no_total: rows from before 2026-10-26
LOADED = ("loaded", "header_only", "loaded_round_off", "loaded_no_total")
RETRY = FAILED + ("loaded_round_off", "loaded_no_total")    # what --retry-failed reads again (a parser fix may improve them)


def done_mail_ids(conn, feed: str, retry_failed: bool = False) -> set:
    """Mails this feed finished: a 'mail_done' marker (written after every advice in the
    mail was handled, so a run killed mid-mail re-reads it) or a skip/error row.
    With retry_failed, mails holding any failed, rounded-off or untotalled advice are read again."""
    with conn.cursor() as cur:
        cur.execute("""select mail_id from public.pa_mail_log where feed = %s
                       group by mail_id
                       having bool_or(status in ('mail_done', 'skipped', 'error'))
                          and not (%s and bool_or(status = any(%s)))""",
                    (feed, retry_failed, list(RETRY)))
        return {r[0] for r in cur.fetchall()}


def mark_mail_done(conn, feed: str, mail: Mail, advices: int) -> None:
    """Close the mail: its queued advice writes plus the marker, in one transaction."""
    with conn.cursor() as cur:
        _queue(conn, _log_sql(cur, feed, mail, "", -1, "mail_done", f"{advices} advice(s)"))
    _flush(conn)


# ---------------------------------------------------------------------------
def _row_hash(*parts) -> str:
    return hashlib.sha256("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()




def _log_sql(cur, feed: str, mail: Mail, source_file: str, idx: int, status: str, detail: str = "",
             advice: Optional[Advice] = None, diff: Optional[float] = None) -> bytes:
    cust = CUSTOMERS.get(advice.customer) if advice else None
    return cur.mogrify(
        """insert into public.pa_mail_log (feed, mail_id, source_file, advice_idx, customer, target_table,
               advice_ref, payment_date, payment_amount, line_sum, diff, line_count, status, detail,
               mail_date, mail_subject, processed_at)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
           on conflict (feed, mail_id, source_file, advice_idx) do update set
               customer = excluded.customer, target_table = excluded.target_table,
               advice_ref = excluded.advice_ref, payment_date = excluded.payment_date,
               payment_amount = excluded.payment_amount, line_sum = excluded.line_sum, diff = excluded.diff,
               line_count = excluded.line_count, status = excluded.status, detail = excluded.detail,
               mail_date = excluded.mail_date, mail_subject = excluded.mail_subject, processed_at = now()""",
        (feed, mail.id, source_file, idx, advice.customer if advice else None, cust.table if cust else None,
         advice.advice_ref if advice else None, advice.payment_date if advice else None,
         advice.payment_amount if advice else None, advice.line_sum() if advice else None, diff,
         len(advice.lines) if advice else None, status, detail[:2000] if detail else None,
         mail.date, mail.subject[:500]))


def log_mail(conn, feed: str, mail: Mail, status: str, detail: str = "", source_file: str = "") -> None:
    with conn.cursor() as cur:
        _queue(conn, _log_sql(cur, feed, mail, source_file, 0, status, detail))
    _flush(conn)


class RefIndex:
    """advice_ref -> {(feed, mail_id)} per customer table, read once per run so the duplicate
    check costs no round trip (the Supabase pooler is ~0.25 s away; one select per advice
    made a full load take hours)."""

    def __init__(self, conn):
        self.conn, self.tables = conn, {}

    def get(self, table: str) -> Dict[str, set]:
        if table not in self.tables:
            idx: Dict[str, set] = {}
            with self.conn.cursor() as cur:
                cur.execute(f"select distinct advice_ref, feed, mail_id from public.{table} where advice_ref is not null")
                for ref, f, m in cur.fetchall():
                    idx.setdefault(ref, set()).add((f, m))
            self.tables[table] = idx
        return self.tables[table]


REUSE_DAYS = 60   # same advice no., > 60 days and > Re 1 apart from every stored copy = a different payment


def _other_payment(cur, table: str, advice: Advice, mail: Mail) -> bool:
    """True when every stored copy of this advice_ref (from other mails) is a different payment: payment
    date more than REUSE_DAYS away AND amount more than Re 1 away. Both must differ: Vijetha re-sends an
    advice months later under a new date with the same amount and lines, and that stays a duplicate.
    Checked 2026-11-19 over ~23k 'duplicate' log rows: only Zepto's 3 reused numbers meet it."""
    if advice.payment_date is None or advice.payment_amount is None:
        return False
    cur.execute(f"select distinct payment_date, payment_amount from public.{table} "
                f"where advice_ref = %s and mail_id <> %s", (advice.advice_ref, mail.id))
    rows = cur.fetchall()
    return bool(rows) and all(
        d is not None and a is not None and abs((d - advice.payment_date).days) > REUSE_DAYS
        and abs(float(a) - advice.payment_amount) > 1 for d, a in rows)


def load_advice(conn, feed: str, mail: Mail, idx: int, advice: Advice, refs: Optional[RefIndex] = None) -> str:
    """Queue one checked advice's writes (delete + insert + log). Returns the pa_mail_log
    status. Nothing is sent here: mark_mail_done sends the whole mail as one transaction, so
    a run killed mid-mail leaves nothing half-written."""
    status, diff = advice.check()
    cust = CUSTOMERS[advice.customer]
    table = cust.table
    refs = refs or RefIndex(conn)
    with conn.cursor() as cur:
        if status == "no_lines":
            _queue(conn, _log_sql(cur, feed, mail, advice.source_file, idx, status, "", advice, diff))
            return status
        # 2026-10-26 (user): nothing with lines stays out. A mismatch loads with a ROUND_OFF line that
        # brings the lines to the printed total; an advice with no printed total loads unchecked.
        detail = ""
        if status == "mismatch":
            detail = f"lines sum {advice.line_sum()} vs printed {advice.payment_amount}; ROUND_OFF {round(-diff, 2)} added"
        elif status == "no_total":
            detail = "no printed total; lines loaded unchecked"

        known = refs.get(table)
        merged = False
        if advice.advice_ref and any(m != mail.id for _, m in known.get(advice.advice_ref, set())) \
                and _other_payment(cur, table, advice, mail):
            # A payer reused the number for another payment (Zepto: 2000012591 = Rs 43,156 on 27 Feb 2024 and
            # Rs 1,03,236 on 12 May 2026). Keyed apart so it is not dropped as a 'duplicate' of the first.
            reused = advice.advice_ref
            advice = dataclasses.replace(advice, advice_ref=f"{reused}@{advice.payment_date:%Y-%m-%d}")
            detail = "; ".join(x for x in (detail, f"advice no. {reused} also names another payment: keyed "
                                                    f"{advice.advice_ref}") if x)
        if advice.advice_ref:
            others = {(f, m) for f, m in known.get(advice.advice_ref, set()) if m != mail.id}
            mine = FEEDS[feed].priority
            if (FEEDS[feed].merge_repeats and others and status == "ok"
                    and all(FEEDS[f].priority == mine for f, _ in others)):
                # Statements that repeat a transfer as it fills in (Instamart VCredits): add only the lines
                # the earlier mails did not carry (matched on invoice / doc ref + amount), then restate the
                # transfer's total on all its rows. An identical repeat adds nothing and logs 'duplicate'.
                cur.execute(f"select invoice_no, doc_ref, net_amount from public.{table} "
                            f"where advice_ref = %s and mail_id <> %s", (advice.advice_ref, mail.id))
                have = Counter((i, d, round(float(n or 0), 2)) for i, d, n in cur.fetchall())
                new = []
                for ln in advice.lines:
                    k = (ln.invoice_no, ln.doc_ref, round(ln.net_amount or 0, 2))
                    if have[k] > 0:
                        have[k] -= 1
                    else:
                        new.append(ln)
                src = ", ".join(sorted(f"{f}:{m}" for f, m in others))
                if not new:
                    _queue(conn, _log_sql(cur, feed, mail, advice.source_file, idx, "duplicate",
                                          f"already loaded from {src}", advice, diff))
                    return "duplicate"
                advice = dataclasses.replace(advice, lines=new, payment_amount=round(sum(l.net_amount or 0 for l in new), 2))
                status, diff = advice.check()
                detail = f"merged {len(new)} new line(s) into the transfer also in {src}"
                merged = True
                others = set()
            if any(FEEDS[f].priority >= mine for f, _ in others):
                src = ", ".join(sorted(f"{f}:{m}" for f, m in others))
                _queue(conn, _log_sql(cur, feed, mail, advice.source_file, idx, "duplicate",
                                      f"already loaded from {src}", advice, diff))
                return "duplicate"
            for f, m in others:          # lower-priority copies give way
                _queue(conn, cur.mogrify(f"delete from public.{table} where mail_id = %s and advice_ref = %s",
                                         (m, advice.advice_ref)))
                _queue(conn, cur.mogrify("update public.pa_mail_log set status = 'superseded', detail = %s "
                                         "where mail_id = %s and advice_ref = %s",
                                         (f"replaced by {feed}:{mail.id}", m, advice.advice_ref)))
                known[advice.advice_ref].discard((f, m))

        sql = [cur.mogrify(f"delete from public.{table} where mail_id = %s and source_file = %s "
                           f"and advice_ref is not distinct from %s",
                           (mail.id, advice.source_file, advice.advice_ref))]
        lines = advice.lines
        if advice.header_only:
            from pa_common import Line
            lines = [Line(line_type="PAYMENT", net_amount=advice.payment_amount,
                          description=advice.extra.get("narration"))]
        elif status == "mismatch":
            from pa_common import Line
            lines = advice.lines + [Line(
                line_type="ROUND_OFF", net_amount=round(-diff, 2),
                description=f"printed total {advice.payment_amount} less parsed lines {advice.line_sum()}")]
        code = cust.codes[0] if len(cust.codes) == 1 else advice.extra.get("customer_code")
        values = []
        for n, l in enumerate(lines, start=1):
            raw = {"advice_extra": advice.extra, "line_extra": l.extra} if (advice.extra or l.extra) else None
            values.append(
                (_row_hash(table, mail.id, advice.source_file, advice.advice_ref, n), feed, code,
                 advice.payer_name, advice.advice_ref, advice.utr, advice.payment_date, advice.payment_amount,
                 n, l.invoice_no, l.invoice_date, l.doc_ref, l.line_type, l.description, l.gross_amount,
                 l.tds_amount, l.deduction_amount, l.net_amount, mail.id, mail.date, mail.sender,
                 mail.subject[:500], advice.source_file, PARSED_BY,
                 json.dumps(raw, default=to_jsonable) if raw else None))
        placeholder = "(" + ",".join(["%s"] * 25) + ")"
        for start in range(0, len(values), 500):
            sql.append(
                f"""insert into public.{table} (row_hash, feed, customer_code, payer_name, advice_ref, utr,
                        payment_date, payment_amount, line_no, invoice_no, invoice_date, doc_ref, line_type,
                        description, gross_amount, tds_amount, deduction_amount, net_amount, mail_id, mail_date,
                        mail_from, mail_subject, source_file, parsed_by, raw_data)
                    values """.encode()
                + b",".join(cur.mogrify(placeholder, v) for v in values[start:start + 500])
                + b" on conflict (row_hash) do nothing")
        final = {"ok": "loaded", "header_only": "header_only",
                 "mismatch": "loaded_round_off", "no_total": "loaded_no_total"}[status]
        if merged:                       # the transfer's total = all its rows, whichever mail brought them
            sql.append(cur.mogrify(f"update public.{table} set payment_amount = t.total from "
                                   f"(select sum(net_amount) total from public.{table} where advice_ref = %s) t "
                                   f"where advice_ref = %s", (advice.advice_ref, advice.advice_ref)))
        sql.append(_log_sql(cur, feed, mail, advice.source_file, idx, final, detail, advice, diff))
        for part in sql:
            _queue(conn, part)
        if advice.advice_ref:
            known.setdefault(advice.advice_ref, set()).add((feed, mail.id))
    return final


# Instamart VCredits rows with no UTR are keyed by entity + vendor code + date (Zoho Books ones with no UTR by
# 'ZOHO-<id>'), so the same payment can sit under that key AND under its UTR (Zoho Books, another VCredits file
# that printed the UTR, the vendor portal export), or under two date keys. Measured 2026-11-17: 418 such rows, Rs 2.6 Cr counted twice. A row of an undated-key advice is a copy
# when the same invoice / doc ref and amount is paid within 30 days under another advice_ref (VCredits
# dates can run 10-16 days ahead of the portal's payment date); the UTR copy is kept,
# else the earliest. Runs after every load, so the order the mails arrive in does not matter.
UNKEYED_DUPES_SQL = """
create temp table pa_dup as
select d.row_hash, d.advice_ref, k.keeper
  from public.pa_instamart d
  cross join lateral (
    select o.advice_ref as keeper
      from public.pa_instamart o
     where o.advice_ref <> d.advice_ref
       and coalesce(o.invoice_no, o.doc_ref) = coalesce(d.invoice_no, d.doc_ref)
       and abs(o.net_amount - d.net_amount) < 0.01
       and abs(o.payment_date - d.payment_date) <= 30
       and (o.utr is not null or (o.payment_date, o.advice_ref) < (d.payment_date, d.advice_ref))
     order by (o.utr is null), o.payment_date, o.advice_ref
     limit 1) k
 where d.feed in ('instamart_vcredits', 'instamart') and d.utr is null and coalesce(d.invoice_no, d.doc_ref) is not null;
delete from public.pa_instamart t using pa_dup d where t.row_hash = d.row_hash;
update public.pa_instamart t set payment_amount = s.total
  from (select advice_ref, round(sum(net_amount), 2) as total from public.pa_instamart
         where advice_ref in (select advice_ref from pa_dup) group by 1) s
 where t.advice_ref = s.advice_ref;
update public.pa_mail_log g
   set status = case when exists (select 1 from public.pa_instamart t where t.advice_ref = x.advice_ref)
                     then g.status else 'duplicate' end,
       detail = concat_ws('; ', nullif(g.detail, ''),
                          x.n || ' payment row(s) dropped, also under ' || x.keepers)
  from (select advice_ref, count(*) n, string_agg(distinct keeper, ', ') keepers from pa_dup group by 1) x
 where g.feed in ('instamart_vcredits', 'instamart') and g.advice_ref = x.advice_ref
   and g.status in ('loaded', 'header_only', 'loaded_round_off', 'loaded_no_total');
select count(*), count(distinct advice_ref) from pa_dup;
"""


def drop_unkeyed_duplicates(conn) -> tuple:
    """Remove VCredits payment rows that are copies of a row under another advice_ref; returns (rows, advices)."""
    with conn.cursor() as cur:
        cur.execute(UNKEYED_DUPES_SQL)      # one string = one implicit transaction; returns the last select
        rows, advices = cur.fetchone()
        cur.execute("drop table pa_dup")
    return rows, advices


def log_run(conn, feed: str, started: dt.datetime, ended: dt.datetime, counts: Dict[str, int],
            details: dict) -> None:
    failed = sum(counts.get(k, 0) for k in FAILED)
    secs = (ended - started).total_seconds()
    with conn.cursor() as cur:
        cur.execute(
            """insert into public.workflow_logs (source, workflow, started_at, ended_at, duration_seconds,
                   duration_text, processed, total_items, failed, skipped, status, details)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (f"pa_{feed}", "Payment advice: mail to Supabase", started, ended, round(secs, 1),
             f"{int(secs // 60)}m {int(secs % 60)}s",
             sum(counts.get(k, 0) for k in LOADED), sum(counts.values()), failed,
             counts.get("skipped", 0) + counts.get("duplicate", 0),
             "Success" if failed == 0 else "Partial", json.dumps({"counts": counts, **details})))
    conn.commit()
