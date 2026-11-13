"""生成流水线编排。

reference(单张正面立绘) → keyframe(各方向单帧) → video(首帧+可选尾帧) →
frames(ffmpeg 切帧) → sprites(重绘/抠图去背景) → export(预览与输出)
每个阶段都可单独执行；Web 界面每次执行都覆盖该步已有产物（force=True），
CLI 不加 --force 时则跳过已存在的产物，便于断点续跑。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Callable

from . import config, ffmpeg_tools, imaging, llm, prompts, workflows
from .comfy import ComfyClient, ComfyError
from .project import Project

MAX_SEED = 2 ** 62

# 第 ② 步的输出画布固定成 1:1 正方形：精灵帧本来就是方图，正 / 侧 / 背
# 各方向同一个尺寸，转视频后切出来的帧不用再二次缩放。
KEYFRAME_SIZE = (1024, 1024)

# 扩画布后视频像素总数跟着同比放大，但有个天花板：再往上模型更容易超时、
# 显存也吃不住，1.5 MP 已经远高于默认的 0.4 MP。
MAX_VIDEO_MP = 1.5

# 去背景原图「参考尺寸」缓存的版本号：算口径一改就 +1，旧缓存自动作废
REF_CACHE_VERSION = 2


def _noop(_: str) -> None:
    pass


class PipelineError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, project: Project, *, log: Callable[[str], None] = _noop,
                 force: bool = False, should_stop: Callable[[], bool] | None = None,
                 on_prompt: Callable[[str], None] | None = None):
        self.p = project
        self.log = log
        self.force = force
        self.should_stop = should_stop
        self.comfy = ComfyClient()
        self.comfy.on_prompt = on_prompt
        self.wf = workflows.WorkflowSet()
        self.cfg = config.load()
        self._base_seed: int | None = None

    # ------------------------------------------------------------ 工具
    def _check_stop(self) -> None:
        if self.should_stop and self.should_stop():
            raise PipelineError("任务已取消")

    def _seed(self, index: int = 0) -> int:
        if self._base_seed is None:
            saved = int(self.p.params.get("seed") or 0)
            self._base_seed = saved or random.randrange(1, MAX_SEED)
        return (self._base_seed + index * 7919) % MAX_SEED

    def _opt_prompt(self, kind: str, text: str) -> str:
        """用 DeepSeek 把内置模板改写一版（可关、可失败、结果按原文缓存）。

        缓存按「阶段 + 原文」哈希存在项目目录里：同一份提示词只花一次钱，
        也保证重复执行同一阶段时喂给模型的文字完全一致，不会因为每次改写
        结果不同而把出图搅得忽好忽坏。
        """
        cfg = self.cfg
        if not text or not cfg.get("prompt_optimize"):
            return text
        key = hashlib.sha1(f"{kind}\n{text}".encode("utf-8")).hexdigest()
        cache_file = self.p.dir / ".prompt_cache.json"
        cache: dict = {}
        if cache_file.is_file():
            try:
                raw = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cache = raw
            except (OSError, ValueError):
                cache = {}
        hit = cache.get(key)
        if isinstance(hit, dict) and hit.get("out"):
            return str(hit["out"])
        try:
            out = llm.optimize(
                text, api_key=str(cfg.get("deepseek_api_key") or ""), kind=kind,
                url=str(cfg.get("deepseek_url") or llm.DEFAULT_URL),
                model=str(cfg.get("deepseek_model") or llm.DEFAULT_MODEL),
                timeout=float(cfg.get("deepseek_timeout") or 40), log=self.log)
        except llm.LLMError as exc:
            self.log(f"  ! 提示词优化跳过（{exc}），直接用内置模板")
            return text
        cache[key] = {"kind": kind, "out": out, "src": text}
        try:
            cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError:
            pass
        return out

    @staticmethod
    def _stamp() -> str:
        return time.strftime("%H%M%S")

    def _new_video_stem(self, out: Path, action_key: str, dir_key: str) -> str:
        base = f"{action_key}_{dir_key}_{time.strftime('%Y%m%d_%H%M%S')}"
        stem, n = base, 2
        while (out / f"{stem}.mp4").exists() or (out / f"{stem}.webm").exists():
            stem = f"{base}_{n}"
            n += 1
        return stem

    def _upload(self, path: Path, tag: str) -> str:
        self.log(f"上传素材 {path.name} → ComfyUI")
        return self.comfy.upload_image(path, filename=f"sm_{self.p.name}_{tag}_{self._stamp()}.png",
                                       log=self.log, should_stop=self.should_stop)

    def _generate_image(self, wf: dict, dest: Path, label: str, *,
                        force_here: bool = False) -> Path:
        if dest.exists() and not (self.force or force_here):
            self.log(f"跳过已存在的 {dest.name}（需要覆盖就重新执行本步，CLI 加 --force）")
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        info = self.comfy.run(wf, dest, exts=(".png", ".jpg", ".webp"), log=self.log,
                              should_stop=self.should_stop)
        self.p.add_job({"stage": label, "prompt_id": info["prompt_id"], "output": dest.name})
        return Path(info["files"][0])

    # ------------------------------------------------------------ 1. 正面设定
    def _front(self) -> Path:
        return self.p.dir_of("reference") / "front.png"

    @staticmethod
    def _file_stamp(path: Path) -> str:
        st = path.stat()
        return f"{st.st_mtime_ns}-{st.st_size}"

    @staticmethod
    def _action_of(params: dict) -> tuple[str, str]:
        """按当前动作描述推出 (文件名片段, 中文名)。"""
        motion = prompts.clean(params.get("video_motion", ""))
        for item in prompts.ACTION_PRESETS:
            if item["motion"] == motion:
                return item["key"], item["label"]
        return "custom", "自定义动作"

    def reference(self) -> dict:
        """生成**一张正面全身立绘**：全流程的角色基准，其余视角都由它派生。"""
        p, params = self.p, self.p.params
        out = p.dir_of("reference")
        w = int(params.get("width") or 768)
        h = int(params.get("height") or 1024)
        negative = prompts.negative_for(params)
        bg_mode = prompts.background_mode(params)

        # 可选：以用户上传的底图做图生图生成正面立绘；未勾选 / 底图缺失时纯文生图
        rel = str(params.get("reference_image") or "")
        base: Path | None = None
        if params.get("reference_use_image") and rel:
            candidate = p.resolve(rel)
            if candidate.is_file():
                base = candidate
            else:
                self.log(f"已勾选底图图生图，但找不到 {rel}，本次回退文生图")
        if params.get("reference_use_image") and not rel:
            self.log("已勾选底图图生图，但还没有上传底图，本次回退文生图")

        if base is not None:
            text = prompts.front_view_from_image(params)
            self.log(f"正面立绘：以上传底图 {base.name} 做图生图（{w}×{h}）")
        else:
            text = prompts.front_view(params)
            self.log(f"正面立绘：文生图生成（{w}×{h}，{bg_mode} 背景）")

        dest = out / "front.png"
        prompt_file = out / "front.prompt.txt"
        text = self._opt_prompt("front", text)      # DeepSeek 改写；关掉 / 失败就用原模板
        old = prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else ""
        changed = prompts.clean(old) != prompts.clean(text)
        if changed and dest.exists() and not self.force:
            self.log("提示词 / 底图与上次不同，重新生成正面立绘")
        prompt_file.write_text(text, encoding="utf-8")

        if base is not None:
            image_name = self._upload(base, "front")
            wf = self.wf.i2i(image_name=image_name, prompt=text, negative=negative,
                             seed=self._seed(0), prefix=f"sm_{p.name}_front",
                             width=w, height=h)
        else:
            wf = self.wf.t2i(prompt=text, negative=negative, width=w, height=h,
                             seed=self._seed(0), prefix=f"sm_{p.name}_front")
        self._generate_image(wf, dest, "reference/front", force_here=changed)
        info = imaging.alpha_info(dest)
        self.log(f"正面立绘已保存 {dest.name}：{info['width']}×{info['height']}")

        # 旧版三视图（一张图并排三个视角）已废弃，留着只会混淆
        for stale_name in ("three_view.png", "three_view.prompt.txt"):
            stale = out / stale_name
            if stale.is_file():
                try:
                    stale.unlink()
                    self.log(f"清理旧版三视图文件 {stale_name}")
                except OSError:
                    pass
        return {"file": dest.name, "width": info["width"], "height": info["height"],
                "source": base.name if base else None,
                "background_mode": bg_mode, "seed": self._base_seed}

    # ------------------------------------------------------------ 2. 单帧
    def _key_specs(self, params: dict) -> list[dict]:
        """第 ② 步要生成哪些单帧：默认四个方向，可追加自定义方向。"""
        dirs: list[str] = []
        for raw in params.get("frame_directions") or list(prompts.VIEW_ORDER):
            view = prompts.norm_view(raw)        # 老项目的 side 折算成 right
            if view and view not in dirs:
                dirs.append(view)
        if not dirs:
            dirs = ["front"]
        specs = [{"id": d, "label": prompts.VIEW_LABELS[d], "view": d, "view_note": ""}
                 for d in dirs]
        used = {s["id"] for s in specs}
        for item in params.get("frame_extra_views") or []:
            if not isinstance(item, dict):
                continue
            label = prompts.clean(item.get("label", ""))
            if not label:
                continue
            cid = re.sub(r"[^a-z0-9_]+", "_", str(item.get("id") or label).lower()).strip("_")
            cid = f"view_{cid}" if cid else f"view{len(specs) + 1}"
            while cid in used:
                cid = f"{cid}_2"
            used.add(cid)
            # 追加方向不在默认四向里，只能以图中的角色为原型重新转一个角度出来
            base = prompts.norm_view(item.get("from")) or "front"
            specs.append({"id": cid, "label": label, "view": "",
                          "from": base,
                          "view_note": prompts.clean(item.get("prompt", ""))})
        return specs

    def _render_frame(self, src: Path, dest: Path, text: str, negative: str, tag: str,
                      *, size: tuple[int, int] | None = None, seed: int | None = None) -> Path:
        """图生图：src 当输入图，结果写到 dest。

        派生视角的输入是正面立绘，追加细节的输入是当前这张单帧，
        所以输入和输出要分开传，不能像以前那样原地覆盖。
        size 为空时跟随输入图尺寸；指定时用它当输出画布。
        """
        image_name = self._upload(src, f"kf_{tag}"[:60])
        w, h = size or (0, 0)
        wf = self.wf.i2i(image_name=image_name, prompt=text, negative=negative,
                         seed=self._seed(200 + len(self.p.keys())) if seed is None else seed,
                         prefix=f"sm_{self.p.name}_kf_{tag}"[:90], width=w, height=h)
        return self._generate_image(wf, dest, f"keyframe/{tag}", force_here=True)

    def keyframe(self) -> dict:
        """拿第 ① 步的正面立绘派生各方向的**单帧**立绘。

        正面直接复用①的成品（本来就是同一张），背面 / 左侧 / 右侧 / 追加方向都由
        这张正面图图生图「转身」得到：一张正面图构图唯一、信息完整，模型只
        需要转视角；不再切图，也不会被三视图的排版带跑。
        """
        p, params = self.p, self.p.params
        front = self._front()
        if not front.is_file():
            raise PipelineError("还没有正面立绘，请先在 ① 生成角色正面")

        out = p.dir_of("keyframe")
        bg = prompts.video_background(params)
        stamp = self._file_stamp(front)
        meta = p.keys_meta()
        old = {k.get("id"): k for k in (meta.get("keys") or []) if isinstance(k, dict)}

        specs = self._key_specs(params)
        # 老项目里的「侧面」已归一成「右侧」，写回参数，前端四个复选框才对得上
        cur = [d for d in (params.get("frame_directions") or []) if d]
        norm = [prompts.norm_view(d) for d in cur]
        norm = [d for d in norm if d]
        if norm and norm != cur:
            p.update_params({"frame_directions": norm})
        edits = params.get("frame_edit_prompts") or {}
        negative = prompts.negative_for(
            params, "多个角色, 两个人物, 并排视图, 三视图, 分屏, 画面分割线, "
                    "文字标签, 水印, 缺失武器, 武器变形")
        ref_changed = bool(meta.get("sheet_stamp")) and meta.get("sheet_stamp") != stamp
        keys: list[dict] = []
        for spec in specs:
            self._check_stop()
            note = (prompts.clean(edits.get(spec["id"], ""))
                    or prompts.clean(old.get(spec["id"], {}).get("edit_prompt", "")))
            dest = out / f"{spec['id']}.png"
            sent = str(old.get(spec["id"], {}).get("prompt") or "")   # 跳过时沿用上次的提示词
            # 正面的底图是第①步的成品；其余方向都以刚生成的正面单帧为底图，
            # 正面还没生成时退回①的成品
            src = front if spec["id"] == "front" else out / "front.png"
            if not src.is_file():
                src = front
            # ① 是 4:3、② 是 1:1：比例不同就不能直接复制，得重画成方图
            reuse = spec["id"] == "front" and imaging.same_aspect(front, KEYFRAME_SIZE)
            if not (self.force or ref_changed or not dest.exists()):
                self.log(f"[单帧/{spec['label']}] 跳过已存在的 {dest.name}")
            elif reuse:
                # 比例一致时正面就是①那张图本身，直接复制，省一次生成也保证完全一致
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(front, dest)
                if not sent:
                    prompt_file = p.dir_of("reference") / "front.prompt.txt"
                    sent = (prompt_file.read_text(encoding="utf-8").strip()
                            if prompt_file.is_file() else "")
                self.log(f"[单帧/正面] 直接复用第①步的正面立绘")
            else:
                if spec["id"] == "front":
                    self.log(f"[单帧/正面] ①是横版、单帧改方图，重画一张 {KEYFRAME_SIZE[0]}×{KEYFRAME_SIZE[1]}")
                else:
                    self.log(f"[单帧/{spec['label']}] 由正面图派生，输入 {src.name}")
                base_text = prompts.frame_view(params, spec["label"], spec["view"],
                                               spec.get("view_note", ""), note)
                text = self._opt_prompt(f"frame:{spec['id']}", base_text)
                if prompts.has_negation(text):
                    # 第②步的提示词只写「要什么」，改写结果里冒否定词就用内置精简版
                    self.log("  ! 改写结果里带否定句，这张改用内置精简提示词")
                    text = base_text
                sent = text
                self._render_frame(src, dest, text, negative, spec["id"], size=KEYFRAME_SIZE)
                # 模型偶尔还是画出多个人物或漏道具，数一下人物数、多了换种子重来
                got = imaging.count_subjects(dest, bg)
                for attempt in range(1, 3):
                    if got <= 1:
                        break
                    self.log(f"[单帧/{spec['label']}] 输出里数到 {got} 个角色，"
                             f"换种子重试（第 {attempt} 次）")
                    self._render_frame(src, dest, text, negative, spec["id"],
                                       size=KEYFRAME_SIZE,
                                       seed=self._seed(9000 + attempt * 131))
                    got = imaging.count_subjects(dest, bg)
                if got > 1:
                    self.log(f"  ! {spec['label']} 仍然是 {got} 个人物，"
                             f"可在列表里点「重绘」再试一次")
            keys.append({
                "id": spec["id"], "label": spec["label"], "view": spec["view"],
                "file": dest.name, "view_note": spec.get("view_note", ""),
                "edit_prompt": note, "prompt": sent,
            })

        # 取消勾选的方向：图也一起清掉，避免目录里留下看不见的孤儿文件
        keep = {k["id"] for k in keys}
        for kid, item in old.items():
            if kid in keep:
                continue
            f = out / str(item.get("file") or f"{kid}.png")
            if f.is_file() and f.parent == out:
                try:
                    f.unlink()
                    self.log(f"[单帧] 清理已取消方向的 {f.name}")
                except OSError:
                    pass

        # 老项目留下的 panels 目录 / 切分结果已经没用了，顺手清掉
        stale = out / "panels"
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        p.save_keys({
            "sheet": f"01_reference/{front.name}",
            "sheet_stamp": stamp,
            "method": "i2i",
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "keys": keys,
        })
        # 第 ③ 步的选择跟随单帧清单：选中的没了就回落到第一张，尾帧无效则退回「同首帧」
        ids = {k["id"] for k in keys}
        patch = {}
        if str(params.get("first_frame") or "") not in ids:
            patch["first_frame"] = keys[0]["id"] if keys else ""
        last = str(params.get("last_frame") or "")
        if last and last not in ids:
            patch["last_frame"] = ""
        if patch:
            p.update_params(patch)
        self.log(f"单帧完成：{len(keys)} 张（" + "，".join(x["label"] for x in keys) + "）")
        return {"count": len(keys),
                "keys": [{"id": x["id"], "label": x["label"], "file": x["file"]} for x in keys]}

    def run_edit(self, op: dict) -> dict:
        """给某一张单帧追加细节重绘（不重新派生整批）。"""
        p = self.p
        kid = str(op.get("key") or "")
        note = prompts.clean(op.get("prompt", ""))
        if not note:
            raise PipelineError("请先填写要增加的细节")
        meta = p.keys_meta()
        item = next((k for k in (meta.get("keys") or []) if k.get("id") == kid), None)
        if item is None:
            raise PipelineError("找不到这张单帧")
        out = p.dir_of("keyframe")
        path = out / str(item.get("file") or "")
        if not path.is_file():
            raise PipelineError(f"缺少 {path.name}，请重新执行第 ② 步生成单帧")
        negative = prompts.negative_for(p.params, "多个角色, 并排视图, 三视图, 画面分割线, 文字标签")
        text = prompts.frame_edit(p.params, item.get("label") or kid, note)
        self.log(f"[单帧/{item.get('label') or kid}] 重绘：{note[:70]}")
        # 追加细节以当前这张单帧为底图（不是正面立绘），改完仍是同一构图
        self._render_frame(path, path, text, negative, kid)
        item["edit_prompt"] = note
        item["prompt"] = text          # 界面上看到的就是这次实际发出去的提示词
        p.save_keys(meta)
        return {"key": kid, "prompt": note, "file": path.name}

    def run_op(self, op: dict) -> dict:
        """编辑类操作：沿用 keyframe 的阶段状态，方便前端统一轮询进度。"""
        self._check_stop()
        self.p.stage_begin("keyframe")
        self.log("===== 单帧重绘开始 =====")
        try:
            result = self.run_edit(op)
            self.p.stage_done("keyframe", result=result)
            self.log("===== 单帧重绘完成 =====")
            return result
        except (ComfyError, PipelineError, workflows.WorkflowError,
                ffmpeg_tools.FFmpegError, RuntimeError, OSError) as exc:
            self.p.stage_fail("keyframe", str(exc))
            self.log(f"!!!!! 单帧重绘失败 —— {exc}")
            raise

    # ------------------------------------------------------------ 3. 视频
    def video(self) -> dict:
        p, params = self.p, self.p.params
        keys = p.keys()
        if not keys:
            raise PipelineError("还没有单帧，请先在 ② 生成各方向单帧立绘")

        def pick(field: str, default: dict | None = None) -> dict:
            kid = str(params.get(field) or "")
            hit = next((k for k in keys if k["id"] == kid), None)
            if hit is not None:
                return hit
            if kid:
                self.log(f"第 ③ 步选的 {field}={kid} 已不存在，改用{'首帧' if default else '第一张单帧'}")
            return default or keys[0]

        first_key = pick("first_frame")
        # 没选尾帧 = 首帧自己当尾帧，视频才能无缝循环（这也是最常用的情况）
        last_key = pick("last_frame", first_key)
        loop = str(first_key.get("file")) == str(last_key.get("file"))
        kf_dir = p.dir_of("keyframe")
        first_src = kf_dir / str(first_key["file"])
        last_src = kf_dir / str(last_key["file"])
        if not first_src.is_file() or not last_src.is_file():
            raise PipelineError("首尾帧图缺失，请重新执行第 ② 步生成单帧")

        out = p.dir_of("video")
        # 每次点击都生成一段新的视频：不同动作 / 不同首尾帧 / 不同工作流会并存，
        # 由「切帧」页选择具体切哪一段。因此这里绝不能因为已有视频就跳过。
        action_key, action = self._action_of(params)
        dir_key = (str(first_key["id"]) if loop
                   else f"{first_key['id']}_to_{last_key['id']}")
        stem = self._new_video_stem(out, action_key, dir_key)
        workflow = str(params.get("video_workflow") or "").strip()
        wf_info = {x["file"]: x for x in self.wf.video_workflows()}
        if workflow and workflow in wf_info and not wf_info[workflow]["compatible"]:
            raise PipelineError(f"视频工作流「{wf_info[workflow]['name']}」不兼容：{wf_info[workflow]['reason']}")
        wf_name = (wf_info.get(workflow, {}).get("name") if workflow else
                   wf_info.get(self.cfg["workflows"]["i2v"], {}).get("name")) or workflow or "默认视频工作流"
        created = time.strftime("%Y-%m-%d %H:%M:%S")
        dir_label = (str(first_key.get("label") or first_key["id"]) if loop else
                     f"{first_key.get('label') or first_key['id']}→{last_key.get('label') or last_key['id']}")
        custom = prompts.clean(params.get("video_label", ""))
        label = custom or f"{action} · {dir_label} · {time.strftime('%m-%d %H:%M:%S')}"
        if custom:
            used = sum(1 for r in p.video_records() if str(r.get("label", "")).startswith(custom))
            if used:
                label = f"{custom} #{used + 1}"

        silent = bool(params.get("video_silent_audio", True))
        text = prompts.clean(params.get("video_prompt", "")) or prompts.video(params, loop=loop)
        # H3 会原生生成音轨，提示词里不写声音它就自己补配乐和杂音；这里统一收尾，
        # 自定义提示词也走同一道，除非用户自己已经写了「声音：」。
        text = prompts.with_silent_audio(text, params)
        (out / f"{stem}.prompt.txt").write_text(text, encoding="utf-8")
        # 视频没有 Alpha 通道：ComfyUI LoadImage 会把透明区取成纯黑，
        # 黑底 + 黑边会让模型脑补出紫色背景和暗色描边。这里统一铺纯色。
        bg = prompts.video_background(params)
        self.log(f"新增视频段「{label}」（工作流 {wf_name}），首帧「{first_key.get('label')}」"
                 f"{'、尾帧同首帧（无缝循环）' if loop else '、尾帧「' + str(last_key.get('label')) + '」（首尾过渡）'}"
                 f"，铺纯色背景 {bg}，"
                 f"{'静音（提示词写明无配乐无人声，并断开音轨）' if silent else '保留模型生成的音轨'}")
        # 视频模型只做动作、不取景：首帧里角色顶满画面时，一跳或一挥砍
        # 手脚和武器就出画。做法是用背景色往外扩画布、角色原样不动，
        # 视频分辨率跟着扩后的画布比例走——角色清晰度不降，动作也有空间。
        def pct(name: str) -> float:
            try:
                return max(0.0, min(45.0, float(params.get(f"video_pad_{name}", 20)))) / 100.0
            except (TypeError, ValueError):
                return 0.20

        pad = (pct("left"), pct("top"), pct("right"), pct("bottom"))
        base_mp = float(params.get("video_megapixels") or 0.4)
        box = imaging.alpha_info(first_src)
        sw, sh = box["width"], box["height"]
        canvas_w, canvas_h = imaging.canvas_size(first_src, pad)
        # 画布变大后像素总数按同一比例给足，角色在成片里的像素密度才和原来一样。
        # 关掉「跟随提高」时可以固定 MP：分辨率低一点、生成快，但角色像素会摊薄。
        area = (canvas_w * canvas_h) / max(1, sw * sh)
        follow = bool(params.get("video_mp_follow", True))
        megapixels = round(min(MAX_VIDEO_MP, base_mp * area if follow else base_mp), 3)
        if canvas_w != sw or canvas_h != sh:
            self.log(f"留白 左{pad[0]:.0%} 上{pad[1]:.0%} 右{pad[2]:.0%} 下{pad[3]:.0%}："
                     f"画布 {sw}×{sh} → {canvas_w}×{canvas_h}，视频就按这个尺寸出图"
                     f"（{base_mp:g} → {megapixels:g} MP"
                     f"{'，角色没缩小、清晰度不降' if follow else '，清晰度固定不跟随'}）")
        else:
            self.log(f"画布 {sw}×{sh}，留白为 0、不做扩展")
        first_flat = imaging.compose_frame(first_src, out / f"{stem}_first.png", bg, pad=pad)
        last_flat = (first_flat if loop else
                     imaging.compose_frame(last_src, out / f"{stem}_last.png", bg, pad=pad))
        first_name = self._upload(first_flat, f"{action_key}_first")
        last_name = first_name if last_flat == first_flat else self._upload(last_flat, f"{action_key}_last")
        # 项目里若保存了固定种子，也要让每一段视频的种子不同，否则会反复生成同一条；
        # 界面上填了「视频种子」就完全按它来，用来复现某一次的出片
        try:
            fixed_seed = int(params.get("video_seed") or 0)
        except (TypeError, ValueError):
            fixed_seed = 0
        seed = (fixed_seed % MAX_SEED if fixed_seed > 0 else
                (self._seed(30) + len(p.video_records()) * 104729) % MAX_SEED)
        self.log(f"视频种子 {seed}（" +
                 ("界面指定" if fixed_seed > 0 else "自动：基准种子 + 段数递增") + "）")
        seconds = float(params.get("video_seconds") or 1.0)
        wf = self.wf.i2v(
            first_name=first_name, last_name=last_name, prompt=text,
            seconds=seconds, megapixels=megapixels,
            turbo=bool(params.get("video_turbo", True)),
            seed=seed, prefix=f"sm_{p.name}_{stem}", workflow=workflow,
            audio=not silent,          # 静音：CreateVideo 不接音频，成片没有音轨
        )
        dest = out / f"{stem}.mp4"
        info = self.comfy.run(wf, dest, exts=(".mp4", ".webm", ".mkv", ".mov", ".gif"),
                              log=self.log, should_stop=self.should_stop)
        p.add_job({"stage": "video", "prompt_id": info["prompt_id"], "output": dest.name})
        dest = Path(info["files"][0])
        probe = ffmpeg_tools.probe(dest)
        self.log(f"视频段已保存 {dest.name}：{probe['width']}×{probe['height']}，"
                 f"{probe['duration']:.2f}s，{probe['fps']}fps")
        record = {
            "id": stem, "file": dest.name, "label": label,
            "action": action, "action_key": action_key,
            "direction": dir_label,
            "first_key": first_key["id"], "first_label": first_key.get("label") or first_key["id"],
            "last_key": last_key["id"], "last_label": last_key.get("label") or last_key["id"],
            "loop": loop,
            "workflow": workflow or str(self.cfg["workflows"]["i2v"]),
            "workflow_label": wf_name,
            "motion": prompts.clean(params.get("video_motion", "")),
            "prompt": text,
            "seconds": seconds, "megapixels": megapixels, "megapixels_base": base_mp,
            "pad": [round(x * 100) for x in pad],
            "canvas": [canvas_w, canvas_h], "input_size": [sw, sh],
            "turbo": bool(params.get("video_turbo", True)),
            "background": bg, "seed": seed, "created": created,
            "prompt_id": info["prompt_id"], "probe": probe,
        }
        p.add_video_record(record)
        p.update_params({"frame_source": dest.name})   # 新段默认成为切帧源，旧段仍可在下拉里选回
        self.log(f"项目现有 {len(p.video_records())} 段视频，可在「切帧」页选择要切的一段")
        return {"file": dest.name, "id": stem, "label": label, "probe": probe,
                "first_key": first_key["id"], "last_key": last_key["id"], "loop": loop,
                "background": bg, "workflow": record["workflow"],
                "pad": record["pad"], "canvas": record["canvas"],
                "megapixels": megapixels,
                "videos": len(p.video_records())}


    # ------------------------------------------------------------ 4. 切帧
    def frames(self) -> dict:
        p, params = self.p, self.p.params
        video_dir = p.dir_of("video")
        records = p.video_records()
        if not records:
            raise PipelineError("找不到任何视频段，请先执行视频阶段")
        chosen = str(params.get("frame_source") or "").strip()
        record = next((r for r in records if r["file"] == chosen), None)
        if record is None:
            if chosen:
                self.log(f"指定的视频段 {chosen} 不存在，回退到最新一段")
            record = records[-1]                      # frame_source 为空 = 最新一段
        video = video_dir / str(record["file"])
        if not video.is_file():
            raise PipelineError(f"视频文件不存在：{video.name}")

        # 每段视频一个独立帧组，切 A 段不会覆盖或混入 B 段的帧。
        # 旧版 loop.mp4 继续平铺在 04_frames/ 根目录，保证老项目的选择不失效。
        legacy = video.name == "loop.mp4" or bool(record.get("legacy"))
        group = "" if legacy else video.stem
        out = p.dir_of("frames") / group if group else p.dir_of("frames")
        self.log(f"切帧源：{record.get('label') or video.name}（{video.name}）→ 帧组「{group or '旧版帧序列'}」")
        self._check_stop()
        frames = ffmpeg_tools.extract_frames(
            video, out,
            fps=float(params.get("extract_fps") or 0),
            scale=int(params.get("frame_scale") or 0),
            log=self.log,
        )
        probe = ffmpeg_tools.probe(video)
        rel_names = [f"{group}/{f.name}" if group else f.name for f in frames]
        # 动作出画的兜底检查：有帧的角色贴着画面边缘，说明这一段动作超出取景，
        # 切出来的帧缺手缺脚也补不回来，提醒把扩画布的留白调大重跑
        tight = [f.name for f in frames if min(imaging.edge_gap(f)) <= 2]
        if tight:
            self.log(f"  ! {len(tight)}/{len(frames)} 帧的角色贴到了画面边缘（动作出画，例如 {tight[0]}），"
                     f"建议把第 ③ 步「四向留白」调大后重新生成视频")
        # 背景漂移检查：模型偶尔把整段纯色底漂成别的颜色，色键就把底色当角色留下
        drift = imaging.background_drift(frames)
        if drift:
            src, base, got = drift
            self.log(f"  ! {src.name} 的背景漂到了 RGB{got}（首帧 RGB{base}），"
                     f"色键去背景会留下一片底色，建议重新生成这一段")
        manifest = {
            "source": video.name, "group": group,
            "label": record.get("label", ""), "count": len(frames),
            "extract_fps": params.get("extract_fps") or probe["fps"],
            "video": probe, "frames": rel_names, "tight_frames": len(tight),
        }
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (p.dir_of("frames") / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # 保留其它帧组的已选帧；本组若第一次切（或强制重跑）则默认均匀挑最多 8 帧
        old = [str(x).replace("\\", "/") for x in (p.data.get("selection") or [])]

        def belongs(x: str) -> bool:
            return (x.startswith(group + "/") if group else ("/" not in x))

        has_group = any(belongs(x) and p.frame_path(x).is_file() for x in old)
        if self.force or not has_group:
            target = min(8, len(frames))
            step = max(1, len(frames) // target)
            picked = [rel_names[i] for i in range(0, len(frames), step)][:target]
            merged, inserted = [], False
            for x in old:
                if belongs(x):
                    if not inserted:
                        merged.extend(picked)
                        inserted = True
                else:
                    merged.append(x)
            if not inserted:
                merged.extend(picked)
            self.log(f"帧组「{group or '旧版帧序列'}」默认均匀挑选 {len(picked)} 帧，可在「挑帧」页调整")
        else:
            merged = [x for x in old if not belongs(x) or p.frame_path(x).is_file()]
        p.set_selection(merged)
        p.update_params({"frame_group": group})       # 挑帧页自动切到刚切出来的组
        return manifest

    # ------------------------------------------------------------ 5. 精灵图
    def _raw_groups(self) -> dict[str, list[Path]]:
        """05_sprites_raw 下现有的全部去背景原图，按帧组分组（"" = 旧版平铺）。"""
        root = self.p.dir_of("sprites_raw")
        out: dict[str, list[Path]] = {}
        loose = sorted(root.glob("*.png"))
        if loose:
            out[""] = loose
        for d in sorted([x for x in root.iterdir() if x.is_dir()], key=lambda x: x.name):
            pngs = sorted(d.glob("*.png"))
            if pngs:
                out[d.name] = pngs
        return out

    def _raw_ref(self, files: list[Path], threshold: int) -> tuple[int, int] | None:
        """一组的参考尺寸（裁掉透明边后的最大宽高），带缓存。

        只重绘某一段视频时，其它组不会再走一遍规范化，但它们必须仍然参与
        「统一基准」——否则这一组会按自己的包围盒缩放，切动作时角色忽大忽小。
        这里流式读一遍其它组的原图取包围盒（不常驻内存），结果按文件版本缓存。
        """
        if not files:
            return None
        cache = files[0].parent / "_ref.json"
        try:
            stamp = f"{len(files)}-{max(f.stat().st_mtime_ns for f in files)}"
        except OSError:
            return None
        if cache.is_file():
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                ref = raw.get("ref")
                if (int(raw.get("v", 0)) == REF_CACHE_VERSION
                        and raw.get("stamp") == stamp
                        and int(raw.get("threshold", -1)) == threshold
                        and isinstance(ref, list) and len(ref) == 2):
                    return int(ref[0]), int(ref[1])
            except (OSError, ValueError, TypeError):
                pass
        ref = imaging.raw_ref(files, threshold=threshold)
        if ref:
            try:
                cache.write_text(json.dumps({"v": REF_CACHE_VERSION, "stamp": stamp,
                                             "threshold": threshold, "ref": list(ref)},
                                            ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return ref

    def sprites(self) -> dict:
        p, params = self.p, self.p.params
        selection = [str(x).replace("\\", "/") for x in (p.data.get("selection") or [])]
        if not selection:
            raise PipelineError("还没有挑选任何帧，请先在「挑帧」页勾选")

        # 按帧组（= 一段视频 = 一个动作 / 方向）拆开处理：
        # 每组各自一套去背景产物与画布，绝不把「走路」和「攻击」混进同一个目录。
        by_group: dict[str, list[str]] = {}
        for rel in selection:
            by_group.setdefault(p.group_of(rel), []).append(rel)

        # 处理范围：默认全部帧组；指定帧组时只处理这一段视频的帧，其它组已出的产物原样保留
        scope = str(params.get("sprite_group") or "").strip()
        if scope:
            known = {g["id"] for g in p.frame_groups()}
            if scope not in known:
                p.update_params({"sprite_group": ""})
                raise PipelineError(f"处理范围指向的帧组「{scope}」已经不存在（视频段可能被删了），"
                                    f"已改回「全部帧组」，请重新确认后再执行")
            if scope not in by_group:
                raise PipelineError(f"帧组「{p.group_label(scope)}」还没有挑选任何帧："
                                    f"请到 ⑤ 挑帧页切到该组勾选帧，或把处理范围改回「全部帧组」")
            by_group = {scope: by_group[scope]}
            self.log(f"处理范围：只处理「{p.group_label(scope)}」这一段（{len(by_group[scope])} 帧），"
                     f"其它组的精灵帧原样保留")
        else:
            self.log(f"处理范围：全部 {len(by_group)} 组、共 {len(selection)} 帧")

        negative = prompts.negative_for(params)
        method = str(params.get("sprite_method") or "redraw")
        bg = prompts.video_background(params)
        tolerance = int(params.get("chroma_tolerance") or 60)
        # 最终精灵帧必须是透明图，所以重绘模板始终套 RGBA 句式
        text = prompts.rgba(prompts.clean(params.get("sprite_prompt", "")) or prompts.sprite_redraw(params))
        note = (text if method != "chroma" else
                f"色键去背景（未调用图生图）：配置背景色 {bg}，实际按画面边缘自动取样，容差 {tolerance}")

        raws_by_group: dict[str, list[Path]] = {}
        for group, rels in by_group.items():
            self._check_stop()
            raw_dir = (p.dir_of("sprites_raw") / group) if group else p.dir_of("sprites_raw")
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "prompt.txt").write_text(note, encoding="utf-8")
            raws: list[Path] = []
            for i, name in enumerate(rels):
                self._check_stop()
                src = p.frame_path(name)             # 支持「帧组/frame_XXXX.png」相对路径
                if not src.is_file():
                    self.log(f"跳过丢失的帧 {name}")
                    continue
                flat = Path(name).name               # 目录已经隔离，组内不必再加前缀
                dest = raw_dir / flat
                tag = f"{group or '旧版帧序列'} {i + 1}/{len(rels)}"
                if dest.exists() and not self.force:
                    self.log(f"[{tag}] 跳过已处理的 {flat}")
                    raws.append(dest)
                    continue
                if method == "chroma":
                    self.log(f"[去背景 {tag}] 色键抠背景（自动取样实际色）")
                    raws.append(imaging.chroma_key(src, dest, None, tolerance, log=self.log))
                    continue
                self.log(f"[去背景 {tag}] 图生图重绘 {flat}")
                image_name = self._upload(src, f"sprite_{Path(flat).stem}"[:60])
                wf = self.wf.i2i(image_name=image_name, prompt=text, negative=negative,
                                 seed=self._seed(100 + i), prefix=f"sm_{p.name}_sp_{Path(flat).stem}"[:90])
                raws.append(self._generate_image(wf, dest, f"sprites/{group + '/' if group else ''}{flat}"))
            if raws:
                raws_by_group[group] = raws
        if not raws_by_group:
            raise PipelineError("没有可用的去背景结果")

        sprites_root = p.dir_of("sprites")
        for group in raws_by_group:
            self._check_stop()
            out_dir = (sprites_root / group) if group else sprites_root
            out_dir.mkdir(parents=True, exist_ok=True)
            for old in out_dir.glob("*.png"):        # 只清本组旧帧，其它组原样保留
                old.unlink()

        per_group_scale = str(params.get("sprite_scale_mode") or "global") == "group"
        extra_refs: dict[str, tuple[int, int]] = {}
        if not per_group_scale:
            # 只处理单组时把其它组的原图也纳入缩放基准，保证切动作时角色大小一致。
            # 仅读包围盒，不改动那些组的产物，也不额外调用模型。
            for gid, files in self._raw_groups().items():
                if gid in raws_by_group:
                    continue
                ref = self._raw_ref(files, int(params.get("alpha_threshold") or 12))
                if ref:
                    extra_refs[gid] = ref
            if extra_refs:
                self.log(f"统一基准：另外 {len(extra_refs)} 组沿用现有去背景原图参与缩放基准（"
                         + "、".join(p.group_label(g) for g in extra_refs) + "），不调用模型")

        final = imaging.normalize_groups(
            raws_by_group, sprites_root,
            target=(int(params.get("sprite_width") or 256), int(params.get("sprite_height") or 256)),
            align=params.get("align", "bottom-center"),
            trim=bool(params.get("trim_alpha", True)),
            threshold=int(params.get("alpha_threshold") or 12),
            per_group_scale=per_group_scale,
            extra_refs=extra_refs,
            log=self.log,
        )

        groups_out: list[dict] = []
        for group, files in final.items():
            out_dir = (sprites_root / group) if group else sprites_root
            report = {f.name: imaging.alpha_info(f) for f in files}
            (out_dir / "alpha_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            avg = sum(v["opaque_ratio"] for v in report.values()) / max(1, len(report))
            self.log(f"[{group or '旧版帧序列'}] Alpha 质检：平均不透明占比 {avg:.1%}（越低说明背景越干净）")
            groups_out.append({"group": group, "label": p.group_label(group),
                               "count": len(files), "opaque_avg": round(avg, 4),
                               "files": [f.name for f in files]})
        total = sum(g["count"] for g in groups_out)
        weighted = sum(g["opaque_avg"] * g["count"] for g in groups_out) / max(1, total)
        if scope:
            self.log(f"本次只处理「{p.group_label(scope)}」：{total} 帧，"
                     f"其它组的精灵帧与质检报告未改动")
        return {"count": total, "opaque_avg": round(weighted, 4), "groups": groups_out,
                "scope": scope,
                "files": [f"{g['group']}/{n}" if g["group"] else n
                          for g in groups_out for n in g["files"]]}

    # ------------------------------------------------------------ 6. 导出
    def export(self) -> dict:
        p, params = self.p, self.p.params
        src_root = p.dir_of("sprites")
        # 去背景产物按组存放在子目录里；旧项目的帧平铺在根目录，算作「旧版帧序列」组
        by_group: dict[str, list[Path]] = {}
        loose = sorted(src_root.glob("*.png"))
        if loose:
            by_group[""] = loose
        for d in sorted([x for x in src_root.iterdir() if x.is_dir()], key=lambda x: x.name):
            pngs = sorted(d.glob("*.png"))
            if pngs:
                by_group[d.name] = pngs
        if not by_group:
            raise PipelineError("还没有精灵帧，请先执行去背景重绘")

        # 导出范围：默认全部帧组；指定帧组时只导出这一段视频的那套 spritesheet / GIF / ZIP
        scope = str(params.get("export_group") or "").strip()
        if scope:
            if scope not in by_group:
                p.update_params({"export_group": ""})
                raise PipelineError(f"帧组「{p.group_label(scope)}」还没有精灵帧，"
                                    f"请先在 ⑥ 处理这一段，或把导出范围改回「全部帧组」")
            by_group = {scope: by_group[scope]}
            self.log(f"导出范围：只导出「{p.group_label(scope)}」这一段")
        else:
            self.log(f"导出范围：全部 {len(by_group)} 组")

        out_root = p.dir_of("export")
        cols = int(params.get("sprite_columns") or 8)
        fps = int(params.get("gif_fps") or 12)
        results: list[dict] = []
        for group, frames in by_group.items():
            self._check_stop()
            out = (out_root / group) if group else out_root
            out.mkdir(parents=True, exist_ok=True)
            label = group or "旧版帧序列"
            prefix = f"[{label}] "

            def _log(msg: str, _p: str = prefix) -> None:
                self.log(_p + msg)

            sheet = out / "spritesheet.png"
            meta = imaging.make_spritesheet(frames, sheet, columns=cols, fps=fps, log=_log)
            gif = imaging.make_gif(frames, out / "preview.gif", fps=fps, log=_log)
            zip_name = f"{p.name}_{group}_sprites.zip" if group else f"{p.name}_sprites.zip"
            zip_path = out / zip_name
            report = (src_root / group / "alpha_report.json") if group else (src_root / "alpha_report.json")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in frames:
                    zf.write(f, f"sprites/{f.name}")
                for f in (sheet, sheet.with_suffix(".json"), gif, report):
                    if f.exists():
                        zf.write(f, f.name)
                zf.writestr("project.json", json.dumps(p.data, ensure_ascii=False, indent=2))
            rel_dir = f"{group}/" if group else ""
            self.log(f"{prefix}打包完成 → {rel_dir}{zip_name}（{zip_path.stat().st_size // 1024} KB）")
            results.append({
                "group": group, "label": p.group_label(group),
                "sheet": f"{rel_dir}spritesheet.png", "gif": f"{rel_dir}preview.gif",
                "zip": zip_name, "zip_path": f"{rel_dir}{zip_name}",
                "frames": len(frames), "columns": meta["columns"],
            })

        total = sum(r["frames"] for r in results)
        if scope:
            self.log(f"本次只导出「{p.group_label(scope)}」：{total} 帧，其它组已有的导出文件未改动")
        else:
            self.log(f"共 {len(results)} 组、{total} 帧分别导出（每组一套 spritesheet / GIF / ZIP）")
        out = {"groups": results, "count": len(results), "frames": total, "scope": scope}
        if len(results) == 1:                        # 单组时保留旧字段，CLI 与老脚本照旧可用
            out.update({k: results[0][k] for k in ("sheet", "gif", "zip", "frames", "columns")})
        return out

    # ------------------------------------------------------------ 调度
    STAGE_FUNCS = {
        "reference": reference, "keyframe": keyframe, "video": video,
        "frames": frames, "sprites": sprites, "export": export,
    }

    def run(self, stage: str) -> dict:
        if stage not in self.STAGE_FUNCS:
            raise PipelineError(f"未知阶段：{stage}")
        self._check_stop()
        self.p.stage_begin(stage)
        self.log(f"===== 阶段开始：{stage} =====")
        try:
            result = self.STAGE_FUNCS[stage](self)
            if self._base_seed and not int(self.p.params.get("seed") or 0):
                self.p.params["seed"] = self._base_seed   # 记录本次随机种子，便于复现
            self.p.stage_done(stage, result=result)
            self.log(f"===== 阶段完成：{stage} =====")
            return result
        except (ComfyError, PipelineError, workflows.WorkflowError,
                ffmpeg_tools.FFmpegError, RuntimeError, OSError) as exc:
            self.p.stage_fail(stage, str(exc))
            self.log(f"!!!!! 阶段失败：{stage} —— {exc}")
            raise

    def run_all(self) -> list[str]:
        done = []
        for stage in ("reference", "keyframe", "video", "frames", "sprites", "export"):
            self._check_stop()
            self.run(stage)
            done.append(stage)
        return done
