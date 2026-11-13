"""SpriteMaker —— 用 ComfyUI + ffmpeg 制作 2D 游戏精灵循环动画。

流水线：文生图正面立绘 → 派生各方向单帧 → 首尾帧循环视频
        → ffmpeg 切帧 → 人工挑帧 → 图生图重绘去背景 → 精灵图 / spritesheet。
"""
from __future__ import annotations

__version__ = "1.0.0"

STAGES = ("reference", "tune", "video", "frames", "sprites", "export")
VIEWS = ("front", "back", "left", "right")
VIEW_LABELS = {"front": "正面", "back": "背面", "left": "左侧", "right": "右侧"}
