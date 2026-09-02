#!/usr/bin/env python3
"""Local-only GoLand process dashboard with guarded termination actions."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = "127.0.0.1"
DEFAULT_PORT = 17654
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
LOG_DIR = ROOT / "logs"
JETBRAINS_LOG = Path.home() / "Library/Logs/JetBrains/GoLand2025.3/idea.log"
TELEMETRY_GLOB = str(Path.home() / "Library/Logs/JetBrains/GoLand2025.3/open-telemetry-meters.*.json")
ACTION_TOKEN = secrets.token_urlsafe(32)
ACTION_LOCK = threading.Lock()


@dataclass(frozen=True)
class Kind:
    group: str
    label: str
    risk: str
    can_stop: bool
    risk_text: str


KINDS: dict[str, Kind] = {
    "codebuddy": Kind(
        "codebuddy", "CodeBuddy", "medium", True,
        "会中断腾讯 CodeBuddy 补全或对话；GoLand 可能立即重新启动它。",
    ),
    "marscode": Kind(
        "marscode", "TraeCode / MarsCode", "medium", True,
        "会中断 TraeCode/MarsCode 请求；插件可能立即重新连接并拉起进程。",
    ),
    "ccgui": Kind(
        "ccgui", "CC GUI / Claude Agent", "high", True,
        "可能中断正在运行的 Claude/Codex 会话并丢失未返回的结果。",
    ),
    "semantic": Kind(
        "semantic", "Semantic Search", "medium", True,
        "会停止语义搜索模型；相关搜索暂时不可用，IDE 可能自动重启服务。",
    ),
    "terminal": Kind(
        "terminal", "IDE Terminal", "high", True,
        "会关闭 IDE 终端及其子任务，可能中断未保存的命令、服务或构建。",
    ),
    "devtool": Kind(
        "devtool", "Build / Debug Tool", "high", True,
        "会中断当前构建、测试或调试任务，可能留下未完成的输出。",
    ),
    "jcef": Kind(
        "jcef", "GoLand UI (JCEF)", "critical", False,
        "属于 IDE 界面基础设施，面板禁止停止。",
    ),
    "fsnotifier": Kind(
        "fsnotifier", "File Watcher", "critical", False,
        "属于 IDE 文件监听基础设施，面板禁止停止。",
    ),
    "unknown": Kind(
        "unknown", "Other GoLand Child", "high", True,
        "用途无法可靠识别；停止前应检查完整命令，可能影响项目任务。",
    ),
}


def run(args: list[str], timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def process_rows() -> list[dict[str, Any]]:
    output = run(["ps", "-axo", "pid=,ppid=,%cpu=,%mem=,rss=,etime=,command="])
    rows: list[dict[str, Any]] = []
    pattern = re.compile(r"^\s*(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\S+)\s+(.*)$")
    for raw in output.splitlines():
        match = pattern.match(raw)
        if not match:
            continue
        pid, ppid, cpu, mem, rss, elapsed, command = match.groups()
        rows.append({
            "pid": int(pid), "ppid": int(ppid), "cpu": float(cpu), "mem": float(mem),
            "rss_mb": round(int(rss) / 1024, 1), "elapsed": elapsed, "command": command,
        })
    return rows


def classify(command: str) -> tuple[str, str]:
    if "/plugins/coding-copilot-jetbrains/bin/fusion-macos-arm64" in command:
        return "codebuddy", "Fusion service"
    if "@agentclientprotocol/codex-acp" in command:
        return "codebuddy", "Codex ACP"
    if "/plugins/marscode/bin/tob-macos-" in command:
        return "marscode", "AI server"
    if "/idea-claude-code-gui/ai-bridge/daemon.js" in command:
        return "ccgui", "AI bridge"
    if "/.codemoss/dependencies/claude-sdk/" in command and "/claude " in command:
        return "ccgui", "Claude Agent"
    if "/semantic-search/" in command and "embeddings-server" in command:
        return "semantic", "Embeddings server"
    if "/cef_server.app/" in command:
        return "jcef", "JCEF server"
    if command.endswith("/Contents/bin/fsnotifier"):
        return "fsnotifier", "File notifier"
    if re.search(r"(^|/)(zsh|bash|fish)(\s|$)", command):
        return "terminal", "Terminal shell"
    if re.search(r"(^|\s)(go|dlv|gradle|java|kotlinc|npm|pnpm|yarn)(\s|$)", command):
        return "devtool", "Build/debug process"
    return "unknown", Path(command.split()[0]).name if command else "Unknown"


def snapshot() -> dict[str, Any]:
    rows = process_rows()
    by_pid = {row["pid"]: row for row in rows}
    goland = [row for row in rows if re.search(r"^/Applications/GoLand[^/]*/Contents/MacOS/goland$", row["command"])]
    goland_ids = {row["pid"] for row in goland}

    descendant_ids: set[int] = set()
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["pid"] in descendant_ids or row["pid"] in goland_ids:
                continue
            if row["ppid"] in goland_ids or row["ppid"] in descendant_ids:
                descendant_ids.add(row["pid"])
                changed = True

    direct_kinds = {pid: classify(by_pid[pid]["command"]) for pid in descendant_ids}
    processes: list[dict[str, Any]] = []
    for pid in descendant_ids:
        row = dict(by_pid[pid])
        group, role = direct_kinds[pid]
        if group == "unknown":
            ancestor = row["ppid"]
            seen: set[int] = set()
            while ancestor in descendant_ids and ancestor not in seen:
                seen.add(ancestor)
                ancestor_group, _ = direct_kinds[ancestor]
                if ancestor_group in {"codebuddy", "marscode", "ccgui", "semantic", "jcef"}:
                    group = ancestor_group
                    role = "Child process"
                    break
                ancestor = by_pid[ancestor]["ppid"]
        kind = KINDS[group]
        row.update({
            "group": group, "group_label": kind.label, "role": role,
            "risk": kind.risk, "risk_text": kind.risk_text, "can_stop": kind.can_stop,
            "fingerprint": fingerprint(row),
        })
        processes.append(row)
    processes.sort(key=lambda item: (-item["cpu"], -item["rss_mb"], item["pid"]))

    groups: list[dict[str, Any]] = []
    for name, kind in KINDS.items():
        members = [proc for proc in processes if proc["group"] == name]
        if not members:
            continue
        groups.append({
            "name": name, "label": kind.label, "risk": kind.risk, "risk_text": kind.risk_text,
            "can_stop": kind.can_stop, "count": len(members),
            "cpu": round(sum(proc["cpu"] for proc in members), 1),
            "rss_mb": round(sum(proc["rss_mb"] for proc in members), 1),
            "pids": [proc["pid"] for proc in members],
        })
    groups.sort(key=lambda item: (-item["cpu"], -item["rss_mb"]))

    return {
        "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "goland": [enrich_goland(row) for row in goland],
        "processes": processes,
        "groups": groups,
        "telemetry": telemetry(),
        "signals": log_signals(),
        "system": system_memory(),
        "limits": {"high_cpu": 25, "high_rss_mb": 300},
    }


def enrich_goland(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    thread_output = run(["ps", "-M", "-p", str(row["pid"])], timeout=2)
    enriched["threads"] = max(0, len(thread_output.splitlines()) - 1)
    return enriched


def fingerprint(row: dict[str, Any]) -> str:
    raw = f"{row['pid']}\0{row['ppid']}\0{row['command']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def telemetry() -> dict[str, Any]:
    files = glob.glob(TELEMETRY_GLOB)
    if not files:
        return {"available": False}
    latest = max(files, key=os.path.getmtime)
    try:
        data = json.loads(Path(latest).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    wanted = {
        "JVM.usedHeapBytes": "heap_used",
        "JVM.committedHeapBytes": "heap_committed",
        "JVM.GC.collections": "gc_count",
        "JVM.GC.collectionTimesMs": "gc_ms",
        "JVM.totalBytesAllocated": "allocated",
        "JVM.threadCount": "jvm_threads",
        "MEM.ramPlusSwapMinusFileMappingsBytes": "process_memory",
    }
    result: dict[str, Any] = {"available": True, "source": Path(latest).name, "age_seconds": int(time.time() - os.path.getmtime(latest))}
    for metric in data:
        key = wanted.get(metric.get("name"))
        if not key:
            continue
        points = metric.get("data", {}).get("points", [])
        if points:
            result[key] = points[0].get("value", 0)
    for key in ("heap_used", "heap_committed", "allocated", "process_memory"):
        if key in result:
            result[key + "_mb"] = round(result[key] / 1024 / 1024, 1)
    committed = result.get("heap_committed", 0)
    result["heap_percent"] = round(result.get("heap_used", 0) * 100 / committed, 1) if committed else None
    return result


def log_signals() -> list[dict[str, Any]]:
    if not JETBRAINS_LOG.exists():
        return []
    try:
        size = JETBRAINS_LOG.stat().st_size
        with JETBRAINS_LOG.open("rb") as handle:
            handle.seek(max(0, size - 2_000_000))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    cutoff = dt.datetime.now() - dt.timedelta(minutes=5)
    families = {
        "codebuddy": re.compile(r"com\.tencent\.code\.intel"),
        "marscode": re.compile(r"com\.codeverse|com\.aiserver|aha-ipc"),
        "ccgui": re.compile(r"com\.github\.claudecodegui|CodemossSettings"),
        "low_memory": re.compile(r"LowMemoryWatcher|Low memory signal"),
    }
    counts = {key: 0 for key in families}
    timestamp_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    for line in text.splitlines():
        match = timestamp_re.match(line)
        if not match:
            continue
        try:
            when = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if when < cutoff:
            continue
        for key, pattern in families.items():
            if pattern.search(line):
                counts[key] += 1
    labels = {"codebuddy": "CodeBuddy 日志", "marscode": "MarsCode 日志", "ccgui": "CC GUI 日志", "low_memory": "低内存信号"}
    return [{"name": key, "label": labels[key], "count": value, "window_minutes": 5} for key, value in counts.items()]


def system_memory() -> dict[str, Any]:
    page_size_text = run(["sysctl", "-n", "hw.pagesize"]).strip()
    vm = run(["vm_stat"])
    try:
        page_size = int(page_size_text)
    except ValueError:
        page_size = 16384
    values: dict[str, int] = {}
    for line in vm.splitlines():
        match = re.match(r"([^:]+):\s+(\d+)\.", line)
        if match:
            values[match.group(1)] = int(match.group(2))
    free_pages = values.get("Pages free", 0) + values.get("Pages speculative", 0)
    compressed_pages = values.get("Pages occupied by compressor", 0)
    return {
        "free_mb": round(free_pages * page_size / 1024 / 1024, 1),
        "compressed_mb": round(compressed_pages * page_size / 1024 / 1024, 1),
    }


def descendants(rows: list[dict[str, Any]], roots: set[int]) -> set[int]:
    result = set(roots)
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["pid"] not in result and row["ppid"] in result:
                result.add(row["pid"])
                changed = True
    return result


def stop_action(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    with ACTION_LOCK:
        current = snapshot()
        candidates = {proc["pid"]: proc for proc in current["processes"]}
        goland_ids = {proc["pid"] for proc in current["goland"]}
        scope = payload.get("scope")
        identifier = payload.get("id")
        ack = payload.get("ack", "")

        if scope == "pid":
            try:
                pid = int(identifier)
            except (TypeError, ValueError):
                return 400, {"error": "PID 无效"}
            proc = candidates.get(pid)
            if not proc:
                return 409, {"error": "进程已消失或不再属于 GoLand"}
            if payload.get("fingerprint") != proc["fingerprint"]:
                return 409, {"error": "进程身份已变化，为避免 PID 复用而拒绝操作"}
            if not proc["can_stop"]:
                return 403, {"error": "该进程属于受保护的 GoLand 基础设施"}
            expected = f"STOP PID {pid}"
            roots = {pid}
            description = f"{proc['group_label']} PID {pid}"
        elif scope == "group":
            group = str(identifier)
            members = [proc for proc in candidates.values() if proc["group"] == group]
            if not members:
                return 409, {"error": "该组进程已不存在"}
            if not KINDS.get(group, KINDS["unknown"]).can_stop:
                return 403, {"error": "该组属于受保护的 GoLand 基础设施"}
            expected = f"STOP GROUP {group}"
            roots = {proc["pid"] for proc in members}
            description = f"{KINDS[group].label} group"
        else:
            return 400, {"error": "操作范围无效"}

        if ack != expected:
            return 400, {"error": f"确认文本不匹配，应为：{expected}"}

        rows = process_rows()
        targets = descendants(rows, roots) - goland_ids
        by_pid = {row["pid"]: row for row in rows}
        safe_targets = [pid for pid in targets if pid in by_pid]
        safe_targets.sort(key=lambda pid: depth(pid, by_pid), reverse=True)
        if not safe_targets:
            return 409, {"error": "没有可停止的进程"}

        stopped: list[int] = []
        errors: list[str] = []
        for pid in safe_targets:
            if pid in goland_ids or pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                errors.append(f"PID {pid}: permission denied")
        log_action({"time": dt.datetime.now().astimezone().isoformat(), "description": description, "pids": stopped, "errors": errors})
        return 200, {"ok": True, "description": description, "stopped": stopped, "errors": errors, "note": "仅发送了 TERM；若插件重新拉起，需在 GoLand 中禁用插件并重启。"}


def depth(pid: int, by_pid: dict[int, dict[str, Any]]) -> int:
    result = 0
    seen: set[int] = set()
    while pid in by_pid and pid not in seen:
        seen.add(pid)
        pid = by_pid[pid]["ppid"]
        result += 1
    return result


def log_action(record: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "actions.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class Handler(SimpleHTTPRequestHandler):
    server_version = "GoLandDashboard/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_GET(self) -> None:
        if self.path == "/api/status":
            self.send_json(200, snapshot())
            return
        if self.path == "/api/config":
            self.send_json(200, {"action_token": ACTION_TOKEN, "poll_ms": 2000})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/stop":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.headers.get("X-Action-Token") != ACTION_TOKEN:
            self.send_json(403, {"error": "操作令牌无效，请刷新页面"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self.send_json(415, {"error": "只接受 application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 32_768:
            self.send_json(400, {"error": "请求大小无效"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "JSON 无效"})
            return
        code, response = stop_action(payload)
        self.send_json(code, response)

    def send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local GoLand AI process dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}"
    print(f"GoLand dashboard: {url}")
    print("Localhost only. Press Ctrl-C to stop.")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Stopped.")


if __name__ == "__main__":
    main()
