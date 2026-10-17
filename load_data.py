#!/usr/bin/env python3
"""
Download + load BGL and OpenStack into SereneDB (idempotent: drops and recreates tables).

Prereqs: SereneDB running on localhost:7890 with ./data mounted at /data inside the container
(see README). Run:  python load_data.py
"""
import csv
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SERVER_DIR = "/data"  # where ./data is mounted INSIDE the SereneDB container

CONN = {"host": os.getenv("SERENEDB_HOST", "localhost"),
        "port": int(os.getenv("SERENEDB_PORT", "7890"))}
if os.getenv("SERENEDB_USER"):
    CONN["user"] = os.environ["SERENEDB_USER"]
if os.getenv("SERENEDB_DB"):
    CONN["dbname"] = os.environ["SERENEDB_DB"]

ZENODO = "https://zenodo.org/records/8196385/files/{}?download=1"
OS_FILES = ["openstack_normal1.log", "openstack_normal2.log", "openstack_abnormal.log"]
BGL_FILE = "BGL.log"
EXPECTED = {"openstack": 207_820, "bgl": 4_747_963}

READ_OPTS = (r"header=false, sep='\n', quote='', escape='', auto_detect=false, "
             r"strict_mode=false, ignore_errors=true, columns={'column0': 'VARCHAR'}")


def die(msg):
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- download
def download(name):
    DATA.mkdir(exist_ok=True)
    dest = DATA / name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    print(f"Downloading {name} from Zenodo ...")
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(ZENODO.format(name), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req) as r, open(part, "wb") as f:  # follows redirects
            total = int(r.headers.get("Content-Length", 0))
            done = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done / 1e6:7.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print()
    except Exception as e:
        die(f"download of {name} failed: {e}\nDownload it manually into ./data/")
    part.rename(dest)
    return dest


def extract_and_collect(archive, wanted):
    """Extract archive to a temp dir and move the wanted files (found anywhere) into ./data."""
    with tempfile.TemporaryDirectory(dir=DATA) as tmp:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        else:
            with tarfile.open(archive) as t:
                try:
                    t.extractall(tmp, filter="data")
                except TypeError:  # older Python without the filter argument
                    t.extractall(tmp)
        for name in wanted:
            hits = list(Path(tmp).rglob(name))
            if not hits:
                die(f"{name} not found inside {archive.name}")
            shutil.move(str(hits[0]), DATA / name)


def ensure_data():
    if not all((DATA / f).exists() for f in OS_FILES):
        extract_and_collect(download("OpenStack.tar.gz"), OS_FILES)
    if not (DATA / BGL_FILE).exists():
        extract_and_collect(download("BGL.zip"), [BGL_FILE])
    missing = [f for f in OS_FILES + [BGL_FILE] if not (DATA / f).exists()]
    if missing:
        die(f"missing data files in ./data: {missing}")
    print("Data files OK:", ", ".join(f"{f} ({(DATA / f).stat().st_size / 1e6:.0f} MB)"
                                        for f in OS_FILES + [BGL_FILE]))


# ---------------------------------------------------------------- SQL helpers
def q(cur, sql):
    cur.execute(sql)
    return cur.fetchall() if cur.description else []


def show(cur, title, sql):
    rows = q(cur, sql)
    print(f"\n-- {title}")
    for r in rows:
        print("   ", " | ".join(str(c) for c in r))
    return rows


def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


# ---------------------------------------------------------------- schema + ownership
def create_schema(cur):
    print("\nCreating schema (dropping existing tables) ...")
    for t in ("openstack", "bgl", "team_ownership"):
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    cur.execute("""
        CREATE TABLE openstack (
            line_no BIGINT, src_file TEXT, ts TIMESTAMP,
            log_date TEXT, log_time TEXT, pid TEXT, level TEXT,
            component TEXT, module TEXT, content TEXT)""")
    cur.execute("""
        CREATE TABLE bgl (
            line_no BIGINT, ts TIMESTAMP, alert_flag TEXT, epoch BIGINT,
            date_str TEXT, node TEXT, time_str TEXT, node_repeat TEXT,
            log_type TEXT, component TEXT, level TEXT, content TEXT)""")
    cur.execute("""
        CREATE TABLE team_ownership (
            dataset TEXT, component TEXT, team_name TEXT, lead_contact TEXT)""")


def load_ownership(cur):
    path = ROOT / "ownership.csv"
    if not path.exists():
        die("ownership.csv not found in repo root")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    values = ", ".join(
        f"({lit(r['dataset'])}, {lit(r['component'])}, {lit(r['team_name'])}, {lit(r['lead_contact'])})"
        for r in rows)
    cur.execute(f"INSERT INTO team_ownership VALUES {values}")
    print(f"Loaded {len(rows)} ownership rows")


