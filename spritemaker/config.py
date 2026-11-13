"""全局配置：默认值 → config.json → 环境变量，后者优先。

参考 ComfyShell/app/settings.py 的做法，把 ComfyUI 地址、认证、路径全部集中管理。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .prompts import DEFAULT_MOTION

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"

DEFAULTS: dict = {
    # ---------- 远程 ComfyUI ----------
    "comfy_url": "http://comfyui.vrke.cn:8080",
    "comfy_user": "comfyui",
    "comfy_password": "vrke",
    "comfy_subfolder": "spritemaker",   # /upload/image 的 subfolder
    "comfy_timeout": 60,                 # 普通 HTTP 超时（秒）
    "job_timeout": 3600,                 # 单个生成任务最长等待（秒）
    "poll_interval": 2.0,                # /history 轮询间隔（秒）
    "poll_request_timeout": 15,          # 单次轮询请求超时（秒），短一些便于快速重试
    "poll_error_backoff": 2.0,           # 轮询失败后的退避间隔（秒）
    "poll_max_errors": 0,                # 轮询连续失败上限；0=不限，一直重试到 job_timeout
    "request_retries": 3,                # 上传/提交/下载等请求遇连接抖动的自动重试次数
    "request_retry_delay": 3.0,          # 请求重试基础间隔（秒），逐次线性递增
    "submit_retries": 3,                 # 提交任务重试次数（重试前查队列，避免重复生成）
    "download_retries": 3,               # 产物下载遇网关 502/超时自动重试次数
    "download_retry_delay": 3.0,         # 下载重试基础间隔（秒），逐次线性递增

    # ---------- 提示词优化（DeepSeek） ----------
    # 内置模板已经能出图，这一步只是把模板改写得更好执行；失败就退回原提示词
    "prompt_optimize": True,             # 关掉就完全用内置模板
    "deepseek_api_key": "",
    "deepseek_url": "https://api.deepseek.com/chat/completions",
    "deepseek_model": "deepseek-chat",
    "deepseek_timeout": 40,              # 单次改写超时（秒）

    # ---------- 本地路径 ----------
    "ffmpeg": "tools/ffmpeg.exe",
    "workflow_dir": "workfllow",
    "workflows": {
        "t2i": "image_qwen_image_2_1_turbo_t2i.json",
        "i2i": "image_qwen_image_2_1_turbo_image_edit.json",
        "i2v": "video_minimax_h3_i2v.json",
    },
    # 视频工作流在界面里的显示名（可选，未列出时用文件名）
    "workflow_labels": {"video_minimax_h3_i2v.json": "MiniMax H3 首尾帧"},
    "projects_dir": "projects",
    "web_dir": "web",

    # ---------- 本地 Web 服务 ----------
    "host": "127.0.0.1",
    "port": 8787,

    # ---------- 新建项目的默认参数 ----------
    "defaults": {
        "character": "一只蓝色小史莱姆骑士，头戴小小的金色头盔，手拿圆形木盾",
        "style": "干净的 2D 游戏精灵，粗深色描边，平涂赛璐璐上色，边缘清晰，色彩明快",
        "negative": "文字, 水印, 签名, logo, 地面, 阴影, 背景景物, "
                    "动态模糊, 3D 渲染, 写实照片, 画面分割线, 文字标签",
        "width": 1024,                   # 正面立绘画布 4:3（横版）：角色两侧留出武器空间
        "height": 768,
        "seed": 0,                       # 0 = 每次随机
        "reference_use_image": False,    # 勾选后以上传的底图做图生图生成正面立绘（可选）
        "reference_image": "",           # 项目内相对路径，如 00_source/base.png
        "background_mode": "solid",      # solid=纯色背景（推荐） / transparent=RGBA 透明参考图
        "background_color": "#00FF00",   # 正面立绘 / 单帧 / 视频共用的纯色背景
        # 第 ② 步：由正面立绘派生哪些方向的单帧
        "frame_directions": ["front", "back", "left", "right"],
        "frame_extra_views": [],         # 追加方向：[{id,label,prompt,from}]，如左前 45°
        "frame_edit_prompts": {},        # 每个方向追加细节的编辑指令，如 {"front": "手里加一把长剑"}
        "weapon_hand": "auto",           # 武器握在哪只手：auto / left / right（左右侧提示词据此写死）
        "first_frame": "",               # 第 ③ 步首帧：第 ② 步的单帧 id；空 = 第一张
        "last_frame": "",                # 第 ③ 步尾帧：空 = 尾帧同首帧（无缝循环）
        "video_workflow": "",            # 空 = 用 workflows.i2v；可指定 workfllow/ 下其它视频工作流
        "video_label": "",               # 自定义视频段名称；留空自动「动作 · 方向 · 时间」
        "video_motion": DEFAULT_MOTION,  # 中文动作描述，可在界面点快捷按钮切换
        "video_prompt": "",              # 自定义视频提示词；留空用内置模板
        "video_seconds": 1.0,            # 默认 1 秒，出片更快、便于做循环
        "video_megapixels": 0.4,         # 默认 0.4 MP，画面更清晰
        # 视频种子：0 = 自动（基准种子 + 段数递增，每次新段都不一样）；
        # 填具体数字 = 这一段就用这个种子，可复现某次效果
        "video_seed": 0,
        # 首帧四周留白（百分比，左 / 上 / 右 / 下各自可调）：用背景色往外扩画布，
        # 角色原样不动，扩出的空间留给跳跃、挥砍的手脚和武器；0 = 不扩
        "video_pad_left": 20,
        "video_pad_top": 20,
        "video_pad_right": 20,
        "video_pad_bottom": 20,
        # 扩了画布之后视频像素总数是否按面积同比提高：开 = 角色清晰度不降
        "video_mp_follow": True,
        # H3 原生生成音轨：提示词不写声音，模型会自己补配乐和杂音。
        # 开 = 末尾固定一段「只有极轻底噪，没有配乐、没有人声、没有旁白」的声音描述。
        "video_silent_audio": True,
        "video_turbo": True,             # 8-step Lightning LoRA
        "extract_fps": 12,               # 0 = 按视频原始帧率全切
        "frame_scale": 0,                # 0 = 保持原尺寸；否则缩放到该边长（正方形）
        "frame_source": "",              # 切帧所选视频文件名；空 = 最新一段
        "frame_group": "",               # 挑帧页当前查看的帧组；空 = 最近切帧的组
        "selection": [],                 # 选中的帧（相对 04_frames/ 的路径）
        "sprite_method": "redraw",       # redraw=图生图重绘 / chroma=色键直接抠纯色背景
        "chroma_tolerance": 60,          # 色键容差（仅 sprite_method=chroma 生效）
        # 第 ⑥ 步处理范围：空 = 全部帧组；填帧组 id（= 视频段 id）则只处理这一段视频的帧，
        # 其它组的精灵帧、质检报告原样保留
        "sprite_group": "",
        "sprite_width": 256,
        "sprite_height": 256,
        "align": "bottom-center",        # bottom-center / center
        "trim_alpha": True,
        "alpha_threshold": 12,
        # 组间比例：global=所有动作共用缩放基准（切动作不变大变小，默认）
        #           group=每个动作各自撑满画布（画面更大，组间比例可能不同）
        "sprite_scale_mode": "global",
        "sprite_columns": 8,
        "gif_fps": 12,
        # 第 ⑦ 步导出 / 预览范围：空 = 全部帧组；填帧组 id 则只导出这一段视频的那套文件
        "export_group": "",
    },
}

# 环境变量 → 配置键
ENV_MAP = {
    "COMFY_URL": "comfy_url",
    "COMFY_USER": "comfy_user",
    "COMFY_PASSWORD": "comfy_password",
    "COMFY_SUBFOLDER": "comfy_subfolder",
    "SPRITEMAKER_FFMPEG": "ffmpeg",
    "SPRITEMAKER_PORT": "port",
    "SPRITEMAKER_HOST": "host",
}

_CONFIG: dict | None = None


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force: bool = False) -> dict:
    """读取配置（带缓存）。config.json 不存在时返回纯默认值。"""
    global _CONFIG
    if _CONFIG is not None and not force:
        return _CONFIG
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = _deep_merge(cfg, user)
        except Exception as exc:  # 配置写坏了不能让整个程序起不来
            print(f"[config] config.json 读取失败，使用默认值：{exc}")
    for env_key, cfg_key in ENV_MAP.items():
        if os.getenv(env_key):
            val: object = os.environ[env_key]
            if cfg_key == "port":
                try:
                    val = int(str(val))
                except ValueError:
                    continue
            cfg[cfg_key] = val
    cfg["comfy_url"] = str(cfg["comfy_url"]).rstrip("/")
    _CONFIG = cfg
    return cfg


def save(cfg: dict) -> None:
    """把当前配置写回 config.json。"""
    global _CONFIG
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    _CONFIG = load(force=True)


def path(key: str) -> Path:
    """把配置里的相对路径解析成绝对路径。"""
    cfg = load()
    p = Path(str(cfg[key]))
    return p if p.is_absolute() else (ROOT / p)


def ffmpeg_exe() -> Path:
    p = path("ffmpeg")
    if not p.exists():
        # 退回 PATH 里的 ffmpeg
        from shutil import which
        alt = which("ffmpeg")
        if alt:
            return Path(alt)
    return p


def default_params() -> dict:
    return copy.deepcopy(load()["defaults"])
