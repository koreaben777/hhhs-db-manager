# hhhs-db-manager

한일합섬 ERP 데이터베이스(더존 NEOE, MS SQL Server)를 **읽기 전용**으로 조회하는 파이썬 도구입니다.
팀원이 노트북·스크립트에서 쓰고, Claude Code·goose 같은 에이전트가 셸에서 그대로 부를 수 있게 만들었습니다.

```python
from hhhs_db_manager import get_table, get_list_tables, get_columns, query

get_list_tables("SA_SO")            # 이름에 SA_SO 가 들어간 테이블과 행수
get_columns("SA_SOH")               # 컬럼 · 한글명 · 자료형 · PK
get_table("SA_SOH", limit=20)       # 미리보기 (SELECT TOP 20 *)
query("SELECT TOP 5 * FROM NEOE.MA_ITEM WHERE CD_ITEM = :cd", cd="ELEXNF 180C")
```

```bash
hhhs-db check                                   # 접속 · 권한 · 설정 자가점검
hhhs-db tables SA_SO                            # 테이블 찾기
hhhs-db columns SA_SOH                          # 컬럼 보기
hhhs-db table SA_SOH -n 20 -w "DT_SO >= '20260901'"
hhhs-db query "SELECT TOP 5 * FROM NEOE.MA_ITEM"
```