# ---------------------------------------------------------------- loading
def load_openstack(cur):
    for fname in OS_FILES:
        offset = q(cur, "SELECT COALESCE(MAX(line_no), 0) FROM openstack")[0][0]
        print(f"Loading {fname} (line_no offset {offset}) ...")
        # Line layout: [1]=logfile token  [2]=date  [3]=time  [4]=pid  [5]=LEVEL
        #              [6]=module         [7:]=content   (SQL arrays are 1-indexed)
        cur.execute(f"""
            INSERT INTO openstack
                (line_no, src_file, ts, log_date, log_time, pid, level, component, module, content)
            SELECT {offset} + row_number() OVER (),
                   {lit(fname)},
                   TRY_CAST(p[2] || ' ' || p[3] AS TIMESTAMP),
                   p[2], p[3], p[4], p[5],
                   str_split(p[1], '.log')[1],
                   p[6],
                   array_to_string(p[7:], ' ')
            FROM (SELECT str_split(column0, ' ') AS p
                  FROM read_csv('{SERVER_DIR}/{fname}', {READ_OPTS})
                  WHERE trim(column0) != '') s""")


def load_bgl(cur):
    print(f"Loading {BGL_FILE} (all rows, this takes a while) ...")
    # Line layout: [1]=alert label [2]=epoch [3]=date [4]=node [5]=time_str
    #              [6]=node repeat [7]=type [8]=component [9]=level [10:]=content
    # ts is built from time_str ('2005-06-03-15.42.50.675872') = local log time.
    cur.execute(f"""
        INSERT INTO bgl
            (line_no, ts, alert_flag, epoch, date_str, node, time_str,
             node_repeat, log_type, component, level, content)
        SELECT row_number() OVER (),
               TRY_CAST(substr(p[5], 1, 10) || ' ' || replace(substr(p[5], 12, 8), '.', ':')
                        || substr(p[5], 20) AS TIMESTAMP),
               p[1], TRY_CAST(p[2] AS BIGINT), p[3], p[4], p[5],
               p[6], p[7], p[8], p[9],
               array_to_string(p[10:], ' ')
        FROM (SELECT str_split(column0, ' ') AS p
              FROM read_csv('{SERVER_DIR}/{BGL_FILE}', {READ_OPTS})
              WHERE trim(column0) != '') s""")


# ---------------------------------------------------------------- verification
def verify(cur):
    print("\n=== Verification ===")
    ok = True
    for t, exp in EXPECTED.items():
        n = q(cur, f"SELECT count(*) FROM {t}")[0][0]
        flag = "OK" if n == exp else "MISMATCH"
        print(f"{t}: {n:,} rows (expected {exp:,}) -> {flag}")
        ok &= (n == exp)

    for t in ("openstack", "bgl"):
        show(cur, f"{t}: level distribution",
             f"SELECT level, count(*) FROM {t} GROUP BY level ORDER BY 2 DESC")
        show(cur, f"{t}: components and owning team",
             f"""SELECT l.component, COALESCE(o.team_name, '** UNMAPPED **'), count(*)
                 FROM {t} l LEFT JOIN team_ownership o
                   ON o.dataset = '{t}' AND o.component = l.component
                 GROUP BY l.component, o.team_name ORDER BY 3 DESC""")
        show(cur, f"{t}: time range and NULL timestamps",
             f"SELECT min(ts), max(ts), count(*) FILTER (WHERE ts IS NULL) FROM {t}")

    # Informational: BGL is chronological, so ts should rarely go backwards by line_no.
    show(cur, "bgl: rows whose ts is earlier than the previous row (should be ~0)",
         """SELECT count(*) FROM (
                SELECT ts, lag(ts) OVER (ORDER BY line_no) AS prev FROM bgl) x
            WHERE ts < prev""")
    return ok


def main():
    ensure_data()
    try:
        conn = psycopg.connect(autocommit=True, **CONN)
    except Exception as e:
        die(f"cannot connect to SereneDB at {CONN['host']}:{CONN['port']}: {e}\n"
            "Is the container running?  docker ps")
    with conn, conn.cursor() as cur:
        create_schema(cur)
        load_ownership(cur)
        try:
            load_openstack(cur)
            load_bgl(cur)
        except Exception as e:
            die(f"load failed: {e}\nCheck that ./data is mounted at {SERVER_DIR} in the "
                "container (docker exec serenedb ls /data) and see the troubleshooting notes.")
        if not verify(cur):
            die("row counts do not match the expected totals (see above)")
    print("\nDone. All row counts match.")


if __name__ == "__main__":
    main()