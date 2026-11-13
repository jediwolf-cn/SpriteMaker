"""远程 ComfyUI 客户端（纯标准库）。

覆盖：探活、上传素材、提交 API 格式工作流、轮询 /history、过滤保存节点产物、下载。
调用方式参考 ComfyShell/app/comfy_client.py，去掉了 httpx 依赖。

远程 ComfyUI 经公网网关访问，偶发 502 / WinError 10060 连接超时。网络抖动策略：
  * 上传 / 提交 / 下载：请求级自动重试；提交重试前先查 /queue，避免响应丢失时重复排队；
  * /history 轮询：单次查询失败只记「网络抖动」，不消耗「任务丢失」计数，
    退避后继续轮询，直到 job_timeout；连接恢复后自动继续等待产物；
  * 所有等待都可被 should_stop 打断（用户取消任务时不会卡在重试 sleep 里）。
"""
from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from . import config

# 只有这些节点算「真产物」；PreviewAny / ImageCompare 会生成临时文件，必须排除
SAVE_CLASSES = {
    "SaveImage", "SaveImageAdvanced", "SaveVideo", "SaveWEBM",
    "SaveAudio", "SaveAudioAdvanced", "SaveAnimatedWEBP", "SaveAnimatedPNG",
}

# 可安全重试的网关 / 连接类错误（生成任务仍在服务端，重试不会重复生成）
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}
_RETRYABLE_HINTS = (
    "HTTP 408", "HTTP 425", "HTTP 429", "HTTP 500", "HTTP 502",
    "HTTP 503", "HTTP 504", "HTTP 522", "HTTP 524",
    "无法连接", "timed out", "timeout", "超时", "读取 ComfyUI 响应失败",
    "Connection reset", "Connection aborted", "Connection refused",
    "Remote end closed", "IncompleteRead", "10054", "10060", "10061",
    "getaddrinfo", "Name or service not known",
)


class ComfyError(RuntimeError):
    pass


LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _is_retryable(exc: BaseException) -> bool:
    text = str(exc)
    return any(hint in text for hint in _RETRYABLE_HINTS)


