
import hashlib, secrets, sqlite3
from datetime import datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：事务全部由 BEGIN IMMEDIATE/COMMIT/ROLLBACK 显式管理，
    # 裁决落笔必须排队，后手不得悄悄成功。
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

def require_duty(username: str):
    """落笔（登记/裁决/放行/定策略）须值班账号，只读账号禁止。"""
    with connect() as c:
        row = c.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
    if row and row["role"] == "只读":
        raise HTTPException(403, "只读账号禁止落笔")

from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

# ── 完整度口径（正确性口径，唯一来源）────────────────────
# 行数大于零但未达期望 = 部分完整，禁止含糊放行。
def verdict_of(row_count: int, expected_count: int) -> str:
    if expected_count > 0 and row_count >= expected_count:
        return "完整"
    if row_count > 0:
        return "部分完整"
    return "待检"

MODES = ("甲", "乙")
MODE_NOTES = {
    "甲": "甲处置：部分完整一律硬拒绝，只写拒绝流水与原因，不改放行态、不推水位",
    "乙": "乙处置：部分完整进入降级只读冻结，详情可看，成功放行与水位推进硬拒绝",
}
REJECT_NOTES = {
    "甲": "部分完整硬拒绝（甲）：只写拒绝流水与原因，不改放行态、不推水位",
    "乙": "部分完整降级只读冻结（乙）：详情可看，成功放行与水位推进硬拒绝",
}

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

SCHEMA = """
CREATE TABLE IF NOT EXISTS slices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_no TEXT UNIQUE NOT NULL,
  biz_date TEXT NOT NULL,
  partition_key TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  expected_count INTEGER NOT NULL,
  completeness TEXT NOT NULL,
  release_status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_id INTEGER NOT NULL,
  consumer TEXT NOT NULL,
  status TEXT NOT NULL,
  note TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  consumer TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  watermark TEXT NOT NULL,
  source_slice TEXT NOT NULL
);
-- 完整度裁决册：互斥账本落笔前的裁定凭据，当日禁止再换。
CREATE TABLE IF NOT EXISTS adjudications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  verdict TEXT NOT NULL,
  mode TEXT,
  decided_by TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  result TEXT NOT NULL,          -- 生效 / 已拒绝
  note TEXT NOT NULL
);
-- 同一切片当日只许一条生效裁决：两名值班同时下裁决须排队，后手记裁决争用。
CREATE UNIQUE INDEX IF NOT EXISTS ux_adjudications_live
  ON adjudications(slice_id, day) WHERE result='生效';
-- 策略旁注：部分完整处置模式（甲/乙）当日定死，禁止再换、禁止混判。
CREATE TABLE IF NOT EXISTS day_policy(
  day TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  decided_by TEXT NOT NULL,
  decided_at TEXT NOT NULL
);
"""

def seed(conn):
    rows = [
        ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
        ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
        ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待裁", "未放行"),
    ]
    for r in rows:
        conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
    # 历史裁决入账（裁决册是口径列的唯一来源）
    conn.execute("INSERT INTO adjudications(slice_id,day,verdict,mode,decided_by,decided_at,result,note) VALUES (1,'2026-11-01','部分完整','甲','system','2026-11-01T17:50:00','生效','按口径裁定：行数大于零但未达期望')")
    conn.execute("INSERT INTO adjudications(slice_id,day,verdict,mode,decided_by,decided_at,result,note) VALUES (2,'2026-11-01','完整',NULL,'system','2026-11-01T17:59:00','生效','按口径裁定：行数达期望')")
    conn.execute("INSERT INTO day_policy(day,mode,decided_by,decided_at) VALUES ('2026-11-01','甲','system','2026-11-01T17:50:00')")
    conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (2,'ads_order','成功','完整切片放行：裁决册在案','2026-11-01T18:00:00')")
    conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (1,'ads_order','拒绝',?,'2026-11-01T18:05:00')", (REJECT_NOTES["甲"],))
    conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")

def migrate(conn):
    """既有库归位：已放行切片必须裁决册在案；无生效裁决的口径列一律回待裁。"""
    for r in conn.execute("""SELECT s.id, s.biz_date,
               (SELECT MIN(created_at) FROM releases WHERE slice_id=s.id AND status='成功') AS first_ok
             FROM slices s
             WHERE s.release_status='已放行'
               AND NOT EXISTS (SELECT 1 FROM adjudications a WHERE a.slice_id=s.id AND a.result='生效')""").fetchall():
        at = r["first_ok"] or (r["biz_date"] + "T00:00:00")
        conn.execute("INSERT INTO adjudications(slice_id,day,verdict,mode,decided_by,decided_at,result,note) VALUES (?,?,?,?,?,?,'生效','迁移补登：已放行切片须有完整裁决入账')",
                     (r["id"], at[:10], "完整", None, "system", at))
    conn.execute("""UPDATE slices SET completeness='待裁'
                    WHERE completeness != '待裁'
                      AND NOT EXISTS (SELECT 1 FROM adjudications a WHERE a.slice_id=slices.id AND a.result='生效')""")

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            seed(conn)
        migrate(conn)

