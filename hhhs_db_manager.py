"""hhhs_db_manager — 한일합섬 ERP(NEOE, MS SQL Server) 읽기 전용 조회 도구.

    from hhhs_db_manager import get_table, get_list_tables, get_columns, query

    get_list_tables("SA_SO")                     # 이름에 SA_SO 가 들어간 테이블 + 행수
    get_columns("SA_SOH")                        # 컬럼 · 한글명 · 자료형 · PK
    get_table("SA_SOH", limit=20)                # SELECT TOP 20 *
    get_table("SA_SOL", columns=["NO_SO", "CD_ITEM", "QT_SO"],
              where="DT_DUEDATE >= :d", d="20260901", order_by="DT_DUEDATE DESC")
    query("SELECT TOP 5 * FROM NEOE.MA_ITEM WHERE CD_ITEM = :cd", cd="ELEXNF 180C")

셸:
    hhhs-db check                                # 접속 · 권한 · 설정 자가점검
    hhhs-db tables SA_SO                         # 테이블 찾기
    hhhs-db columns SA_SOH
    hhhs-db table SA_SOH -n 20 -w "DT_SO >= '20260901'"
    hhhs-db query "SELECT TOP 5 * FROM NEOE.MA_ITEM"
    hhhs-db query - <<'SQL'                      # 여러 줄은 stdin
    SELECT ...
    SQL
    hhhs-db --csv table SA_SOH -n 1000 > so.csv  # 파일 저장

접속정보는 .env 파일(HHHS_ENV_FILE → ./.env → 이 파일과 같은 폴더의 .env 순)에서 읽는다.
    DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME

DB 부하 방지 장치(자세한 설명은 README):
    SELECT/WITH 만 실행 · 결과 행 상한(HHHS_MAX_ROWS, 기본 10,000)에서 수신 중단 ·
    쿼리 제한시간(HHHS_QUERY_TIMEOUT, 기본 60초) · 프로세스당 동시 쿼리 1개 · 연결 1개 ·
    느린 쿼리 뒤 자동 휴지 · 100만 행 이상 테이블에 조건 없는 조회 차단 ·
    READ UNCOMMITTED + LOCK_TIMEOUT 으로 ERP 사용자 트랜잭션을 막지 않음
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    "get_table", "get_list_tables", "get_columns", "query", "check", "engine", "main",
    "DBError", "ConfigError", "QueryRejected", "QueryTimeout", "PermissionDenied",
]

import logging
import os
import re
import sys
import threading
import time
import warnings
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from dotenv import load_dotenv

log = logging.getLogger("hhhs_db_manager")

# ---------------------------------------------------------------------------
# TLS — 서버(SQL Server 2016)의 낡은 인증서를 OpenSSL 3 기본 정책이 거부한다.
# 이 프로세스에만 기준을 낮춘다. pymssql 이 import 되기 전에 설정돼야 한다.
# ---------------------------------------------------------------------------
_OPENSSL_LEGACY_CNF = """\
openssl_conf = openssl_init
[openssl_init]
ssl_conf = ssl_sect
[ssl_sect]
system_default = system_default_sect
[system_default_sect]
CipherString = DEFAULT:@SECLEVEL=0
Options = UnsafeLegacyRenegotiation
MinProtocol = TLSv1
"""


def _setup_tls() -> None:
    os.environ.setdefault("TDSVER", "7.4")
    if os.environ.get("OPENSSL_CONF"):
        return
    cnf = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hhhs_db_manager" / "openssl-legacy.cnf"
    if not cnf.exists():
        cnf.parent.mkdir(parents=True, exist_ok=True)
        cnf.write_text(_OPENSSL_LEGACY_CNF)
    os.environ["OPENSSL_CONF"] = str(cnf)


_setup_tls()

from sqlalchemy import create_engine, event, text  # noqa: E402  (TLS 설정 뒤에 import)
from sqlalchemy.exc import DBAPIError  # noqa: E402

# ---------------------------------------------------------------------------
# 설정 (환경변수로 조정)
# ---------------------------------------------------------------------------
SCHEMA = os.getenv("HHHS_SCHEMA", "NEOE")                       # 기본 스키마
QUERY_TIMEOUT = int(os.getenv("HHHS_QUERY_TIMEOUT", "60"))      # 쿼리 1건 제한시간(초)
MAX_ROWS = int(os.getenv("HHHS_MAX_ROWS", "10000"))             # 결과 행 상한 (0 = 무제한)
COOLDOWN = os.getenv("HHHS_COOLDOWN", "1") != "0"               # 느린 쿼리 뒤 자동 휴지
HEAVY_ROWS = int(os.getenv("HHHS_HEAVY_ROWS", "1000000"))       # 이 행수 이상이면 '큰 테이블'
LOCK_TIMEOUT_MS = 5000                                           # 잠금 대기 상한

_HERE = Path(__file__).resolve().parent
_lock = threading.Lock()                                         # 프로세스당 동시 쿼리 1개
_state = {"last_elapsed": 0.0, "last_end": 0.0, "count": 0}


class DBError(Exception):
    """이 모듈이 내는 모든 오류의 부모."""


class ConfigError(DBError):
    """접속정보(.env) 문제."""


class QueryRejected(DBError):
    """실행 전에 거부한 SQL — SELECT 가 아니거나 서버에 부담이 큰 형태."""


class QueryTimeout(DBError):
    """제한시간 안에 끝나지 않아 서버에서 중단됨."""


class PermissionDenied(DBError):
    """테이블 SELECT 권한 없음."""


# ---------------------------------------------------------------------------
# 접속
# ---------------------------------------------------------------------------
def _load_env() -> Path | None:
    explicit = os.getenv("HHHS_ENV_FILE")
    if explicit and not Path(explicit).is_file():
        raise ConfigError("HHHS_ENV_FILE 경로에 파일이 없습니다. 경로를 확인하세요.")
    for cand in (explicit, Path.cwd() / ".env", _HERE / ".env"):
        if cand and Path(cand).is_file():
            load_dotenv(cand, override=False)   # 이미 있는 환경변수가 우선
            return Path(cand)
    return None


@lru_cache(maxsize=1)
def engine():
    """SQLAlchemy 엔진. 처음 호출할 때 한 번 만들고 재사용한다 (연결 1개)."""
    env_file = _load_env()
    cfg = {k: os.getenv("DB_" + k) for k in ("HOST", "PORT", "USER", "PASSWORD", "NAME")}
    missing = ["DB_" + k for k, v in cfg.items() if not v]
    if missing:
        where = f" (읽은 파일: {env_file})" if env_file else " (.env 파일을 찾지 못함)"
        raise ConfigError(
            f"접속정보가 비어 있습니다: {missing}{where}. "
            "HHHS_ENV_FILE, ./.env, 모듈 폴더의 .env 중 하나에 넣거나 환경변수로 지정하세요."
        )
    url = (
        f"mssql+pymssql://{quote_plus(cfg['USER'])}:{quote_plus(cfg['PASSWORD'])}"
        f"@{cfg['HOST']}:{cfg['PORT']}/{cfg['NAME']}"
    )
    eng = create_engine(
        url,
        pool_size=1, max_overflow=0,          # 프로세스당 서버 연결 1개
        pool_pre_ping=True, pool_recycle=1800,
        connect_args={
            "timeout": QUERY_TIMEOUT,         # pymssql: 쿼리 실행 제한(초)
            "login_timeout": 10,
            "appname": f"hhhs_db_manager/{__version__}",   # 서버 세션 목록에서 식별 가능
        },
    )

    @event.listens_for(eng, "connect")
    def _on_connect(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        # 읽기 전용 분석이 ERP 사용자 트랜잭션을 기다리게 하거나 막지 않도록
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        cur.execute(f"SET LOCK_TIMEOUT {LOCK_TIMEOUT_MS}")
        cur.close()

    return eng


# ---------------------------------------------------------------------------
# 쿼리 실행 — 부하 장치는 전부 여기를 지난다
# ---------------------------------------------------------------------------
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+(?:\[?\w+\]?\.)?\[?(\w+)\]?", re.I)
_LIGHT_RE = re.compile(r"\bTOP\b|\bWHERE\b|\bGROUP\s+BY\b|\b(?:COUNT|SUM|MIN|MAX|AVG)\s*\(", re.I)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _sql_code(sql: str) -> str:
    """검사용 SQL: 문자열·인용 식별자·주석은 공백 처리 (중첩 주석 포함)."""
    out = []
    i = 0
    while i < len(sql):
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end
            out.append(" ")
        elif sql.startswith("/*", i):
            depth = 1
            i += 2
            while i < len(sql) and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise QueryRejected("닫히지 않은 SQL 주석입니다.")
            out.append(" ")
        elif sql[i] in "'\"[":
            endchar = "]" if sql[i] == "[" else sql[i]
            i += 1
            while i < len(sql):
                if sql[i] == endchar:
                    i += 1
                    if i < len(sql) and sql[i] == endchar:
                        i += 1
                        continue
                    break
                i += 1
            else:
                raise QueryRejected("닫히지 않은 SQL 문자열 또는 식별자입니다.")
            out.append(" quoted_token ")
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _validate_read_only(sql: str) -> None:
    code = _sql_code(sql).strip()
    if not re.match(r"^(SELECT|WITH)\b", code, re.I):
        raise QueryRejected("SELECT / WITH 로 시작하는 조회만 실행합니다.")
    if ";" in (code[:-1] if code.endswith(";") else code):
        raise QueryRejected("SQL 문장은 한 번에 하나만 실행하세요.")
    if re.search(r"\bNEXT\s+VALUE\s+FOR\b", code, re.I):
        raise QueryRejected("시퀀스 값을 변경하는 조회는 허용하지 않습니다.")
    if re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|INTO|EXEC|EXECUTE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|DENY|BACKUP|RESTORE|DBCC|WAITFOR|USE|SET|OPENROWSET|OPENQUERY|OPENDATASOURCE)\b", code, re.I):
        raise QueryRejected("읽기 전용 SELECT만 허용합니다. 쓰기·실행·외부 접근 구문은 사용할 수 없습니다.")


def _row_limit(value, name: str, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise QueryRejected(f"{name} 은(는) {'0 이상의' if allow_zero else '양의'} 정수여야 합니다.")
    return value


def _reject_if_heavy(sql: str) -> None:
    """100만 행 이상 테이블을 TOP·WHERE·집계 없이 읽는 SQL 은 실행 전에 막는다."""
    if _LIGHT_RE.search(_sql_code(sql)):
        return
    big = set(get_list_tables(min_rows=HEAVY_ROWS)["table"].str.upper())
    hit = sorted({t for t in _TABLE_RE.findall(sql) if t.upper() in big})
    if hit:
        raise QueryRejected(
            f"{hit} 은(는) {HEAVY_ROWS:,}행 이상인데 TOP · WHERE · 집계가 없습니다. "
            "조건을 넣거나, 정말 전체가 필요하면 query(..., allow_heavy=True) 로 실행하세요."
        )


def _cooldown() -> None:
    """직전 쿼리가 느렸으면 그만큼 쉬고 시작한다 (20%, 최대 5초). 연속 호출 최소 간격 0.1초."""
    if not COOLDOWN:
        return
    since = time.time() - _state["last_end"]
    wait = max(0.1, min(_state["last_elapsed"] * 0.2, 5.0)) - since
    if wait > 0:
        time.sleep(wait)


def _short(e: BaseException) -> str:
    msg = str(getattr(e, "orig", None) or e)
    return re.sub(r"\s+", " ", msg)[:300]


def _is_transient(e: BaseException) -> bool:
    msg = _short(e)
    return any(k in msg for k in ("20006", "20047", "20009", "08S01", "reset by peer", "DBPROCESS is dead"))


def _translate(e: BaseException, sql: str) -> DBError:
    msg = _short(e)
    low = msg.lower()
    if "timed out" in low or "20003" in msg:
        engine().dispose()   # 타임아웃 뒤 연결은 못 쓰므로 버린다
        return QueryTimeout(
            f"{QUERY_TIMEOUT}초 안에 끝나지 않아 서버에서 중단했습니다. TOP · 기간 조건으로 범위를 줄이세요. "
            "(정말 오래 걸리는 집계면 환경변수 HHHS_QUERY_TIMEOUT 을 올려서 실행)"
        )
    if "permission was denied" in low:
        return PermissionDenied("SELECT 권한이 없는 테이블입니다. 테이블 이름을 적어 권한 추가를 요청하세요.")
    m = re.search(r"Invalid object name '([^']+)'", msg)
    if m:
        return DBError(f"테이블이 없습니다: {m.group(1)} — get_list_tables('{m.group(1).split('.')[-1][:6]}') 로 이름을 확인하세요.")
    m = re.search(r"Invalid column name '([^']+)'", msg)
    if m:
        return DBError(f"컬럼이 없습니다: {m.group(1)} — get_columns(테이블) 로 확인하세요.")
    if "20017" in msg or "unexpected eof" in low:
        return DBError("TLS 협상 실패. 이 모듈보다 pymssql 을 먼저 import 했으면 새 프로세스(노트북이면 커널 재시작)에서 hhhs_db_manager 를 먼저 import 하세요.")
    if "18456" in msg or "login failed" in low:
        return ConfigError(".env 의 계정 · 비밀번호가 거부됐습니다.")
    if "20009" in msg or "unable to connect" in low:
        return DBError("서버에 닿지 못했습니다. 네트워크 · 방화벽(IP 허용목록)을 확인하세요.")
    if "lock request time out" in low or "1222" in msg:
        return DBError(f"다른 작업이 잠근 행을 {LOCK_TIMEOUT_MS // 1000}초 기다리다 포기했습니다. 잠시 후 다시 시도하세요.")
    return DBError("DB 조회에 실패했습니다. SQL 구문·바인딩 값·연결 상태를 확인하세요. 원본 오류는 접속정보 보호를 위해 표시하지 않습니다.")


def _run(sql: str, params: dict, max_rows: int) -> pd.DataFrame:
    for attempt in (1, 2):
        try:
            with engine().connect() as conn:
                res = conn.execute(text(sql), params)
                rows = res.fetchmany(max_rows + 1) if max_rows else res.fetchall()
                cols = list(res.keys())
                res.close()                      # 결과 닫기. 서버 스캔량·드라이버 버퍼링 상한은 아님
            break
        except DBAPIError as e:
            if attempt == 1 and _is_transient(e):
                log.warning("연결이 끊겨 2초 후 재시도합니다.")
                engine().dispose()
                time.sleep(2)
                continue
            raise
    truncated = bool(max_rows) and len(rows) > max_rows
    df = pd.DataFrame(rows[:max_rows] if truncated else rows, columns=cols)
    df.attrs["truncated"] = truncated
    if truncated:
        warnings.warn(
            f"결과가 {max_rows:,}행에서 잘렸습니다. 조건을 좁히거나 max_rows= 를 올리세요.",
            stacklevel=4,
        )
    return df


def query(sql: str, params: dict | None = None, *, max_rows: int | None = None,
          allow_heavy: bool = False, **kw) -> pd.DataFrame:
    """SELECT 를 실행해 DataFrame 으로 돌려준다.

    값은 :이름 바인딩으로 넘긴다 — query("... WHERE CD_ITEM = :cd", cd="ABC") 또는 params={"cd": "ABC"}.
    max_rows   결과 행 상한 (기본 HHHS_MAX_ROWS=10,000, 0 이면 무제한). 넘으면 잘라내고 경고, df.attrs["truncated"]=True.
    allow_heavy 100만 행 이상 테이블을 조건 없이 읽는 것을 허용.
    """
    params = {**(params or {}), **kw}
    max_rows = _row_limit(MAX_ROWS if max_rows is None else max_rows, "max_rows")
    _validate_read_only(sql)
    if not allow_heavy:
        _reject_if_heavy(sql)
    with _lock:
        _cooldown()
        t0 = time.time()
        try:
            df = _run(sql, params, max_rows)
        except DBAPIError as e:
            raise _translate(e, sql) from None
        finally:
            _state["last_elapsed"] = time.time() - t0
            _state["last_end"] = time.time()
            _state["count"] += 1
    log.info("query %.2fs rows=%d%s", _state["last_elapsed"], len(df), " (truncated)" if df.attrs.get("truncated") else "")
    return df


# ---------------------------------------------------------------------------
# 편의 함수
# ---------------------------------------------------------------------------
def _split_name(table_name: str) -> tuple[str, str]:
    parts = table_name.strip().replace("[", "").replace("]", "").split(".")
    if len(parts) == 1:
        schema, tbl = SCHEMA, parts[0]
    elif len(parts) == 2:
        schema, tbl = parts
    else:
        raise QueryRejected(f"테이블 이름 형식이 잘못됐습니다: {table_name!r} (예: SA_SOH 또는 NEOE.SA_SOH)")
    for p in (schema, tbl):
        if not _IDENT_RE.match(p):
            raise QueryRejected(f"허용되지 않는 이름입니다: {p!r}")
    return schema, tbl


@lru_cache(maxsize=1)
def _catalog() -> pd.DataFrame:
    # sys.partitions 의 행수는 통계값이라 테이블을 읽지 않는다 — 4,000개를 세도 1초 미만
    return query(
        """
        SELECT s.name AS [schema], o.name AS [table], CASE o.type WHEN 'V' THEN 'view' ELSE 'table' END AS [type],
               SUM(p.rows) AS [rows],
               (SELECT COUNT(*) FROM sys.columns c WHERE c.object_id = o.object_id) AS [columns],
               CONVERT(char(10), o.modify_date, 120) AS modified
        FROM sys.objects o
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        LEFT JOIN sys.partitions p ON p.object_id = o.object_id AND p.index_id IN (0, 1)
        WHERE o.type IN ('U', 'V')
        GROUP BY s.name, o.name, o.type, o.object_id, o.modify_date
        ORDER BY s.name, o.name
        """,
        max_rows=0, allow_heavy=True,
    )


def get_list_tables(like: str | None = None, *, min_rows: int | None = None,
                    schema: str | None = None, refresh: bool = False) -> pd.DataFrame:
    """테이블 · 뷰 목록 (schema, table, type, rows, columns, modified).

    like      이름에 포함된 문자열 (대소문자 무시).  예: get_list_tables("QTIO")
    min_rows  이 행수 이상만.  빈 테이블을 빼려면 min_rows=1
    schema    스키마로 제한.  기본은 전체
    refresh   캐시를 버리고 서버에서 다시 읽음 (처음 한 번만 서버를 읽고 이후 재사용)
    """
    if refresh:
        _catalog.cache_clear()
    df = _catalog()
    if schema:
        df = df[df["schema"].str.upper() == schema.upper()]
    if like:
        df = df[df["table"].str.contains(like, case=False, regex=False)]
    if min_rows is not None:
        df = df[df["rows"] >= min_rows]
    return df.reset_index(drop=True)


def get_columns(table_name: str) -> pd.DataFrame:
    """컬럼 목록 (column, name_kr, type, nullable, pk). 한글명은 ERP 사전(CM_DICTION)에서 붙인다."""
    schema, tbl = _split_name(table_name)
    base = """
        SELECT c.COLUMN_NAME AS [column],
               {name_kr}
               c.DATA_TYPE + CASE WHEN c.CHARACTER_MAXIMUM_LENGTH IS NOT NULL
                                  THEN '(' + CASE WHEN c.CHARACTER_MAXIMUM_LENGTH = -1 THEN 'max'
                                                  ELSE CAST(c.CHARACTER_MAXIMUM_LENGTH AS varchar) END + ')'
                                  WHEN c.NUMERIC_PRECISION IS NOT NULL AND c.DATA_TYPE IN ('numeric', 'decimal')
                                  THEN '(' + CAST(c.NUMERIC_PRECISION AS varchar) + ',' + CAST(c.NUMERIC_SCALE AS varchar) + ')'
                                  ELSE '' END AS [type],
               c.IS_NULLABLE AS nullable,
               k.ORDINAL_POSITION AS pk
        FROM INFORMATION_SCHEMA.COLUMNS c
        LEFT JOIN (SELECT kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.COLUMN_NAME, kcu.ORDINAL_POSITION
                   FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                   JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                     ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                     AND tc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
                     AND tc.TABLE_NAME = kcu.TABLE_NAME AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY') k
          ON k.TABLE_SCHEMA = c.TABLE_SCHEMA AND k.TABLE_NAME = c.TABLE_NAME AND k.COLUMN_NAME = c.COLUMN_NAME
        {dict_join}
        WHERE c.TABLE_SCHEMA = :s AND c.TABLE_NAME = :t
        ORDER BY c.ORDINAL_POSITION
    """
    with_dict = dict(
        name_kr="d.NM_KR AS name_kr,",
        dict_join=f"LEFT JOIN (SELECT CD_SYSTEM, MIN(NM_KR) AS NM_KR FROM [{SCHEMA}].CM_DICTION GROUP BY CD_SYSTEM) d ON d.CD_SYSTEM = c.COLUMN_NAME",
    )
    try:
        df = query(base.format(**with_dict), s=schema, t=tbl, allow_heavy=True)
    except DBError as exc:
        if isinstance(exc, (QueryTimeout, ConfigError)):
            raise
        df = query(base.format(name_kr="CAST(NULL AS nvarchar(200)) AS name_kr,", dict_join=""), s=schema, t=tbl, allow_heavy=True)
    if df.empty:
        raise DBError(f"테이블이 없습니다: {schema}.{tbl} — get_list_tables('{tbl[:6]}') 로 이름을 확인하세요.")
    df["pk"] = df["pk"].astype("Int64")   # 1, 2, … / <NA>
    return df


def get_table(table_name: str, limit: int | None = 100, *, columns: list[str] | None = None,
              where: str | None = None, order_by: str | None = None,
              max_rows: int | None = None, **params) -> pd.DataFrame:
    """테이블을 읽는다.  SELECT TOP {limit} {columns} FROM {table} WHERE {where} ORDER BY {order_by}

    limit     기본 100. None 이면 TOP 없이 — 큰 테이블은 where 가 없으면 거부된다
    columns   ["NO_SO", "DT_SO"] 처럼 컬럼 이름 목록. 없으면 *
    where     SQL 조건문. 값은 :이름 으로 두고 키워드 인자로 넘긴다 — where="DT_SO >= :d", d="20260901"
    order_by  "DT_SO DESC" 처럼
    """
    schema, tbl = _split_name(table_name)
    if columns:
        bad = [c for c in columns if not _IDENT_RE.match(c)]
        if bad:
            raise QueryRejected(f"허용되지 않는 컬럼명: {bad}")
        cols = ", ".join(f"[{c}]" for c in columns)
    else:
        cols = "*"
    if limit is not None:
        _row_limit(limit, "limit", allow_zero=False)
    sql = f"SELECT {'TOP (' + str(int(limit)) + ') ' if limit else ''}{cols} FROM [{schema}].[{tbl}]"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        if not re.fullmatch(r"[\w\[\]\s,]+", order_by):
            raise QueryRejected(f"order_by 는 컬럼명과 ASC/DESC 만 허용합니다: {order_by!r}")
        sql += f" ORDER BY {order_by}"
    return query(sql, params, max_rows=max_rows)


def check(verbose: bool = True) -> dict:
    """접속 · 권한 · 설정 자가점검. 결과를 dict 로 돌려주고 verbose 면 출력한다."""
    info = query("SELECT @@VERSION AS v, DB_NAME() AS db, SYSTEM_USER AS usr, @@SPID AS spid", allow_heavy=True).iloc[0]
    cat = get_list_tables(refresh=True)
    can_write = query(
        f"""
        SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = '{SCHEMA}' AND (
              HAS_PERMS_BY_NAME(QUOTENAME(TABLE_SCHEMA) + '.' + QUOTENAME(TABLE_NAME), 'OBJECT', 'INSERT') = 1
           OR HAS_PERMS_BY_NAME(QUOTENAME(TABLE_SCHEMA) + '.' + QUOTENAME(TABLE_NAME), 'OBJECT', 'UPDATE') = 1
           OR HAS_PERMS_BY_NAME(QUOTENAME(TABLE_SCHEMA) + '.' + QUOTENAME(TABLE_NAME), 'OBJECT', 'DELETE') = 1)
        """, allow_heavy=True,
    ).iloc[0, 0]
    result = {
        "server": info.v.splitlines()[0].strip(),
        "database": info.db, "user": info.usr, "session_id": int(info.spid),
        "tables": int(len(cat)), "tables_with_data": int((cat["rows"] > 0).sum()),
        "writable_tables_in_schema": int(can_write),
        "settings": {
            "schema": SCHEMA, "query_timeout_s": QUERY_TIMEOUT, "max_rows": MAX_ROWS,
            "cooldown": COOLDOWN, "heavy_rows": HEAVY_ROWS, "lock_timeout_ms": LOCK_TIMEOUT_MS,
            "isolation": "READ UNCOMMITTED", "pool_size": 1,
        },
    }
    if verbose:
        print(f"서버      : {result['server']}")
        print(f"DB / 계정 : {result['database']} / {result['user']}  (세션 {result['session_id']})")
        print(f"테이블    : {result['tables']:,}개, 데이터 있는 테이블 {result['tables_with_data']:,}개")
        print(f"쓰기 권한 : {SCHEMA} 스키마에서 {can_write}개 테이블" + ("  ✅ 읽기 전용" if can_write == 0 else "  ⚠️ 쓰기 가능한 테이블이 있습니다"))
        s = result["settings"]
        print(f"부하 장치 : 제한시간 {s['query_timeout_s']}초 · 행 상한 {s['max_rows']:,} · 동시 1개 · 휴지 {'on' if s['cooldown'] else 'off'}"
              f" · 큰 테이블 기준 {s['heavy_rows']:,}행 · {s['isolation']} · LOCK_TIMEOUT {s['lock_timeout_ms']}ms")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="hhhs-db", description="한일합섬 ERP DB 읽기 전용 조회")
    p.add_argument("--csv", action="store_true", help="CSV 로 출력 (파일 저장: > out.csv)")
    p.add_argument("--max-rows", type=int, default=None, help=f"결과 행 상한 (기본 {MAX_ROWS:,}, 0=무제한)")
    p.add_argument("-v", "--verbose", action="store_true", help="실행 로그 출력")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="접속 · 권한 · 설정 자가점검")
    s = sub.add_parser("tables", help="테이블 목록 (+행수). 예: hhhs-db tables SA_SO")
    s.add_argument("like", nargs="?", help="이름에 포함된 문자열")
    s.add_argument("--min-rows", type=int, default=1, help="이 행수 이상만 (기본 1 = 빈 테이블 제외, 0 = 전체)")
    s.add_argument("--schema", default=None)
    s = sub.add_parser("columns", help="컬럼 목록. 예: hhhs-db columns SA_SOH")
    s.add_argument("table")
    s = sub.add_parser("table", help="테이블 미리보기. 예: hhhs-db table SA_SOH -n 20 -w \"DT_SO >= '20260901'\"")
    s.add_argument("table")
    s.add_argument("-n", "--limit", type=int, default=20, help="TOP N (기본 20, 0 = 제한 없음)")
    s.add_argument("-c", "--columns", help="쉼표로 구분한 컬럼명")
    s.add_argument("-w", "--where", help="SQL 조건문")
    s.add_argument("-o", "--order-by", help="정렬. 예: 'DT_SO DESC'")
    s = sub.add_parser("query", help="SELECT 실행. SQL 문자열 또는 '-' (stdin)")
    s.add_argument("sql")
    s.add_argument("--allow-heavy", action="store_true", help="큰 테이블 전체 조회 허용")
    a = p.parse_args(argv)

    if a.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if a.cmd == "check":
            check()
            return 0
        if a.cmd == "tables":
            df = get_list_tables(a.like, min_rows=a.min_rows, schema=a.schema)
        elif a.cmd == "columns":
            df = get_columns(a.table)
        elif a.cmd == "table":
            cols = [c.strip() for c in a.columns.split(",")] if a.columns else None
            df = get_table(a.table, a.limit or None, columns=cols, where=a.where, order_by=a.order_by, max_rows=a.max_rows)
        else:
            sql = sys.stdin.read() if a.sql == "-" else a.sql
            df = query(sql, max_rows=a.max_rows, allow_heavy=a.allow_heavy)
    except DBError as e:
        print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if a.csv:
        df.to_csv(sys.stdout, index=False)
    else:
        with pd.option_context("display.max_colwidth", 60, "display.width", 250):
            print(df.to_string(index=False, max_rows=200))
        note = " · 상한에서 잘림" if df.attrs.get("truncated") else (" · 200행까지만 표시" if len(df) > 200 else "")
        print(f"\n({len(df):,} rows{note}, {_state['last_elapsed']:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
