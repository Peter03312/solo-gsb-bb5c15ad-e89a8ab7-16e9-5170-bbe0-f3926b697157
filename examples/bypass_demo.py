#!/usr/bin/env python3
"""临时旁通评估调用示例（仅依赖标准库，全程只读）。

用法：
    python3 examples/bypass_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/bypass_network.json）：

    S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀,故障)-- U3
                   |           |            |
                 P3[阀]      P5[阀]       P7[阀]
                   |           |            |
                  U1          U2           E --P8[阀]-- U4

抢修期间，故障管段 P6 已隔离（关 P4 与 P7，U3/U4 失供，事件待修复）。
调度员评估一条临时旁通管的供水效果：服务先按当前阀位、运行水源与待修复
故障核算基线分档交付，再仅在内存中接入旁通管（允许与现有管段并联）；
接入后若任一待修复故障管段的任一端从运行水源可达，判为不可用并返回
故障编号与危险端点（不输出旁通交付量）；安全时返回基线与旁通后的
各等级、各用水点交付量及差值。本脚本依次演示：

  1. 导入管网（版本 1）
  2. 提交隔离：故障 P6，关 P4 与 P7（版本 -> 2，U3/U4 失供）
  3. 安全旁通 B—U4：U4 恢复供水，返回基线/旁通后交付与差值
  4. 并联旁通 S—A：与 P1（容量 6）并联提升瓶颈，等级 1 的 U1 先拿满
  5. 危险旁通 B—D：故障管段 P6 的 D 端将重新进水，判为不可用
  6. 旁通与故障管段并联（D—U3）：该连通块无运行水源，安全但无提升
  7. 已读版本过期：version_match=false，仍按当前状态核算
  8. 非法请求：端点相同/不存在、容量非法、管段未覆盖 -> 422 整体拒绝
  9. 确认全程只读：版本仍为 2、事件与阀位不变
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
NETWORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bypass_network.json")

# bypass_network 的全部水源与管段（编号须完整覆盖，不多不少）
SOURCE_CAPS = [{"source_id": "S", "capacity": 100}]
PIPE_CAPS = [
    {"pipe_id": pid, "capacity": 100}
    for pid in ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8")
]
# 带保障等级的用水需求：U1/U2 等级 1，U3/U4 等级 2
DEMANDS = [
    {"sink_id": "U1", "demand": 10, "level": 1},
    {"sink_id": "U2", "demand": 10, "level": 1},
    {"sink_id": "U3", "demand": 10, "level": 2},
    {"sink_id": "U4", "demand": 10, "level": 2},
]


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def bypass(version, frm, to, capacity, pipes=None):
    return call(
        "POST",
        "/api/delivery/bypass",
        {
            "version": version,
            "bypass": {"from": frm, "to": to, "capacity": capacity},
            "sources": SOURCE_CAPS,
            "pipes": pipes or PIPE_CAPS,
            "demands": DEMANDS,
        },
    )


def delivered(body: dict, key: str) -> list:
    return [(d["sink_id"], d["delivered"]) for d in body[key]["deliveries"]]


def main() -> None:
    with open(NETWORK, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200 and body["version"] == 1, body

    # 2. 提交隔离：故障 P6，关 P4 与 P7（U3/U4 失供，版本 -> 2）
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4", "P7"], "version": 1},
    )
    show("2. 提交隔离 P6（关 P4/P7，U3/U4 失供）", status, body)
    assert status == 200 and body["version"] == 2, body
    assert body["affected_sink_ids"] == ["U3", "U4"], body

    # 3. 安全旁通 B—U4：E/U4 区与故障端点被已关阀门 P7 隔开
    status, body = bypass(2, "B", "U4", 50)
    show("3. 安全旁通 B—U4（U4 恢复 10，U3 仍断供）", status, body)
    assert status == 200, body
    assert body["usable"] is True and body["block_reasons"] == [], body
    assert delivered(body, "baseline") == [
        ("U1", 10), ("U2", 10), ("U3", 0), ("U4", 0),
    ], body
    assert delivered(body, "with_bypass") == [
        ("U1", 10), ("U2", 10), ("U3", 0), ("U4", 10),
    ], body
    assert body["delta"]["total_delivered_delta"] == 10, body
    assert [
        (d["sink_id"], d["delivered_delta"]) for d in body["delta"]["deliveries"]
    ] == [("U1", 0), ("U2", 0), ("U3", 0), ("U4", 10)], body

    # 4. 并联旁通 S—A：P1 容量 6 为瓶颈，旁通 4 提升等级 1 交付
    tight_pipes = [
        {"pipe_id": pid, "capacity": (6 if pid == "P1" else 100)}
        for pid in ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8")
    ]
    status, body = bypass(2, "S", "A", 4, pipes=tight_pipes)
    show("4. 并联旁通 S—A（P1=6 瓶颈：基线 6 -> 旁通后 10）", status, body)
    assert status == 200 and body["usable"] is True, body
    assert body["baseline"]["total_delivered"] == 6, body
    assert body["with_bypass"]["total_delivered"] == 10, body
    assert body["delta"]["total_delivered_delta"] == 4, body

    # 5. 危险旁通 B—D：故障管段 P6 的 D 端将重新进水，判为不可用
    status, body = bypass(2, "B", "D", 50)
    show("5. 危险旁通 B—D（P6 的 D 端重新进水，不输出旁通交付量）", status, body)
    assert status == 200, body
    assert body["usable"] is False, body
    assert body["with_bypass"] is None and body["delta"] is None, body
    assert [
        (b["code"], b["pipe_id"], b["reachable_endpoints"])
        for b in body["block_reasons"]
    ] == [("FAULT_REWATER_RISK", "P6", ["D"])], body
    # 基线仍然给出
    assert body["baseline"]["total_delivered"] == 20, body

    # 6. 旁通与故障管段并联（D—U3）：该连通块无运行水源，安全但无提升
    status, body = bypass(2, "D", "U3", 50)
    show("6. 旁通与故障管段并联 D—U3（安全但无提升）", status, body)
    assert status == 200 and body["usable"] is True, body
    assert body["delta"]["total_delivered_delta"] == 0, body
    assert delivered(body, "with_bypass") == delivered(body, "baseline"), body

    # 7. 过期版本：仍按当前状态核算
    status, body = bypass(99, "B", "U4", 50)
    show("7. 已读版本过期（version_match=false，结果同第 3 步）", status, body)
    assert status == 200, body
    assert body["version"] == 2 and body["version_match"] is False, body
    assert body["usable"] is True and body["delta"]["total_delivered_delta"] == 10, body

    # 8. 非法请求（整体拒绝，返回全部问题明细）
    bad_same = {
        "version": 2,
        "bypass": {"from": "B", "to": "B", "capacity": 50},  # 两端相同
        "sources": SOURCE_CAPS,
        "pipes": PIPE_CAPS,
        "demands": DEMANDS,
    }
    status, body = call("POST", "/api/delivery/bypass", bad_same)
    show("8a. 旁通两端相同 -> 422 BYPASS_REQUEST_INVALID", status, body)
    assert status == 422 and body["error"]["code"] == "BYPASS_REQUEST_INVALID", body
    assert "BYPASS_ENDPOINT_SAME" in {i["code"] for i in body["error"]["issues"]}, body

    bad_ids = {
        "version": 2,
        "bypass": {"from": "B", "to": "UX", "capacity": 50},  # 端点不存在
        "sources": SOURCE_CAPS,
        "pipes": PIPE_CAPS[:-1],  # 管段未覆盖（缺 P8）
        "demands": DEMANDS,
    }
    status, body = call("POST", "/api/delivery/bypass", bad_ids)
    show("8b. 端点不存在 + 管段未覆盖 -> 422（明细同批返回）", status, body)
    assert status == 422, body
    codes = {i["code"] for i in body["error"]["issues"]}
    assert {"BYPASS_ENDPOINT_NOT_FOUND", "DELIVERY_PIPE_MISSING"} <= codes, codes

    bad_capacity = {
        "version": 2,
        "bypass": {"from": "B", "to": "U4", "capacity": -1},  # 容量非法
        "sources": SOURCE_CAPS,
        "pipes": PIPE_CAPS,
        "demands": DEMANDS,
    }
    status, body = call("POST", "/api/delivery/bypass", bad_capacity)
    show("8c. 旁通容量为负 -> 422（请求结构校验）", status, body)
    assert status == 422, body

    # 9. 只读核对：版本仍为 2、仅 1 条待修复事件、P4/P7 仍关闭
    status, net = call("GET", "/api/network")
    assert status == 200 and net["version"] == 2, net
    valves = {p["id"]: p["valve_closed"] for p in net["pipes"]}
    assert valves["P4"] is True and valves["P7"] is True, valves
    status, events = call("GET", "/api/isolation/events")
    assert status == 200 and len(events) == 1 and events[0]["status"] == "open"
    print(
        "\n9. 只读核对通过：版本仍为 2，仅 1 条待修复事件（P6），"
        "P4/P7 阀位未被旁通评估改动。"
    )
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
