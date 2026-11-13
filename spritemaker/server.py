"""本地 Web 服务：REST API + 静态页面 + 后台流水线任务。

只用 Python 标准库 http.server，不引入 Flask/FastAPI。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import threading
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, ffmpeg_tools, prompts, workflows
from .comfy import ComfyClient
from .pipeline import Pipeline
from .project import (Project, ProjectError, create_project, get_project,
                      list_projects, delete_project, valid_name)

JOBS: dict[str, dict] = {}
JOB_RUNNERS: dict[str, "JobRunner"] = {}
JOBS_LOCK = threading.Lock()


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=None).encode("utf-8")


class ApiError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


class JobRunner:
    """后台线程执行流水线，日志同时写内存和项目 logs/ 目录。"""

    def __init__(self, project: Project, stage: str, force: bool = True, op: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.project = project
        self.stage = stage
        self.op = op or None
        self.job: dict = {
            "id": self.id, "project": project.name, "stage": stage,
            "status": "running", "logs": [], "error": None, "result": None,
            "cancel": False, "started": __import__("time").time(),
        }
        with JOBS_LOCK:
            JOBS[self.id] = self.job
            JOB_RUNNERS[self.id] = self
        self.log_file = project.dir / "logs" / f"{self.id}.log"
        self.thread = threading.Thread(target=self._work, args=(force,), daemon=True)

    def log(self, msg: str) -> None:
        line = str(msg)
        with JOBS_LOCK:
            self.job["logs"].append(line)
            if len(self.job["logs"]) > 800:
                del self.job["logs"][:200]
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def start(self) -> str:
        self.thread.start()
        return self.id

    def _note_prompt(self, pid: str) -> None:
        """登记本任务提交给 ComfyUI 的 prompt_id，取消时据此精准中断。"""
        with JOBS_LOCK:
            ids = self.job.setdefault("prompt_ids", [])
            if pid not in ids:
                ids.append(pid)
            self.job["prompt_id"] = pid

    def cancel(self) -> dict:
        """立即置取消标志，并异步让 ComfyUI 停掉本任务（不阻塞 HTTP 响应）。"""
        with JOBS_LOCK:
            if self.job["status"] != "running":
                return {"ok": True, "already_finished": True, "interrupting": False}
            first = not self.job["cancel"]
            self.job["cancel"] = True
            ids = list(self.job.get("prompt_ids") or [])
        if not first:
            return {"ok": True, "repeated": True, "interrupting": bool(ids)}
        self.log("已请求取消：停止本地步骤…")

        def _do() -> None:
            if not ids:
                self.log("本任务尚未提交 ComfyUI，无需中断远端任务")
                return
            try:
                for line in ComfyClient().interrupt(ids):
                    self.log(line)
            except Exception as exc:  # noqa: BLE001
                self.log(f"中断 ComfyUI 失败（本地任务仍会立即停止）：{exc}")

        threading.Thread(target=_do, name=f"cancel-{self.id}", daemon=True).start()
        return {"ok": True, "interrupting": bool(ids)}

    def _work(self, force: bool) -> None:
        try:
            pipe = Pipeline(self.project, log=self.log, force=force,
                            should_stop=lambda: bool(self.job["cancel"]),
                            on_prompt=self._note_prompt)
            if self.op:
                result = pipe.run_op(self.op)
            else:
                result = pipe.run_all() if self.stage == "all" else pipe.run(self.stage)
            with JOBS_LOCK:
                self.job["status"] = "cancelled" if self.job["cancel"] else "done"
                self.job["result"] = result
        except Exception as exc:  # noqa: BLE001
            if self.job["cancel"]:
                self.log("任务已取消")
            else:
                self.log(traceback.format_exc(limit=3))
            with JOBS_LOCK:
                self.job["status"] = "cancelled" if self.job["cancel"] else "failed"
                self.job["error"] = None if self.job["cancel"] else str(exc)
        finally:
            with JOBS_LOCK:
                self.job["finished"] = __import__("time").time()


def health() -> dict:
    cfg = config.load()
    out: dict = {"comfy_url": cfg["comfy_url"], "comfy": "unknown",
                 "ffmpeg": "unknown", "workflows": {}, "errors": []}
    try:
        stats = ComfyClient().system_stats()
        out["comfy"] = "ok"
        out["comfy_version"] = (stats.get("system") or {}).get("comfyui_version", "?")
    except Exception as exc:  # noqa: BLE001
        out["comfy"] = "error"
        out["errors"].append(str(exc))
    try:
        exe = ffmpeg_tools.executable()
        out["ffmpeg"] = "ok" if exe.exists() else "missing"
        out["ffmpeg_path"] = str(exe)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(str(exc))
    try:
        wf_set = workflows.WorkflowSet()
        out["workflows"] = wf_set.check()
        out["video_workflows"] = wf_set.video_workflows()
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(str(exc))
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "SpriteMaker/1.0"

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _ensure_idle(name: str) -> None:
        """项目正在跑任务时不允许删产物 / 删项目，避免边写边删。"""
        with JOBS_LOCK:
            active = [j for j in JOBS.values()
                      if j["project"] == name and j["status"] == "running"]
        if active:
            raise ApiError(f"项目正在执行任务 {active[0]['id']}（{active[0]['stage']}），"
                           f"请等待完成或先取消")
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, _json_bytes(obj))

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"请求体不是合法 JSON：{exc}") from exc

    def log_message(self, fmt: str, *args) -> None:
        if "/api/" in (self.path or ""):
            return
        super().log_message(fmt, *args)

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path, q = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                return self._static(config.path("web_dir") / "index.html", "text/html; charset=utf-8")
            if path in ("/app.js", "/style.css", "/favicon.ico"):
                ctype = {"app.js": "application/javascript; charset=utf-8",
                         "style.css": "text/css; charset=utf-8",
                         "favicon.ico": "image/x-icon"}[path.lstrip("/")]
                return self._static(config.path("web_dir") / path.lstrip("/"), ctype)
            if path == "/file":
                return self._file(q)
            if path == "/api/health":
                return self._json(health())
            if path == "/api/presets":
                return self._json({"actions": prompts.ACTION_PRESETS})
            if path == "/api/workflows":
                return self._json({"videos": workflows.WorkflowSet().video_workflows()})
            if path == "/api/config":
                cfg = config.load()
                safe = dict(cfg)
                safe["comfy_password"] = "******" if cfg.get("comfy_password") else ""
                safe["deepseek_api_key"] = "******" if cfg.get("deepseek_api_key") else ""
                return self._json(safe)
            if path == "/api/projects":
                return self._json({"projects": list_projects()})
            m = re.fullmatch(r"/api/projects/([^/]+)", path)
            if m:
                name = urllib.parse.unquote(m.group(1))
                return self._json(get_project(name).manifest())
            if path == "/api/jobs":
                with JOBS_LOCK:
                    return self._json({"jobs": [
                        {k: v for k, v in j.items()
                         if k not in ("logs", "cancel", "prompt_id", "prompt_ids")}
                        for j in reversed(list(JOBS.values()))][:30]})
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9]+)", path)
            if m:
                with JOBS_LOCK:
                    job = JOBS.get(m.group(1))
                    if not job:
                        raise ApiError("任务不存在", 404)
                    since = int((q.get("since") or ["0"])[0])
                    out = {k: v for k, v in job.items()
                           if k not in ("logs", "cancel", "prompt_id", "prompt_ids")}
                    out["logs"] = job["logs"][since:]
                    out["log_count"] = len(job["logs"])
                    return self._json(out)
            raise ApiError("接口不存在", 404)
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.code)
        except (ProjectError, FileNotFoundError) as exc:
            self._json({"error": str(exc)}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=4)}, 500)

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._body()
            if path == "/api/config":
                cfg = config.load()
                for key in ("comfy_url", "comfy_user", "comfy_password", "comfy_subfolder",
                            "host", "port", "poll_interval", "job_timeout",
                            "comfy_timeout", "poll_request_timeout", "poll_error_backoff",
                            "poll_max_errors", "request_retries", "request_retry_delay",
                            "submit_retries", "download_retries", "download_retry_delay",
                            "prompt_optimize", "deepseek_api_key", "deepseek_model",
                            "deepseek_url", "deepseek_timeout"):
                    if key in body and not (
                            key in ("comfy_password", "deepseek_api_key") and body[key] == "******"):
                        cfg[key] = body[key]
                config.save(cfg)
                return self._json({"ok": True, "health": health()})
            if path == "/api/projects":
                params = body.get("params") or {}
                p = create_project(body.get("name", ""), params)
                return self._json(p.manifest(), 201)
            m = re.fullmatch(r"/api/projects/([^/]+)/params", path)
            if m:
                p = get_project(urllib.parse.unquote(m.group(1)))
                p.update_params(body.get("params") or body)
                return self._json(p.manifest())
            m = re.fullmatch(r"/api/projects/([^/]+)/selection", path)
            if m:
                p = get_project(urllib.parse.unquote(m.group(1)))
                return self._json({"selection": p.set_selection(body.get("files") or [])})
            m = re.fullmatch(r"/api/projects/([^/]+)/reference-image", path)
            if m:
                p = get_project(urllib.parse.unquote(m.group(1)))
                if body.get("clear"):
                    p.update_params({"reference_use_image": False, "reference_image": ""})
                    f = p.dir_of("source") / "base.png"
                    if f.exists():
                        f.unlink()
                    return self._json(p.manifest())
                raw = "".join(str(body.get("data") or "").split())
                if raw.startswith("data:") and "," in raw:
                    raw = raw.split(",", 1)[1]
                try:
                    data = base64.b64decode(raw, validate=True)
                except Exception as exc:  # noqa: BLE001
                    raise ApiError(f"图片数据不是合法 base64：{exc}") from exc
                return self._json(p.save_reference_image(data))
            m = re.fullmatch(r"/api/projects/([^/]+)/videos/(delete|rename|seed)", path)
            if m:
                p = get_project(urllib.parse.unquote(m.group(1)))
                filename = str(body.get("file") or "")
                if not filename:
                    raise ApiError("缺少视频文件名")
                if m.group(2) == "delete":
                    p.delete_video(filename)
                    return self._json(p.manifest())
                if m.group(2) == "seed":
                    raw = str(body.get("seed") or "").strip()
                    if not raw.isdigit():
                        raise ApiError("种子需要是非负整数")
                    return self._json({"videos": p.update_video_seed(filename, int(raw))})
                return self._json({"videos": p.rename_video(filename, str(body.get("label") or ""))})
            m = re.fullmatch(r"/api/projects/([^/]+)/keys/(delete)", path)
            if m:
                p = get_project(urllib.parse.unquote(m.group(1)))
                return self._json(p.delete_key(str(body.get("id") or "")))
            m = re.fullmatch(r"/api/projects/([^/]+)/clear", path)
            if m:
                p = get_project(urllib.parse.unquote(m.group(1)))
                stage = str(body.get("stage") or "")
                group = str(body.get("group") or "")
                self._ensure_idle(p.name)
                result = p.clear_group(stage, group)   # group 为空 = 清整步
                man = p.manifest()
                man["cleared"] = result
                return self._json(man)
            m = re.fullmatch(r"/api/projects/([^/]+)/run", path)
            if m:
                p = get_project(urllib.parse.unquote(m.group(1)))
                stage = str(body.get("stage") or "reference")
                op = body.get("op") or None
                if op is not None and not isinstance(op, dict):
                    raise ApiError("op 必须是对象")
                if op is None and stage != "all" and stage not in Pipeline.STAGE_FUNCS:
                    raise ApiError(f"未知阶段：{stage}")
                self._ensure_idle(p.name)
                # 界面不再提供「强制重跑」开关：每次执行都覆盖该步已有产物
                force = bool(body.get("force", True))
                runner = JobRunner(p, stage, force, op)
                return self._json({"job_id": runner.start()}, 202)
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9]+)/cancel", path)
            if m:
                with JOBS_LOCK:
                    runner = JOB_RUNNERS.get(m.group(1))
                if not runner:
                    raise ApiError("任务不存在", 404)
                return self._json(runner.cancel())
            raise ApiError("接口不存在", 404)
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.code)
        except (ProjectError, FileNotFoundError) as exc:
            self._json({"error": str(exc)}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=4)}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        m = re.fullmatch(r"/api/projects/([^/]+)", urllib.parse.urlparse(self.path).path)
        try:
            if not m:
                raise ApiError("接口不存在", 404)
            name = urllib.parse.unquote(m.group(1))
            self._ensure_idle(valid_name(name))
            delete_project(name)
            self._json({"ok": True})
        except (ApiError, ProjectError) as exc:
            self._json({"error": str(exc)}, getattr(exc, "code", 400))

    # ------------------------------------------------------------ 文件
    def _static(self, path: Path, ctype: str) -> None:
        if not path.exists():
            raise ApiError(f"缺少静态文件 {path.name}", 404)
        self._send(200, path.read_bytes(), ctype)

    def _file(self, q: dict) -> None:
        name = (q.get("project") or [""])[0]
        rel = (q.get("path") or [""])[0]
        if not name or not rel:
            raise ApiError("缺少 project / path 参数")
        p = get_project(name)
        target = p.resolve(rel)
        if not target.is_file():
            raise ApiError("文件不存在", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = target.read_bytes()
        rng = self.headers.get("Range")
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else len(data) - 1
            end = min(end, len(data) - 1)
            chunk = data[start:end + 1]
            self._send(206, chunk, ctype, {
                "Content-Range": f"bytes {start}-{end}/{len(data)}",
                "Accept-Ranges": "bytes",
            })
            return
        self._send(200, data, ctype, {"Accept-Ranges": "bytes"})


def serve(host: str | None = None, port: int | None = None) -> None:
    cfg = config.load()
    host = host or str(cfg["host"])
    port = int(port or cfg["port"])
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"SpriteMaker Web UI: http://{host}:{port}")
    print(f"ComfyUI: {cfg['comfy_url']}  项目目录: {config.path('projects_dir')}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