@app.on_event("startup")
def _start():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
        a = c.execute("SELECT COUNT(*) c FROM adjudications").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "adjudications": a, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
    return {"items": [dict(r) for r in rows]}

class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int

@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_duty(user)
    if body.expected_count <= 0:
        raise HTTPException(400, "期望行数须大于零")
    if body.row_count < 0:
        raise HTTPException(400, "实收行数不得为负")
    # 登记不落结论：口径一律待裁，由完整度裁决册定夺。
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, "待裁", "未放行"),
            )
            return {"id": cur.lastrowid, "completeness": "待裁"}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")

class AdjudicateIn(BaseModel):
    completeness: str          # 值班提交的裁定结论
    mode: str | None = None    # 结论为部分完整时必填：甲 / 乙

@app.post("/api/slices/{sid}/adjudicate")
def adjudicate(sid: int, body: AdjudicateIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_duty(user)
    if body.completeness not in ("完整", "部分完整", "待检"):
        raise HTTPException(400, "非法裁决结论")
    if body.mode is not None and body.mode not in MODES:
        raise HTTPException(400, "非法处置模式")
    today = datetime.now().date().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")  # 裁决排队：写锁先行，后手看到的必是已落笔状态
        row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        if row["release_status"] == "已放行":
            raise HTTPException(409, "已放行切片禁止再裁")

        def reject(note: str, code: int = 409):
            # 拒绝也入册留痕（结果=已拒绝），不得悄悄失败
            conn.execute("INSERT INTO adjudications(slice_id,day,verdict,mode,decided_by,decided_at,result,note) VALUES (?,?,?,?,?,?,'已拒绝',?)",
                         (sid, today, body.completeness, body.mode, user, now, note))
            conn.commit()
            raise HTTPException(code, note)

        # ① 按正确性口径裁定：提交结论与口径不符即偏离裁决册
        correct = verdict_of(row["row_count"], row["expected_count"])
        if body.completeness != correct:
            reject("完整度口径偏了")
        # ② 同一切片当日已有生效裁决：后手记裁决争用，不得悄悄成功
        if conn.execute("SELECT 1 FROM adjudications WHERE slice_id=? AND day=? AND result='生效'", (sid, today)).fetchone():
            reject("裁决争用")
        # ③ 部分完整须定死甲/乙；当日模式已定死则禁止再换、禁止混判
        if correct == "部分完整":
            if body.mode is None:
                raise HTTPException(400, "部分完整须定死甲或乙处置")
            pol = conn.execute("SELECT * FROM day_policy WHERE day=?", (today,)).fetchone()
            if pol and pol["mode"] != body.mode:
                reject("完整度口径偏了")
            if not pol:
                conn.execute("INSERT INTO day_policy(day,mode,decided_by,decided_at) VALUES (?,?,?,?)", (today, body.mode, user, now))
        note = {"完整": "按口径裁定：行数达期望", "部分完整": "按口径裁定：行数大于零但未达期望", "待检": "按口径裁定：零行待检"}[correct]
        conn.execute("INSERT INTO adjudications(slice_id,day,verdict,mode,decided_by,decided_at,result,note) VALUES (?,?,?,?,?,?,'生效',?)",
                     (sid, today, correct, body.mode if correct == "部分完整" else None, user, now, note))
        # 状态机：乙处置进入降级只读冻结；其余非已放行态归未放行
        if correct == "部分完整" and body.mode == "乙":
            new_status = "冻结"
        else:
            new_status = "未放行"
        conn.execute("UPDATE slices SET completeness=?, release_status=? WHERE id=?", (correct, new_status, sid))
        conn.commit()
        return {"id": sid, "completeness": correct, "mode": body.mode if correct == "部分完整" else None, "release_status": new_status}
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()

class PolicyIn(BaseModel):
    mode: str

@app.get("/api/policy")
def get_policy(authorization: str | None = Header(default=None)):
    require_user(authorization)
    today = datetime.now().date().isoformat()
    with connect() as c:
        pol = c.execute("SELECT * FROM day_policy WHERE day=?", (today,)).fetchone()
    return {
        "today": today,
        "mode": pol["mode"] if pol else None,
        "decided_by": pol["decided_by"] if pol else None,
        "decided_at": pol["decided_at"] if pol else None,
        "modes": [{"mode": m, "note": MODE_NOTES[m]} for m in MODES],
    }

@app.post("/api/policy")
def pin_policy(body: PolicyIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_duty(user)
    if body.mode not in MODES:
        raise HTTPException(400, "非法处置模式")
    today = datetime.now().date().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pol = conn.execute("SELECT * FROM day_policy WHERE day=?", (today,)).fetchone()
        if pol:
            if pol["mode"] != body.mode:
                raise HTTPException(409, f"当日处置模式已定死为{pol['mode']}，禁止再换")
            return {"day": today, "mode": pol["mode"], "already": True}
        conn.execute("INSERT INTO day_policy(day,mode,decided_by,decided_at) VALUES (?,?,?,?)", (today, body.mode, user, now))
        conn.commit()
        return {"day": today, "mode": body.mode, "already": False}
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()

@app.get("/api/adjudications")
def list_adjudications(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT a.*, s.slice_no FROM adjudications a
                            JOIN slices s ON s.id=a.slice_id ORDER BY a.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/slices/partial")
def list_partial(authorization: str | None = Header(default=None)):
    """部分完整列表：以裁决册生效裁决为准，联流水聚合计数，保证四方可对读。"""
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""
            SELECT s.*, a.mode AS mode, a.decided_by, a.decided_at,
              (SELECT COUNT(*) FROM releases r WHERE r.slice_id=s.id AND r.status='拒绝') AS reject_cnt,
              (SELECT COUNT(*) FROM releases r WHERE r.slice_id=s.id AND r.status='成功') AS ok_cnt
            FROM slices s
            JOIN adjudications a ON a.slice_id=s.id AND a.result='生效'
            WHERE a.verdict='部分完整'
              AND a.id=(SELECT MAX(id) FROM adjudications x WHERE x.slice_id=s.id AND x.result='生效')
            ORDER BY s.id""").fetchall()
    return {"items": [dict(r) for r in rows]}

class ReleaseIn(BaseModel):
    consumer: str

@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_duty(user)
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        # 互斥账本落笔前，须先按正确性口径裁定并入册
        adj = conn.execute("SELECT * FROM adjudications WHERE slice_id=? AND result='生效' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        if not adj:
            raise HTTPException(409, "落笔前须先按完整度口径裁决入账")
        if adj["verdict"] != "完整":
            # 部分完整禁止含糊放行：按裁决册定死的甲/乙处置硬拒绝，
            # 只写拒绝流水与原因，不改放行态、不推水位。
            if adj["verdict"] == "部分完整":
                note = REJECT_NOTES[adj["mode"]]
            else:
                note = "待检切片完整度未达标：只写拒绝流水，不改放行态、不推水位"
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                         (sid, body.consumer, "拒绝", note, now))
            conn.commit()
            raise HTTPException(409, note)
        if conn.execute("SELECT 1 FROM releases WHERE slice_id=? AND consumer=? AND status='成功'", (sid, body.consumer)).fetchone():
            raise HTTPException(409, "该下游已放行，禁止重复落笔")
        conn.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
        conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                     (sid, body.consumer, "成功", "完整切片放行：裁决册在案", now))
        conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
                     (body.consumer, row["biz_date"], now, row["slice_no"]))
        conn.commit()
        return {"ok": True}
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()

@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/consistency")
def consistency(authorization: str | None = Header(default=None)):
    """对读校验：部分完整列表、裁决详情、放行流水、策略旁注不得互相打架。"""
    require_user(authorization)
    violations: list[str] = []
    with connect() as c:
        slices = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
        for s in slices:
            no = s["slice_no"]
            adj = c.execute("SELECT * FROM adjudications WHERE slice_id=? AND result='生效' ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone()
            ok_cnt = c.execute("SELECT COUNT(*) c FROM releases WHERE slice_id=? AND status='成功'", (s["id"],)).fetchone()["c"]
            rej_cnt = c.execute("SELECT COUNT(*) c FROM releases WHERE slice_id=? AND status='拒绝'", (s["id"],)).fetchone()["c"]
            wm_cnt = c.execute("SELECT COUNT(*) c FROM watermarks WHERE source_slice=?", (no,)).fetchone()["c"]
            if adj is None:
                if s["completeness"] != "待裁":
                    violations.append(f"{no}：未入裁决册但口径列为{s['completeness']}")
                if s["release_status"] == "已放行":
                    violations.append(f"{no}：未裁决却已放行")
                continue
            if s["completeness"] != adj["verdict"]:
                violations.append(f"{no}：列表口径{s['completeness']}与裁决册{adj['verdict']}打架")
            if adj["verdict"] != "完整":
                if s["release_status"] == "已放行":
                    violations.append(f"{no}：部分完整被标成已放行")
                if ok_cnt:
                    violations.append(f"{no}：非完整切片存在成功流水")
                if wm_cnt:
                    violations.append(f"{no}：非完整切片推了水位")
            if s["release_status"] == "已放行":
                if rej_cnt:
                    violations.append(f"{no}：拒绝流水与已放行态同时成立")
                if not ok_cnt:
                    violations.append(f"{no}：已放行但无成功流水")
            if adj["verdict"] == "部分完整":
                want = "冻结" if adj["mode"] == "乙" else "未放行"
                if s["release_status"] != want:
                    violations.append(f"{no}：{adj['mode']}处置放行态应为{want}，实为{s['release_status']}")
    return {"ok": not violations, "violations": violations}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
