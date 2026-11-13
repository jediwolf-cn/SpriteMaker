"""工作流加载与参数注入。

workfllow/ 下三份都是 ComfyUI **API 格式**（{节点ID: {class_type, inputs}}），可直接提交。
这里按节点类型定位 + 已知 ID 校验来注入参数，并剪掉与保存链路无关的预览/扩写分支
（避免 ImageCompare 产生临时图、避免 TextGenerate 白白加载大语言模型）。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from . import config
from .comfy import SAVE_CLASSES


class WorkflowError(RuntimeError):
    pass


def _safe_prefix(text: str) -> str:
    """ComfyUI 的 filename_prefix 只用 ASCII，避免落到文件系统时乱码。"""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_")
    return s or "spritemaker"


def _find(wf: dict, class_type: str, *, title: str | None = None) -> tuple[str, dict]:
    hits = [(nid, n) for nid, n in wf.items() if n.get("class_type") == class_type]
    if title:
        narrowed = [h for h in hits if title.lower() in str(h[1].get("_meta", {}).get("title", "")).lower()]
        if narrowed:
            hits = narrowed
    if not hits:
        raise WorkflowError(f"工作流里找不到 {class_type} 节点（title={title}）")
    return hits[0]


def _node(wf: dict, nid: str, class_type: str) -> dict:
    n = wf.get(nid)
    if n and n.get("class_type") == class_type:
        return n
    return _find(wf, class_type)[1]


def _pick(wf: dict, class_type: str, *, nid: str | None = None,
          title: str | None = None) -> dict | None:
    """宽松定位：优先已知节点 ID，再按 title 关键字，最后取同类型第一个。

    多条视频工作流的节点 ID 不一定相同，找不到时返回 None 让调用方自行降级，
    而不是直接抛错导致整段生成失败。
    """
    if nid and isinstance(wf.get(nid), dict) and wf[nid].get("class_type") == class_type:
        return wf[nid]
    hits = [n for n in wf.values() if n.get("class_type") == class_type]
    if title:
        narrow = [n for n in hits if title.lower() in str(n.get("_meta", {}).get("title", "")).lower()]
        if narrow:
            hits = narrow
    return hits[0] if hits else None


def _by_id(wf: dict, nid: str, class_type: str) -> dict | None:
    n = wf.get(nid)
    return n if isinstance(n, dict) and n.get("class_type") == class_type else None


def _by_title(wf: dict, class_type: str, *keywords: str) -> dict | None:
    """按节点标题里的关键字定位参数节点（新工作流 ID 变化时更可靠）。"""
    for n in wf.values():
        if n.get("class_type") != class_type:
            continue
        title = str(n.get("_meta", {}).get("title", "")).lower()
        if any(k in title for k in keywords):
            return n
    return None


def _video_gen(wf: dict) -> dict:
    """定位视频生成节点：已知模型类，或任何带 prompt + 首尾帧输入的节点。"""
    for nid, n in wf.items():
        if n.get("class_type") in ("MiniMaxH3ImageToVideo", "WanImageToVideo",
                                   "WanFirstLastFrameToVideo", "LTXVImageToVideo"):
            return n
    for n in wf.values():
        inp = n.get("inputs") or {}
        cls = str(n.get("class_type") or "")
        if ("prompt" in inp and any(k in inp for k in ("last_frame", "first_frame", "start_frame", "image"))
                and "TextEncode" not in cls):
            return n
    raise WorkflowError("工作流里找不到视频生成节点（需要带 prompt 与首尾帧输入的节点）")


def _link_refs(value) -> list[str]:
    if (isinstance(value, list) and len(value) == 2
            and isinstance(value[0], str) and isinstance(value[1], int)):
        return [value[0]]
    return []


def _prune_unreachable(wf: dict) -> dict:
    """只保留「保存节点」反向依赖到的节点，其余全部丢掉。"""
    roots = [nid for nid, n in wf.items() if n.get("class_type") in SAVE_CLASSES]
    if not roots:
        raise WorkflowError("工作流里没有保存节点（SaveImage / SaveVideo）")
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in wf:
            continue
        seen.add(nid)
        for value in wf[nid].get("inputs", {}).values():
            stack.extend(_link_refs(value))
    return {k: v for k, v in wf.items() if k in seen}


class WorkflowSet:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or config.load()
        self.dir = config.path("workflow_dir")
        self.files = dict(cfg["workflows"])
        self._cache: dict[str, dict] = {}

    def load(self, key: str) -> dict:
        if key not in self._cache:
            p = self.dir / self.files[key]
            if not p.exists():
                raise WorkflowError(f"缺少工作流文件：{p}")
            self._cache[key] = json.loads(p.read_text(encoding="utf-8"))
        return copy.deepcopy(self._cache[key])

    def load_file(self, filename: str) -> dict:
        """按文件名直接加载 workfllow/ 下的工作流（供多工作流选择使用）。"""
        name = Path(str(filename)).name
        p = (self.dir / name).resolve()
        if p.parent != self.dir.resolve() or p.suffix.lower() != ".json":
            raise WorkflowError(f"工作流文件名非法：{filename}")
        if not p.is_file():
            raise WorkflowError(f"缺少工作流文件：{p}")
        key = "@file:" + name
        if key not in self._cache:
            self._cache[key] = json.loads(p.read_text(encoding="utf-8"))
        return copy.deepcopy(self._cache[key])

    def video_workflows(self) -> list[dict]:
        """扫描目录里所有视频工作流，给界面下拉用；顺带做一次兼容性自检。"""
        cfg = config.load()
        labels = dict(cfg.get("workflow_labels") or {})
        default = str(cfg["workflows"].get("i2v") or "")
        out: list[dict] = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                wf = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(wf, dict) or not wf:
                continue
            classes = {str(n.get("class_type") or "") for n in wf.values() if isinstance(n, dict)}
            looks_video = bool(classes & {"SaveVideo", "SaveWEBM", "CreateVideo",
                                          "MiniMaxH3ImageToVideo", "WanImageToVideo"})
            looks_video = looks_video or "video" in p.name.lower() or "i2v" in p.name.lower()
            if not looks_video:
                continue
            ok, reason = self.check_video_workflow(wf)
            out.append({
                "file": p.name, "name": labels.get(p.name) or p.stem,
                "default": p.name == default, "nodes": len(wf),
                "compatible": ok, "reason": reason,
            })
        out.sort(key=lambda x: (not x["default"], x["file"]))
        return out

    @staticmethod
    def check_video_workflow(wf: dict) -> tuple[bool, str]:
        """判断一条工作流能否被 i2v() 注入首尾帧（不要求节点 ID 完全一致）。"""
        classes = {str(n.get("class_type") or "") for n in wf.values() if isinstance(n, dict)}
        if not classes & {"SaveVideo", "SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG"}:
            return False, "缺少视频保存节点（SaveVideo 等）"
        if "LoadImage" not in classes:
            return False, "缺少 LoadImage 首帧节点"
        try:
            gen = _video_gen(wf)
        except WorkflowError as exc:
            return False, str(exc)
        if not isinstance(gen.get("inputs"), dict):
            return False, "视频生成节点没有 inputs"
        return True, ""

    def check(self) -> dict[str, int]:
        """自检：三份默认工作流都能解析且关键节点齐全。"""
        result = {}
        for key in ("t2i", "i2i", "i2v"):
            wf = self.load(key)
            _find(wf, "RandomNoise")
            if key == "i2v":
                _find(wf, "MiniMaxH3ImageToVideo")
                _find(wf, "LoadImage")
                _find(wf, "SaveVideo")
            else:
                _find(wf, "TextEncodeQwenImage21")
                _find(wf, "SaveImageAdvanced")
                if key == "i2i":
                    _find(wf, "LoadImage")
            result[key] = len(wf)
        return result

    # ------------------------------------------------------------ 文生图
    def t2i(self, *, prompt: str, negative: str, width: int, height: int,
            seed: int, prefix: str) -> dict:
        w = self.load("t2i")
        enc = _node(w, "459:452", "TextEncodeQwenImage21")
        enc["inputs"]["prompt"] = prompt            # 直接覆盖 link，绕过 Prompt 改写分支
        enc["inputs"]["negative_prompt"] = negative
        enc["inputs"]["resolution"] = max(width, height)

        latent = _node(w, "459:456", "EmptyLatentImage")
        latent["inputs"].update(width=width, height=height, batch_size=1)

        _node(w, "459:488", "RandomNoise")["inputs"]["noise_seed"] = int(seed)

        switch = _node(w, "459:474", "ComfySwitchNode")
        switch["inputs"]["switch"] = False
        switch["inputs"]["on_false"] = prompt
        switch["inputs"]["on_true"] = prompt        # 断开 TextGenerate 分支

        _node(w, "461", "SaveImageAdvanced")["inputs"]["filename_prefix"] = _safe_prefix(prefix)
        return _prune_unreachable(w)

    # ------------------------------------------------------------ 图生图
    def i2i(self, *, image_name: str, prompt: str, negative: str, seed: int,
            prefix: str, width: int = 0, height: int = 0) -> dict:
        w = self.load("i2i")
        _node(w, "470", "LoadImage")["inputs"]["image"] = image_name

        enc = _node(w, "459:474", "TextEncodeQwenImage21")
        enc["inputs"]["prompt"] = prompt
        enc["inputs"]["negative_prompt"] = negative
        enc["inputs"]["resolution"] = 0             # 0 = 跟随输入图尺寸

        pswitch = next(
            (n for n in w.values()
             if n.get("class_type") == "ComfySwitchNode" and isinstance(n["inputs"].get("on_false"), str)),
            None,
        )
        if pswitch is None:
            raise WorkflowError("图生图工作流里找不到提示词开关节点")
        pswitch["inputs"]["switch"] = False
        pswitch["inputs"]["on_false"] = prompt
        pswitch["inputs"]["on_true"] = prompt

        # 尺寸开关：false = 用输入图编码出的 latent（保持原尺寸）；true = 固定 EmptyLatentImage
        fixed = bool(width and height)
        lswitch = next(
            (n for n in w.values()
             if n.get("class_type") == "ComfySwitchNode" and isinstance(n["inputs"].get("on_false"), list)),
            None,
        )
        if lswitch is not None:
            lswitch["inputs"]["switch"] = fixed
        if fixed:
            latent = _node(w, "459:456", "EmptyLatentImage")
            latent["inputs"].update(width=width, height=height, batch_size=1)

        _node(w, "459:504", "RandomNoise")["inputs"]["noise_seed"] = int(seed)
        _node(w, "461", "SaveImageAdvanced")["inputs"]["filename_prefix"] = _safe_prefix(prefix)
        return _prune_unreachable(w)

    # ------------------------------------------------------------ 首尾帧视频
    def i2v(self, *, first_name: str, last_name: str | None, prompt: str,
            seconds: float, megapixels: float, turbo: bool, seed: int,
            prefix: str, fps: int = 24, workflow: str = "", audio: bool = True) -> dict:
        """注入首尾帧视频工作流。

        workflow 为空用配置默认；指定文件名时用 workfllow/ 下另一份工作流。
        节点 ID 只在同款工作流里可靠，所以全部走 _pick 的「ID → title → 同类首个」
        三级兜底，缺失的可选节点直接跳过，便于以后接入更多视频工作流。
        """
        w = self.load_file(workflow) if workflow else self.load("i2v")
        gen = _video_gen(w)
        first_load = _pick(w, "LoadImage", nid="114", title="首帧")
        if first_load is None:
            raise WorkflowError("视频工作流缺少 LoadImage（首帧）节点")
        first_load["inputs"]["image"] = first_name

        gen["inputs"]["prompt"] = prompt
        if last_name:
            w["sm_tail_load"] = {
                "class_type": "LoadImage", "inputs": {"image": last_name},
                "_meta": {"title": "尾帧"},
            }
            # 尾帧支路必须与首帧走完全相同的缩放参数，否则首尾尺寸不一致
            first_scale = _pick(w, "ImageScaleToTotalPixels", nid="119")
            if first_scale is not None:
                tail_inputs = copy.deepcopy(first_scale["inputs"])
                tail_inputs["image"] = ["sm_tail_load", 0]
                w["sm_tail_scale"] = {
                    "class_type": "ImageScaleToTotalPixels", "inputs": tail_inputs,
                    "_meta": {"title": "尾帧尺寸"},
                }
                tail_ref: list = ["sm_tail_scale", 0]
            else:
                tail_ref = ["sm_tail_load", 0]
            key = next((k for k in ("last_frame", "end_frame", "end_image")
                        if k in gen["inputs"]), "last_frame")
            gen["inputs"][key] = tail_ref
        else:
            gen["inputs"].pop("last_frame", None)

        # 时长 / 清晰度 / 加速开关：先按标题关键字、再按已知 ID；
        # 两者都找不到就跳过该参数（沿用工作流自带值），不硬猜第一个同类型节点。
        duration = (_by_title(w, "PrimitiveFloat", "duration", "时长", "秒数")
                    or _by_id(w, "105:111", "PrimitiveFloat"))
        if duration is not None:
            duration["inputs"]["value"] = float(seconds)

        pixels = (_by_title(w, "PrimitiveFloat", "像素", "megapixel", "resolution", "清晰度")
                  or _by_id(w, "127", "PrimitiveFloat"))
        if pixels is not None and pixels is not duration:
            pixels["inputs"]["value"] = float(megapixels)

        lora = (_by_title(w, "PrimitiveBoolean", "lora", "加速")
                or _by_id(w, "105:126", "PrimitiveBoolean"))
        if lora is not None:
            lora["inputs"]["value"] = bool(turbo)

        noise = _pick(w, "RandomNoise", nid="105:15")
        if noise is not None:
            noise["inputs"]["noise_seed"] = int(seed)
        create = _pick(w, "CreateVideo", nid="105:91")
        if create is not None:
            create["inputs"]["fps"] = int(fps)
            if not audio:
                # H3 的音频是 CreateVideo 上的可选输入。提示词里写「不要配乐、不要人声」
                # 只影响模型生成的内容，管不住它照样出声（实测仍有一段环境声），
                # 所以静音要在这里断线：不接音频，成片就没有音轨，断开的音频解码支路
                # 会随不可达节点一起被剪掉。
                create["inputs"].pop("audio", None)
        for cls in ("SaveVideo", "SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG"):
            save = _pick(w, cls, nid="92") if cls == "SaveVideo" else _pick(w, cls)
            if save is not None:
                save["inputs"]["filename_prefix"] = _safe_prefix(prefix)
        return _prune_unreachable(w)
