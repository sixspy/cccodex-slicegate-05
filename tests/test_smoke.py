
import threading

import pytest
from fastapi.testclient import TestClient
from app import app, init_db

@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield

def _login(c, username="admin", password="123456"):
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}

def _register(c, h, slice_no, rows, expect):
    r = c.post("/api/slices", headers=h,
               json={"slice_no": slice_no, "biz_date": "2026-11-05", "partition_key": "mid",
                     "row_count": rows, "expected_count": expect})
    assert r.status_code == 200
    return r.json()["id"]

def _releases(c, h, sid):
    return [x for x in c.get("/api/releases", headers=h).json()["items"] if x["slice_id"] == sid]

def _slice(c, h, sid):
    return [s for s in c.get("/api/slices", headers=h).json()["items"] if s["id"] == sid][0]

def test_pipeline():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "日切放行"
    h = _login(c)
    assert len(c.get("/api/slices", headers=h).json()["items"]) >= 1
    # 互斥账本落笔前须先裁决：未裁决直接放行 → 409 且不留任何流水
    sid = _register(c, h, "SL-T-1", 10, 10)
    assert _slice(c, h, sid)["completeness"] == "待裁"
    before = len(c.get("/api/releases", headers=h).json()["items"])
    r = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert r.status_code == 409
    assert len(c.get("/api/releases", headers=h).json()["items"]) == before
    # 裁决入册 → 完整放行 → 推水位
    a = c.post(f"/api/slices/{sid}/adjudicate", headers=h, json={"completeness": "完整"})
    assert a.status_code == 200
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert rel.status_code == 200
    assert _slice(c, h, sid)["release_status"] == "已放行"
    assert any(w["source_slice"] == "SL-T-1" for w in c.get("/api/watermarks", headers=h).json()["items"])
    # 同下游重复放行 → 409
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"}).status_code == 409

def test_partial_jia_hard_reject():
    """甲处置：部分完整硬拒绝，只写拒绝流水与原因，不改放行态、不推水位。"""
    c = TestClient(app)
    h = _login(c)
    sid = _register(c, h, "SL-P-JIA", 5, 10)
    a = c.post(f"/api/slices/{sid}/adjudicate", headers=h, json={"completeness": "部分完整", "mode": "甲"})
    assert a.status_code == 200 and a.json()["release_status"] == "未放行"
    r = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert r.status_code == 409
    s = _slice(c, h, sid)
    assert s["completeness"] == "部分完整" and s["release_status"] == "未放行"  # 不改放行态
    rels = _releases(c, h, sid)
    assert len(rels) == 1 and rels[0]["status"] == "拒绝" and "甲" in rels[0]["note"]  # 只写拒绝流水与原因
    assert not any(w["source_slice"] == "SL-P-JIA" for w in c.get("/api/watermarks", headers=h).json()["items"])  # 不推水位

def test_partial_yi_readonly_freeze():
    """乙处置：降级只读冻结，详情可看，成功放行与水位推进硬拒绝。"""
    c = TestClient(app)
    h = _login(c)
    sid = _register(c, h, "SL-P-YI", 7, 10)
    a = c.post(f"/api/slices/{sid}/adjudicate", headers=h, json={"completeness": "部分完整", "mode": "乙"})
    assert a.status_code == 200 and a.json()["release_status"] == "冻结"
    # 详情可看
    assert _slice(c, h, sid)["release_status"] == "冻结"
    r = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert r.status_code == 409
    s = _slice(c, h, sid)
    assert s["release_status"] == "冻结"          # 仍是冻结，绝不变已放行
    assert all(x["status"] == "拒绝" for x in _releases(c, h, sid))
    assert not any(w["source_slice"] == "SL-P-YI" for w in c.get("/api/watermarks", headers=h).json()["items"])

def test_mode_locked_same_day_no_mixing():
    """当日定死后禁止再换、禁止甲乙混判，偏离记完整度口径偏了。"""
    c = TestClient(app)
    h = _login(c)
    s1 = _register(c, h, "SL-M-1", 5, 10)
    s2 = _register(c, h, "SL-M-2", 6, 10)
    assert c.post(f"/api/slices/{s1}/adjudicate", headers=h, json={"completeness": "部分完整", "mode": "甲"}).status_code == 200
    # 换乙 → 拒，记完整度口径偏了
    r = c.post(f"/api/slices/{s2}/adjudicate", headers=h, json={"completeness": "部分完整", "mode": "乙"})
    assert r.status_code == 409 and r.json()["detail"] == "完整度口径偏了"
    book = c.get("/api/adjudications", headers=h).json()["items"]
    assert any(x["slice_id"] == s2 and x["result"] == "已拒绝" and x["note"] == "完整度口径偏了" for x in book)
    # 策略旁注同样锁死
    assert c.post("/api/policy", headers=h, json={"mode": "乙"}).status_code == 409
    assert c.get("/api/policy", headers=h).json()["mode"] == "甲"