class ComfyClient:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or config.load()
        self.base = str(cfg["comfy_url"]).rstrip("/")
        self.user = str(cfg.get("comfy_user") or "")
        self.password = str(cfg.get("comfy_password") or "")
        self.subfolder = str(cfg.get("comfy_subfolder") or "spritemaker")
        self.timeout = float(cfg.get("comfy_timeout") or 60)
        self.job_timeout = float(cfg.get("job_timeout") or 3600)
        self.poll = float(cfg.get("poll_interval") or 2.0)
        # 单次查询用较短超时，卡住的连接尽快失败并进入下一轮重试
        self.poll_request_timeout = float(cfg.get("poll_request_timeout") or 15)
        self.poll_error_backoff = float(cfg.get("poll_error_backoff") or 2.0)
        self.poll_max_errors = max(0, int(cfg.get("poll_max_errors") or 0))  # 0 = 不限次，直到 job_timeout
        # 请求级重试：上传 / 提交 / 下载
        self.request_retries = max(1, int(cfg.get("request_retries") or 3))
        self.request_retry_delay = float(cfg.get("request_retry_delay") or 3.0)
        self.submit_retries = max(1, int(cfg.get("submit_retries") or self.request_retries))
        self.download_retries = max(1, int(cfg.get("download_retries") or self.request_retries))
        self.download_retry_delay = float(cfg.get("download_retry_delay") or self.request_retry_delay)
        self.client_id = "spritemaker_" + uuid.uuid4().hex[:12]
        # run() 期间把日志与取消回调挂到实例上，让上传等外部调用也能复用
        self.log: LogFn = _noop
        self.should_stop: Callable[[], bool] | None = None
        # run() 提交成功后回传 prompt_id，供外部在取消时精准中断这一个任务
        self.on_prompt: Callable[[str], None] | None = None

    # ------------------------------------------------------------ 基础请求
    def _auth_headers(self) -> dict[str, str]:
        if not self.user:
            return {}
        token = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token}

    @staticmethod
    def _sleep(seconds: float, should_stop: Callable[[], bool] | None = None) -> None:
        """可中断的 sleep：取消任务时不会卡在重试等待里。"""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if should_stop and should_stop():
                raise ComfyError("任务已取消")
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.25, left))

    def _request(self, path: str, body=None, *, timeout: float | None = None,
                 raw: bytes | None = None, extra_headers: dict | None = None,
                 retries: int = 0, retry_delay: float | None = None,
                 log: LogFn | None = None,
                 should_stop: Callable[[], bool] | None = None) -> bytes:
        """单次 HTTP 请求；retries>0 时对连接类 / 网关类错误自动重试。"""
        report = log if log is not None else self.log
        cancel = should_stop if should_stop is not None else self.should_stop
        headers = self._auth_headers()
        data = None
        if raw is not None:
            data = raw
            headers.update(extra_headers or {})
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        eff_timeout = timeout or self.timeout
        delay = self.request_retry_delay if retry_delay is None else retry_delay
        last: ComfyError | None = None

        for attempt in range(1, retries + 2):
            if cancel and cancel():
                raise ComfyError("任务已取消")
            retryable = False
            try:
                req = urllib.request.Request(
                    self.base + path, data=data,
                    method="POST" if data is not None else "GET", headers=headers,
                )
                with urllib.request.urlopen(req, timeout=eff_timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code == 400 and path == "/prompt":
                    # 工作流校验错误重试也没用，直接给出可读的节点信息
                    raise ComfyError("工作流校验未通过：" + _format_node_error(detail)) from exc
                last = ComfyError(f"{path} 返回 HTTP {exc.code}：{detail[:400]}")
                retryable = exc.code in RETRYABLE_STATUS
            except urllib.error.URLError as exc:
                last = ComfyError(f"无法连接 ComfyUI（{self.base}）：{exc.reason}")
                retryable = _is_retryable(last)
            except (OSError, http.client.HTTPException) as exc:
                # resp.read() 中途断连、IncompleteRead 等
                last = ComfyError(f"读取 ComfyUI 响应失败（{path}）：{exc}")
                retryable = True

            if attempt > retries or not retryable:
                raise last
            wait = float(delay) * attempt
            report(f"网络抖动：{str(last)[:140]}；{wait:.0f}s 后重试（{attempt}/{retries}）")
            self._sleep(wait, cancel)
        raise last or ComfyError(f"请求 {path} 失败")

    def _json(self, path: str, body=None, **kw) -> dict:
        return json.loads(self._request(path, body, **kw).decode("utf-8", "replace"))

    # ------------------------------------------------------------ 服务
    def system_stats(self) -> dict:
        # 页面加载探活不宜长时间阻塞，最多快速重试一次
        return self._json("/system_stats", timeout=10,
                          retries=min(1, self.request_retries - 1), retry_delay=1.0)

    def queue_state(self, *, timeout: float | None = None) -> dict:
        """查队列；网络失败抛 ComfyError，由 wait() 决定重试（不能吞成「队列为空」）。"""
        return self._json("/queue", timeout=timeout or max(10, self.poll_request_timeout))

    # ------------------------------------------------------------ 素材上传
    def upload_image(self, src: Path | bytes, filename: str | None = None,
                     subfolder: str | None = None, *, log: LogFn | None = None,
                     should_stop: Callable[[], bool] | None = None) -> str:
        """上传到 ComfyUI 的 input 目录，返回 LoadImage 可用的 `子目录/文件名`。"""
        if isinstance(src, Path):
            content = src.read_bytes()
            filename = filename or src.name
        else:
            content = src
            filename = filename or "image.png"
        # 文件名保持 ASCII，避免 ComfyUI 侧路径编码问题
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        sub = self.subfolder if subfolder is None else subfolder
        boundary = "----spritemaker" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(safe)[0] or "application/octet-stream"

        def field(name: str, value: str) -> bytes:
            return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    f"{value}\r\n").encode("utf-8")

        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                f"filename=\"{safe}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
        body += content + b"\r\n"
        body += field("overwrite", "true") + field("subfolder", sub) + field("type", "input")
        body += f"--{boundary}--\r\n".encode("utf-8")
        raw = self._request(
            "/upload/image", raw=body, timeout=max(self.timeout, 180),
            extra_headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            retries=self.request_retries, log=log, should_stop=should_stop,
        )
        info = json.loads(raw.decode("utf-8", "replace"))
        name = info.get("name") or safe
        got_sub = info.get("subfolder") or ""
        return f"{got_sub}/{name}" if got_sub else name

    # ------------------------------------------------------------ 提交 / 等待
    def _queued_prompt_id(self) -> str | None:
        """提交超时应答丢失时，从队列里找回本 client_id 已排入的任务，避免重复生成。"""
        try:
            q = self.queue_state()
        except ComfyError:
            return None
        for key in ("queue_running", "queue_pending"):
            for row in q.get(key) or []:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                meta = row[3] if len(row) > 3 else None
                cid = meta.get("client_id") if isinstance(meta, dict) else None
                if cid and cid == self.client_id:
                    return str(row[1])
        return None

    def queue_prompt(self, prompt: dict) -> str:
        payload = {"prompt": prompt, "client_id": self.client_id}
        last: ComfyError | None = None
        for attempt in range(1, self.submit_retries + 1):
            if attempt > 1:
                existing = self._queued_prompt_id()
                if existing:
                    self.log(f"检测到任务 {existing} 已在 ComfyUI 队列中"
                             "（上次提交可能只是响应丢失），直接等待该任务")
                    return existing
            try:
                data = self._json("/prompt", payload, timeout=60)
            except ComfyError as exc:
                last = exc
                if attempt >= self.submit_retries or not _is_retryable(exc):
                    raise
                wait = self.request_retry_delay * attempt
                self.log(f"提交任务失败（{str(exc)[:120]}），{wait:.0f}s 后第 {attempt + 1} 次尝试")
                self._sleep(wait, self.should_stop)
                continue
            if data.get("node_errors"):
                raise ComfyError("工作流校验未通过：" + json.dumps(data["node_errors"], ensure_ascii=False)[:600])
            return data["prompt_id"]
        raise last or ComfyError("提交 ComfyUI 任务失败")

    def history(self, prompt_id: str, *, timeout: float | None = None) -> dict | None:
        """查单个任务的 history；网络失败向上抛，由 wait() 统一重试。"""
        return self._json(f"/history/{prompt_id}",
                          timeout=timeout or self.poll_request_timeout).get(prompt_id)

    def interrupt(self, prompt_ids: list[str]) -> list[str]:
        """取消本任务在 ComfyUI 侧的执行，返回可写进日志的动作说明。

        只按 prompt_id 处理本服务提交的任务：先删排队项，若正在执行再发
        /interrupt；不会打断用户自己在 ComfyUI 界面里提交的其它任务。
        """
        ids = {str(x) for x in prompt_ids or []}
        if not ids:
            return []
        q = self.queue_state(timeout=5)
        running = {str(row[1]) for row in (q.get("queue_running") or []) if len(row) > 1}
        pending = {str(row[1]) for row in (q.get("queue_pending") or []) if len(row) > 1}
        out: list[str] = []
        todo = sorted(ids & pending)
        if todo:
            self._request("/queue", {"delete": todo}, timeout=6, retries=0)
            out.append(f"已从 ComfyUI 队列移除本任务的 {len(todo)} 个排队项")
        if ids & running:
            self._request("/interrupt", {}, timeout=6, retries=0)
            out.append("已中断 ComfyUI 正在执行的任务")
        if not out:
            out.append("本任务在 ComfyUI 队列中已无待执行项")
        return out

    def wait(self, prompt_id: str, *, log: LogFn = _noop, timeout: float | None = None,
             should_stop: Callable[[], bool] | None = None) -> dict:
        """轮询到完成，返回 history 条目。

        网络查询失败 ≠ 任务丢失：只有连续多次「查询成功但队列里没有该任务」才提前报错；
        连接抖动会一直退避重试到 job_timeout，不会让整条流水线白跑。
        """
        total = float(timeout or self.job_timeout)
        deadline = time.monotonic() + total
        self.log, self.should_stop = log, should_stop
        missing = 0            # 连续「成功查询但队列中不存在」的次数
        net_errors = 0         # 当前连续网络失败次数
        total_errors = 0
        last_error = ""

        while time.monotonic() < deadline:
            if should_stop and should_stop():
                raise ComfyError("任务已取消")
            ok = True
            hist = None
            try:
                hist = self.history(prompt_id)
            except ComfyError as exc:
                ok, last_error = False, str(exc)

            if hist:
                status = hist.get("status") or {}
                state = status.get("status_str") or ""
                if state == "error":
                    raise ComfyError("ComfyUI 执行出错：" + _extract_error(status))
                if hist.get("outputs"):
                    if net_errors:
                        log(f"ComfyUI 连接已恢复（此前失败 {net_errors} 次），取到产物")
                    return hist
                if status.get("completed") is True:
                    raise ComfyError("任务完成但没有输出文件，请检查工作流保存节点")

            if ok:
                try:
                    q = self.queue_state()
                except ComfyError as exc:
                    ok, last_error = False, str(exc)
                else:
                    ids = {str(x[1]) for x in (q.get("queue_running") or []) if len(x) > 1}
                    ids |= {str(x[1]) for x in (q.get("queue_pending") or []) if len(x) > 1}
                    if prompt_id in ids:
                        missing = 0
                    else:
                        missing += 1
                        if missing >= 4:
                            # 队列消失前最后确认一次产物（服务端可能刚写完 history）
                            try:
                                last = self.history(prompt_id) or {}
                            except ComfyError:
                                last = {}
                            if last.get("outputs"):
                                return last
                            raise ComfyError("任务已从 ComfyUI 队列消失（可能被中断或服务端重启）")

            if not ok:
                net_errors += 1
                total_errors += 1
                backoff = max(self.poll, self.poll_error_backoff)
                if net_errors <= 3 or net_errors % 10 == 0:
                    log(f"轮询 ComfyUI 失败（第 {net_errors} 次）：{last_error[:150]}；"
                        f"{backoff:.0f}s 后自动重试，任务继续等待")
                if self.poll_max_errors and net_errors > self.poll_max_errors:
                    raise ComfyError(
                        f"轮询 ComfyUI 连续失败 {net_errors} 次（poll_max_errors="
                        f"{self.poll_max_errors}）：{last_error[:200]}")
            elif net_errors:
                log(f"ComfyUI 连接已恢复（此前失败 {net_errors} 次），继续等待任务 {prompt_id}")
                net_errors = 0

            self._sleep(self.poll if ok else max(self.poll, self.poll_error_backoff), should_stop)
        raise ComfyError(f"等待 ComfyUI 超时（{int(total)} 秒；期间网络失败 {total_errors} 次，"
                         f"最后一次：{last_error[:150]}）")

    # ------------------------------------------------------------ 产物下载
    @staticmethod
    def save_node_ids(prompt: dict) -> set[str]:
        return {nid for nid, node in prompt.items() if node.get("class_type") in SAVE_CLASSES}

    @staticmethod
    def collect_outputs(hist: dict, save_ids: set[str] | None = None,
                        exts: tuple[str, ...] | None = None) -> list[dict]:
        """只取保存节点的产物，跳过 ImageCompare 的 *_temp_ 临时图。"""
        out: list[dict] = []
        seen: set[tuple] = set()
        outputs = hist.get("outputs") or {}
        if save_ids:
            outputs = {k: v for k, v in outputs.items() if k in save_ids} or outputs
        for _nid, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            for _key, items in node_out.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or not item.get("filename"):
                        continue
                    fn = str(item["filename"])
                    if "_temp_" in fn or fn.startswith("comfy.compare"):
                        continue
                    if exts and not fn.lower().endswith(exts):
                        continue
                    sig = (fn, item.get("subfolder", ""), item.get("type", "output"))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    out.append(item)
        return out

    def fetch(self, item: dict, dest: Path, *, timeout: float = 600,
              log: LogFn = _noop) -> Path:
        query = urllib.parse.urlencode({
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        last: ComfyError | None = None
        for attempt in range(1, self.download_retries + 1):
            try:
                data = self._request("/view?" + query, timeout=timeout)
                if not data:
                    raise ComfyError(f"下载产物为空：{item['filename']}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                return dest
            except ComfyError as exc:
                last = exc
                if attempt >= self.download_retries or not _is_retryable(exc):
                    raise
                wait = self.download_retry_delay * attempt
                log(f"下载 {item['filename']} 失败（{str(exc)[:120]}），{wait:.0f}s 后第 {attempt + 1} 次重试")
                self._sleep(wait, self.should_stop)
        raise last or ComfyError(f"下载 {item['filename']} 失败")

    def run(self, prompt: dict, dest: Path, *, exts: tuple[str, ...] | None = None,
            log: LogFn = _noop, timeout: float | None = None,
            should_stop: Callable[[], bool] | None = None) -> dict:
        """一站式：提交 → 等待 → 下载保存节点产物到 dest（文件或多文件目录）。

        dest 带后缀时视为单文件路径；多产物自动追加 _1/_2。返回任务信息字典。
        """
        self.log, self.should_stop = log, should_stop
        save_ids = self.save_node_ids(prompt)
        log(f"提交 ComfyUI 任务（{len(prompt)} 节点，保存节点 {','.join(sorted(save_ids)) or '无'}）")
        pid = self.queue_prompt(prompt)
        if self.on_prompt:
            try:
                self.on_prompt(pid)
            except Exception:  # noqa: BLE001 - 登记失败不能影响出图
                pass
        log(f"已排队 prompt_id={pid}，等待生成…")
        hist = self.wait(pid, log=log, timeout=timeout, should_stop=should_stop)
        items = self.collect_outputs(hist, save_ids, exts)
        if not items:
            raise ComfyError("任务完成，但没有可下载的图片/视频产物")
        files: list[Path] = []
        for i, item in enumerate(items):
            suffix = Path(str(item["filename"])).suffix
            if dest.suffix:
                target = dest if i == 0 else dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
            else:
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / f"{dest.stem or 'output'}_{i:03d}{suffix}"
            self.fetch(item, target, log=log)
            files.append(target)
            log(f"下载产物 → {target.name}（{target.stat().st_size // 1024} KB）")
        return {"prompt_id": pid, "files": [str(f) for f in files]}


def _extract_error(status: dict) -> str:
    for msg in status.get("messages") or []:
        if isinstance(msg, list) and len(msg) >= 2 and msg[0] in ("execution_error", "execution_interrupted"):
            detail = msg[1] if isinstance(msg[1], dict) else {}
            node = detail.get("node_type") or detail.get("node_id") or ""
            reason = detail.get("exception_message") or detail.get("exception_type") or ""
            return f"{node}: {reason}".strip(": ")
    return str(status.get("status_str") or "未知错误")


def _format_node_error(detail: str) -> str:
    try:
        data = json.loads(detail)
    except Exception:
        return detail[:500]
    parts: list[str] = []
    for _nid, info in (data.get("node_errors") or {}).items():
        cls = info.get("class_type", "?")
        for err in info.get("errors", [])[:2]:
            kind = err.get("type") or ""
            msg = str(err.get("message") or "")
            text = str(err.get("details") or "")
            if kind == "value_not_in_list":
                m = re.search(r"'([^']+)' not in", text)
                parts.append(f"缺少模型文件「{m.group(1) if m else text}」（{cls}）")
            else:
                parts.append(f"[{cls}] {msg} {text}".strip())
    if not parts:
        err = data.get("error")
        parts.append(str(err.get("message") if isinstance(err, dict) else err or data)[:300])
    return "；".join(parts[:3]) or detail[:300]