- 모든 결과는 pandas `DataFrame` (CLI 는 표 또는 `--csv`)
- 계정도 도구도 **SELECT 만** 실행합니다
- 운영 ERP 에 부담을 주지 않도록 **행 상한 · 제한시간 · 동시 실행 1개 · 느린 쿼리 뒤 휴지 · 큰 테이블 조건 없는 조회 차단** 이 기본으로 켜져 있습니다 (→ [DB 부하 방지 장치](#db-부하-방지-장치))

---

## 목차

1. [설치](#설치)
2. [접속정보 설정](#접속정보-설정)
3. [빠른 시작](#빠른-시작)
4. [파이썬 API](#파이썬-api)
5. [CLI](#cli)
6. [DB 부하 방지 장치](#db-부하-방지-장치)
7. [오류가 나면](#오류가-나면)
8. [조회 팁](#조회-팁)
9. [에이전트에서 쓰기](#에이전트에서-쓰기)
10. [주의사항](#주의사항)
11. [개발](#개발)

---

## 설치

Python 3.10 이상. [uv](https://docs.astral.sh/uv/) 를 권장하지만 pip 도 됩니다.

**쓰기만 할 때** — 프로젝트 가상환경에 바로 설치

```bash
uv pip install "git+https://github.com/koreaben777/hhhs-db-manager"
# 또는
pip install "git+https://github.com/koreaben777/hhhs-db-manager"
```

**고치면서 쓸 때** — 저장소를 받아 편집 가능 모드로

```bash
git clone https://github.com/koreaben777/hhhs-db-manager
cd hhhs-db-manager
uv venv && uv pip install -e .
```

설치되면 파이썬 모듈 `hhhs_db_manager` 와 명령 `hhhs-db` 가 생깁니다.

> macOS 에서 `pymssql` 설치가 실패하면 `brew install freetds` 후 다시 시도하세요.

---

## 접속정보 설정

접속값은 저장소에 **없습니다**. 팀 담당자에게 받아 `.env` 파일로 둡니다.

```bash
cp .env.example .env     # 값을 채운다
```

```
DB_HOST=서버주소
DB_PORT=1433
DB_USER=계정
DB_PASSWORD=비밀번호
DB_NAME=NEOE
```

찾는 순서 — 먼저 발견되는 것 하나만 읽습니다.

| 순서 | 위치 | 언제 |
| --- | --- | --- |
| 1 | 환경변수 `HHHS_ENV_FILE` 이 가리키는 파일 | `.env` 를 다른 곳에 모아 둘 때 |
| 2 | 현재 작업 폴더의 `.env` | 노트북 · 스크립트 옆에 둘 때 |
| 3 | `hhhs_db_manager.py` 와 같은 폴더의 `.env` | 저장소를 clone 해서 쓸 때 |

이미 셸에 `DB_HOST` 같은 환경변수가 있으면 그 값이 `.env` 보다 우선합니다.

- `.env` 는 `.gitignore` 에 있어 커밋되지 않습니다. 메신저 · 이슈 · 노트북 출력에도 값을 붙여 넣지 마세요.
- 사내 macOS 에서는 `.env` 에 Finder `보안` 태그를 붙여 두면 에이전트가 파일을 열지 않습니다. 이 도구는 파일 내용을 출력하지 않고 접속에만 씁니다.

---

## 빠른 시작

### 노트북으로 (에이전트 없이 쓸 때)

`hhhs_db_manager.ipynb` 는 입력창으로 조건을 받아 조회하는 **DB 검색기** 형태입니다. 셀을 실행하면 테이블명 · 컬럼 · 조건 · 정렬 · 건수 등을 물어보고, 비워 두면 기본값으로 실행됩니다.

접속 확인 → 테이블 찾기 → 컬럼 보기(한글명) → 데이터 조회 → 값으로 찾기 → 값별 건수 → 자유 SQL → 코드값 뜻 → 저장 순서이며, 결과는 항상 `df` 에 남아 마지막 절에서 파일로 저장할 수 있습니다.

```bash
uv pip install -e ".[notebook]"     # 커널(ipykernel) 포함 설치. 이후 VS Code · Jupyter 에서 .venv 커널 선택
```

### 노트북 실행 환경과 사용 순서

저장소 폴더에서 설치합니다. `.env`가 이미 있으면 복사 명령으로 덮어쓰지 마세요.

```bash
uv venv --python 3.13                 # .venv가 없는 경우에만
uv pip install -e ".[notebook]"       # ipykernel + JupyterLab + Excel 저장 엔진
uv run jupyter lab hhhs_db_manager.ipynb
```

VS Code에서는 노트북 우측 상단 **커널 선택 → Python 환경 → 이 저장소의 .venv**를 선택하세요.
Jupyter에서 커널을 찾지 못하면 다음 명령으로 등록할 수 있습니다.

```bash
uv run python3 -m ipykernel install --user --name hhhs-db --display-name "Python (hhhs-db)"
```

1. **0절**을 먼저 실행합니다. 이후 필요한 절만 `Shift+Enter`로 실행하세요. `모두 실행`은 권장하지 않습니다.
2. **1절 테이블 → 2절 컬럼 → 3절 미리보기** 순서로 확인합니다. 입력창을 비우면 기본값이 사용됩니다.
3. **4절**은 입력값을 SQL 바인딩으로 전달합니다. **3·5절 WHERE와 6절 SQL은 직접 작성하는 SQL**이므로 검토 후 실행하세요.
4. 건수 입력은 1~10,000만 허용합니다. TOP은 결과 행수 제한이지 스캔 비용 제한이 아닙니다. **5절 분포 집계도 기간 조건을 권장**합니다.
5. 실패하거나 취소하면 `df`를 비워 이전 결과의 잘못된 저장을 방지합니다. **8절**은 파일명만 받고 기존 파일은 덮어쓰지 않습니다.
6. 결과는 출력한 저장 폴더(프로젝트 루트)에 보관합니다. `DB조회도구` 폴더에서 실행하면 상위 프로젝트 폴더입니다. 상위 폴더는 이 저장소의 `.gitignore` 적용 대상이 아니므로 다른 저장소에도 결과를 추가하지 마세요.
7. 공유 전에는 **모든 출력 지우기** 후 저장하세요. 입력한 SQL·값 자체도 민감정보가 없는지 별도로 확인합니다.

`notebook` 추가 의존성을 설치하지 않으면 `.xlsx` 저장에 필요한 `openpyxl`이 없을 수 있습니다.
접속 또는 TLS 설정 변경 후에는 커널을 재시작하세요. `HHHS_QUERY_TIMEOUT`, `HHHS_MAX_ROWS`,
`HHHS_SCHEMA` 등의 동작 설정은 **import 전에 프로세스 환경변수로 설정**해야 합니다.
현재 `.env` 로딩은 첫 연결 시 접속정보를 읽는 용도이므로 `.env`에 동작 설정을 추가하는 것만으로 적용되지 않습니다.
`HHHS_ENV_FILE`을 명시했는데 파일이 없으면 다른 `.env`로 넘어가지 않고 오류를 냅니다.

### 파이썬 (노트북)

```python
from hhhs_db_manager import get_table, get_list_tables, get_columns, query, check

check()                                  # 처음 한 번 — 접속 · 권한 · 설정 확인

get_list_tables("QTIO")                  # 이름에 QTIO 가 들어간 테이블
get_list_tables(min_rows=1_000_000)      # 100만 행 이상 테이블
get_columns("MM_QTIOLOT")                # 컬럼 · 한글명 · 자료형 · PK 순서

get_table("SA_SOH")                      # TOP 100
get_table("SA_SOL", limit=50,
          columns=["NO_SO", "SEQ_SO", "CD_ITEM", "QT_SO", "DT_DUEDATE"],
          where="DT_DUEDATE >= :d AND CD_COMPANY = :co", d="20260901", co="1000",
          order_by="DT_DUEDATE DESC")

df = query("""
    SELECT CD_ITEM, SUM(QT_IO) AS qty
    FROM NEOE.MM_QTIOLOT
    WHERE DT_IO >= :d AND FG_IO = '010'
    GROUP BY CD_ITEM ORDER BY qty DESC
""", d="20260801")
df.to_excel("판매출고_8월.xlsx", index=False)   # openpyxl 필요
```

### 셸

```bash
hhhs-db check
hhhs-db tables SO                               # 빈 테이블은 기본으로 숨김 (--min-rows 0 이면 전체)
hhhs-db columns SA_SOH
hhhs-db table SA_SOH -n 5
hhhs-db table SA_SOL -n 50 -c NO_SO,CD_ITEM,QT_SO -w "DT_DUEDATE >= '20260901'" -o "DT_DUEDATE DESC"
hhhs-db query "SELECT COUNT(*) FROM NEOE.SA_SOH WHERE DT_SO >= '20260101'"
hhhs-db query - <<'SQL'
SELECT TOP 10 NO_SO, DT_SO, CD_PARTNER
FROM NEOE.SA_SOH
ORDER BY DT_SO DESC
SQL
hhhs-db --csv table SA_SOH -n 1000 > 수주_1000건.csv
```

---

## 파이썬 API

조회 함수는 `pandas.DataFrame` 을 돌려주고 (`check`는 dict),, 실패하면 `DBError` 계열 예외를 냅니다.

### `get_list_tables(like=None, *, min_rows=None, schema=None, refresh=False)`

테이블 · 뷰 목록. 열: `schema, table, type, rows, columns, modified`.

| 인자 | 설명 |
| --- | --- |
| `like` | 이름에 포함된 문자열, 대소문자 무시. `"SO"` → `SA_SOH`, `SA_SOL`, … |
| `min_rows` | 이 행수 이상만. 빈 테이블을 빼려면 `1` |
| `schema` | `"NEOE"` 처럼 스키마 제한 |
| `refresh` | 서버에서 다시 읽음. 목록은 프로세스 안에서 한 번만 읽고 재사용 |

행수는 서버 통계(`sys.partitions`)라 테이블을 읽지 않고 즉시 나오며, 정확한 `COUNT(*)` 와 차이가 날 수 있습니다. 일반 뷰의 행수는 NULL일 수 있어 `min_rows=1` 검색에서는 숨겨집니다.

### `get_columns(table_name)`

컬럼 목록. 열: `column, name_kr, type, nullable, pk`. `pk` 는 기본키 안의 순서(없으면 빈 값).
`name_kr` 은 ERP 안의 사전 테이블(`CM_DICTION`)에서 붙이며, 사전에 없는 컬럼은 비어 있습니다.

### `get_table(table_name, limit=100, *, columns=None, where=None, order_by=None, max_rows=None, **params)`

`SELECT TOP {limit} {columns} FROM {table} WHERE {where} ORDER BY {order_by}` 를 만들어 실행합니다.

| 인자 | 설명 |
| --- | --- |
| `table_name` | `"SA_SOH"` 또는 `"NEOE.SA_SOH"`. 스키마를 생략하면 `NEOE` |
| `limit` | 양의 정수, 기본 100. `None` 이면 TOP 없음. Python에서 0·음수는 거부합니다 (CLI `-n 0`은 TOP 없음으로 변환) |
| `max_rows` | 수신 상한. 기본 10,000이며 `limit`을 크게 해도 자동으로 증가하지 않습니다 |
| `columns` | 컬럼 이름 목록. 생략하면 `*` |
| `where` | SQL 조건문. 값은 `:이름` 으로 쓰고 키워드 인자로 넘깁니다 |
| `order_by` | `"DT_SO DESC"` 처럼. 컬럼명과 ASC/DESC 만 허용 |

```python
get_table("MA_ITEM", 10, columns=["CD_ITEM", "NM_ITEM"], where="NM_ITEM LIKE :kw", kw="%POLY%")
```

### `query(sql, params=None, *, max_rows=None, allow_heavy=False, **kw)`

SELECT 를 그대로 실행합니다. 값은 `:이름` 바인딩으로 — 문자열을 f-string 으로 붙이지 마세요.

| 인자 | 설명 |
| --- | --- |
| `max_rows` | 결과 행 상한. 기본 10,000 (`HHHS_MAX_ROWS`). 넘으면 잘라내고 경고하며 `df.attrs["truncated"]` 가 `True`. `0` 은 무제한 |
| `allow_heavy` | 100만 행 이상 테이블을 TOP · WHERE · 집계 없이 읽는 것을 허용 |

### `check(verbose=True)`

접속 · 권한 · 설정을 점검하고 dict 로 돌려줍니다. 새 환경에서 처음 한 번, 접속이 의심될 때 실행합니다.

### 예외

| 예외 | 뜻 |
| --- | --- |
| `ConfigError` | `.env` 가 없거나 비어 있음, 로그인 거부 |
| `QueryRejected` | 실행 전에 거부 — SELECT 가 아님, 큰 테이블 조건 없음, 이름 형식 오류 |
| `QueryTimeout` | 제한시간 초과로 서버에서 중단 |
| `PermissionDenied` | SELECT 권한이 없는 테이블 |
| `DBError` | 그 외 (없는 테이블 · 컬럼, 네트워크 등). 위 예외들의 부모라 `except DBError` 로 한 번에 잡을 수 있음 |

---

## CLI

```
hhhs-db [--csv] [--max-rows N] [-v] <명령> ...

  check                         접속 · 권한 · 설정 자가점검
  tables [LIKE] [--min-rows N] [--schema S]
                                테이블 목록 (+행수). 기본 --min-rows 1 (빈 테이블 제외)
  columns TABLE                 컬럼 목록
  table TABLE [-n N] [-c COLS] [-w WHERE] [-o ORDER]
                                미리보기. -n 기본 20, 0 이면 제한 없음. -c 는 쉼표 구분
  query SQL | -                 SELECT 실행. '-' 면 stdin 에서 읽음. --allow-heavy 로 큰 테이블 허용
```

- 표는 200행까지 보여 줍니다. 더 필요하면 `--csv` 로 파일에 저장하세요.
- 마지막 줄에 `(행수, 실행시간)` 이 붙습니다. 상한에 걸리면 `상한에서 잘림` 이라고 표시됩니다.
- `-v` 를 붙이면 재시도 · 실행시간 로그가 stderr 로 나옵니다.

---

## DB 부하 방지 장치

운영 ERP 를 실무자가 쓰는 동안 조회하는 도구라, **서버에 부담을 주지 않는 것**을 조회 편의보다 우선했습니다. 전부 기본으로 켜져 있고 환경변수로 조정합니다.

| 장치 | 동작 | 조정 |
| --- | --- | --- |
| 조회 구문 보조 검사 | SELECT/WITH 시작 검사, 다중 문장·쓰기 키워드·SELECT INTO·외부 데이터 접근 구문 차단 (문자열·주석 제외). 완전한 SQL 파서나 보안 경계는 아님 | 최종 보장은 SELECT 전용 서버 계정 |
| 결과 행 상한 | 상한 + 1행까지만 받아 DataFrame을 자르고 결과를 닫음. 드라이버 버퍼링·서버 스캔량까지 제한하지는 않음. 잘리면 경고 | `HHHS_MAX_ROWS` (기본 10,000, 0 = 무제한), `max_rows=` |
| 쿼리 제한시간 | 서버에서 실행 중인 쿼리를 중단. 재시도하지 않음(무거운 쿼리를 반복하지 않기 위해) | `HHHS_QUERY_TIMEOUT` (기본 60초) |
| 큰 테이블 보호 | 100만 행 이상 테이블을 `TOP` · `WHERE` · 집계 · `GROUP BY` 없이 읽는 SQL 은 실행 전 거부 | `HHHS_HEAVY_ROWS`, `allow_heavy=True` |
| 동시 실행 1개 | 프로세스 안에서 쿼리는 한 번에 하나(잠금). 서버 연결도 1개(풀 크기 1) | — |
| 응답 적응 휴지 | 직전 쿼리가 오래 걸렸으면 그 시간의 20%(최대 5초)를 쉬고 다음 쿼리를 시작. 연속 호출 최소 간격 0.1초 | `HHHS_COOLDOWN=0` 으로 끔 |
| 잠금 회피 | 세션을 `READ UNCOMMITTED` 로 열어 데이터 잠금 경합을 줄임. 스키마 잠금 등은 여전히 발생 가능. `LOCK_TIMEOUT 5초` | — |
| 카탈로그 캐시 | 테이블 목록은 프로세스당 한 번만 읽음. 큰 테이블 판정도 이 캐시로 | `get_list_tables(refresh=True)` |
| 일시 오류 재시도 | 연결 끊김(20006 · 20047 · 20009)만 2초 뒤 한 번 재시도. 타임아웃 · 권한 오류는 재시도하지 않음 | — |
| 세션 식별 | 서버 세션 목록에 `hhhs_db_manager/버전` 으로 표시되어 DBA 가 이 도구의 쿼리를 구분할 수 있음 | — |

`READ UNCOMMITTED` 는 커밋되지 않은 행을 읽을 수 있다는 뜻입니다. 실무자가 입력 중인 전표가 집계에 섞이거나 행이 누락·중복될 수 있습니다. 날짜를 하루 전으로 잡아도 일관성이 보장되지는 않습니다. 마감 수치는 DBA가 승인한 일관성 있는 조회 환경에서 별도 검산하세요.

---

## 오류가 나면

메시지 앞의 예외 이름으로 구분합니다.

| 메시지 | 원인 | 조치 |
| --- | --- | --- |
| `ConfigError: 접속정보가 비어 있습니다` | `.env` 를 못 찾았거나 값이 빔 | [접속정보 설정](#접속정보-설정) 의 순서대로 파일 위치 확인 |
| `ConfigError: 계정 · 비밀번호가 거부` | 로그인 실패(18456) | 담당자에게 최신 값 요청 |
| `DBError: 서버에 닿지 못했습니다` | 네트워크 · 방화벽 | 사내망 · VPN 확인. 위치가 바뀌었으면 공인 IP 허용 요청 |
| `DBError: TLS 협상 실패` | 이 모듈보다 `pymssql` 이 먼저 import 됨 | 새 프로세스에서 `hhhs_db_manager` 를 먼저 import (노트북은 커널 재시작) |
| `QueryRejected: SELECT / WITH 로 시작하는 조회만` | 쓰기 · DDL 시도 | 이 도구의 범위 밖 |
| `QueryRejected: … 100만 행 이상인데 TOP · WHERE · 집계가 없습니다` | 큰 테이블 전체 읽기 | 조건을 넣거나 `allow_heavy=True` |
| `QueryTimeout` | 60초 초과 | 기간 · TOP 으로 범위를 줄임. 꼭 필요하면 `HHHS_QUERY_TIMEOUT` 상향 |
| `PermissionDenied` | 테이블 단위 권한이라 새 테이블은 자동으로 안 보임 | 테이블명을 적어 권한 추가 요청 |
| `DBError: 테이블이 없습니다` / `컬럼이 없습니다` | 이름 오타 | `get_list_tables("일부")`, `get_columns(테이블)` 로 확인 |
| `DBError: 다른 작업이 잠근 행을 … 기다리다 포기` | 실무자 트랜잭션과 충돌(드묾) | 잠시 후 재시도 |
| `UserWarning: 결과가 10,000행에서 잘렸습니다` | 행 상한 | 조건을 좁히거나 `max_rows=` 상향, CLI 는 `--max-rows` |

---

## 조회 팁

- 업무 테이블은 스키마 `NEOE` 에 있습니다. `get_table` 은 자동으로 붙이고, `query` 에서는 `NEOE.테이블` 로 씁니다.
- 일자 컬럼(`DT_*`)은 날짜형이 아니라 `'YYYYMMDD'` **문자열**입니다. `DT_SO >= '20260901'` 처럼 비교하고 `GETDATE()` 와 직접 비교하지 마세요. `DTS_*` 는 `'YYYYMMDDHHMMSS'`.
- 회사코드 `CD_COMPANY` 가 대부분 테이블의 PK 앞에 있습니다. 운영 회사 하나만 볼 때는 조건을 거세요 — 인덱스도 탑니다.
- HEAD/LINE 구조(`SA_SOH`/`SA_SOL` 등)는 건수를 셀 때 어느 쪽인지 구분합니다.
- 넓은 테이블(100열 이상)에 `SELECT *` 는 피하고 `columns=` 로 필요한 것만 고릅니다.
- 컬럼 이름은 영문 약어라 추측하면 틀립니다. `get_columns` 의 `name_kr` 로 확인하세요.
- 집계 값은 `COUNT(*)` 를 따로 돌려 검산합니다.

---

## 에이전트에서 쓰기

Claude Code · goose · codex 등 셸을 쓸 수 있는 에이전트는 `hhhs-db` CLI 를 그대로 부르면 됩니다. 결과가 표로 stdout 에 나오고, 오류는 예외 이름과 함께 stderr 로 나옵니다.

```bash
hhhs-db tables PRQ
hhhs-db columns PR_PRQL
hhhs-db table PR_PRQL -n 10 -w "DT_PRQ >= '20260901'"
hhhs-db --csv query "SELECT ..." > result.csv
```

에이전트 지침(스킬)에 넣을 때 권장하는 규칙:

1. SQL 을 쓰기 전에 `tables` → `columns` 로 이름을 확인한다.
2. 탐색은 `-n` 을 작게, 집계는 기간 조건을 넣는다.
3. 결과 · SQL · 근거(어떤 테이블 · 컬럼을 왜 골랐는지)를 함께 보고한다.
4. 조회 결과를 외부 서비스로 보내지 않는다.

---

## 주의사항

- **읽기 전용 계정을 전제**로 합니다. 도구의 SELECT 검사는 실수를 막는 보조 장치이고, 최종 보장은 서버 권한입니다. `hhhs-db check` 의 「쓰기 권한」이 0 이 아니면 담당자에게 알리세요.
- 조회 결과와 테이블 구조는 사내 자료입니다. 외부 서비스 · 공개 저장소 · 메신저에 올리지 마세요. 결과 파일(`*.csv`, `*.xlsx`)은 `.gitignore` 에 있습니다.
- 접속정보는 이 저장소에 없고, 앞으로도 넣지 않습니다.
- 실행 중인 ERP 서버입니다. 큰 테이블 전체 조회나 반복 실행이 필요하면 야간에 하거나 담당자와 상의하세요.

---

## 개발

```bash
git clone https://github.com/koreaben777/hhhs-db-manager && cd hhhs-db-manager
uv venv && uv pip install -e .
cp .env.example .env            # 값 채우기
hhhs-db check                   # 접속 · 권한 · 설정
```

- 코드는 `hhhs_db_manager.py` 한 파일, 사람용 예시는 `hhhs_db_manager.ipynb` 입니다. 공개 조회 함수는 `query()`를 거치므로 부하 장치는 거기에만 있습니다.
- `db.py` 는 이전 이름으로 부르던 스크립트를 위한 호환 파일입니다. 새 코드에서는 쓰지 마세요.
- TLS 설정(`openssl-legacy.cnf`)은 모듈 안에 내장되어 첫 실행 때 `~/.cache/hhhs_db_manager/` 에 풀립니다. 서버 인증서가 갱신되면 이 부분을 지우면 됩니다.
- 먼저 DB 연결 없이 회귀 테스트를 실행합니다: `uv run python3 -m unittest -v`.
- 테스트는 SQL 보조 검사, 입력 건수, 수신 상한, 사전 조회 실패, CLI 상한, 노트북 구문·출력·입력 도우미를 검증하며 `.env`를 읽지 않습니다.
- 실제 접속 검증은 승인된 환경에서 `hhhs-db check`, `hhhs-db tables SO`, `hhhs-db table SA_SOH -n 3`으로 별도 수행합니다. 오프라인 테스트 통과가 운영 DB·TLS 연결 성공을 의미하지는 않습니다.
- 서버 오류의 원문은 자격증명·호스트 노출 방지를 위해 일반 오류와 재시도 로그에서 표시하지 않습니다.
- `engine()`은 저수준 SQLAlchemy 연결이므로 직접 사용하면 `query()`의 보조 검사를 거치지 않습니다. 노트북에서는 공개 조회 함수를 사용하세요.

이 저장소는 유진 AI CoE 팀 내부용입니다. 별도 라이선스 표기가 없으며 회사 외부 이용을 허용하지 않습니다.