def test_wrong_verdict_recorded():
    """不按正确性口径裁定 → 拒绝并记完整度口径偏了，切片状态不动。"""
    c = TestClient(app)
    h = _login(c)
    sid = _register(c, h, "SL-W-1", 5, 10)  # 口径应为部分完整
    r = c.post(f"/api/slices/{sid}/adjudicate", headers=h, json={"completeness": "完整"})
    assert r.status_code == 409 and r.json()["detail"] == "完整度口径偏了"
    book = c.get("/api/adjudications", headers=h).json()["items"]
    assert any(x["slice_id"] == sid and x["result"] == "已拒绝" and x["note"] == "完整度口径偏了" for x in book)
    assert _slice(c, h, sid)["completeness"] == "待裁"  # 状态机未被污染

def test_adjudication_contention_queued():
    """两名值班同时下裁决须排队：后手记裁决争用，不得悄悄成功。"""
    c1, c2 = TestClient(app), TestClient(app)
    h1, h2 = _login(c1), _login(c2)
    sid = _register(c1, h1, "SL-C-1", 5, 10)
    barrier = threading.Barrier(2)
    results = []

    def adj(client, h):
        barrier.wait(timeout=5)
        r = client.post(f"/api/slices/{sid}/adjudicate", headers=h,
                        json={"completeness": "部分完整", "mode": "甲"})
        results.append(r.status_code)

    ts = [threading.Thread(target=adj, args=(c1, h1)), threading.Thread(target=adj, args=(c2, h2))]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sorted(results) == [200, 409]
    book = c1.get("/api/adjudications", headers=h1).json()["items"]
    live = [x for x in book if x["slice_id"] == sid and x["result"] == "生效"]
    assert len(live) == 1  # 后手不得悄悄成功
    assert any(x["slice_id"] == sid and x["result"] == "已拒绝" and x["note"] == "裁决争用" for x in book)
    # 当日再裁同一切片（哪怕不同时）也是争用
    r = c1.post(f"/api/slices/{sid}/adjudicate", headers=h1, json={"completeness": "部分完整", "mode": "甲"})
    assert r.status_code == 409 and r.json()["detail"] == "裁决争用"

def test_partial_never_released_and_views_consistent():
    """禁止部分完整标成已放行，禁止拒绝流水与已放行态同时成立；四方可对读。"""
    c = TestClient(app)
    h = _login(c)
    pid = _register(c, h, "SL-V-P", 3, 10)
    oid = _register(c, h, "SL-V-F", 10, 10)
    c.post(f"/api/slices/{pid}/adjudicate", headers=h, json={"completeness": "部分完整", "mode": "乙"})
    c.post(f"/api/slices/{oid}/adjudicate", headers=h, json={"completeness": "完整"})
    c.post(f"/api/slices/{pid}/release", headers=h, json={"consumer": "ads_x"})   # 硬拒绝
    assert c.post(f"/api/slices/{oid}/release", headers=h, json={"consumer": "ads_x"}).status_code == 200
    # 部分完整列表与裁决册对得上
    partials = c.get("/api/slices/partial", headers=h).json()["items"]
    assert [p["id"] for p in partials if p["id"] == pid]
    p = [p for p in partials if p["id"] == pid][0]
    assert p["mode"] == "乙" and p["release_status"] == "冻结" and p["reject_cnt"] == 1 and p["ok_cnt"] == 0
    # 已放行切片无任何拒绝流水
    assert all(x["status"] == "成功" for x in _releases(c, h, oid))
    # 已放行切片禁止再裁
    assert c.post(f"/api/slices/{oid}/adjudicate", headers=h, json={"completeness": "完整"}).status_code == 409
    # 四方对读无打架
    cs = c.get("/api/consistency", headers=h).json()
    assert cs["ok"], cs["violations"]

def test_viewer_readonly():
    c = TestClient(app)
    h = _login(c, "viewer", "view123")
    assert c.get("/api/slices", headers=h).status_code == 200
    assert c.post("/api/slices", headers=h, json={"slice_no": "SL-R-1", "biz_date": "2026-11-05",
                  "partition_key": "mid", "row_count": 1, "expected_count": 1}).status_code == 403
    assert c.post("/api/slices/1/adjudicate", headers=h, json={"completeness": "部分完整", "mode": "甲"}).status_code == 403
    assert c.post("/api/slices/1/release", headers=h, json={"consumer": "x"}).status_code == 403
