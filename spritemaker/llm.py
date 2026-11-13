"""用大模型（DeepSeek）改写绘图提示词。

内置模板已经能跑，但句式是硬编码的、偏啰嗦，模型执行时容易漏条件。
这里把模板交给 DeepSeek 重写一版：保留全部硬性约束，只把表达理顺、
把关键要求提到前面、把互相矛盾的说法去掉。

几条原则：
- 只改写法，不改事实。角色设定、视角、背景色、道具都不能被替换或新增。
- 硬约束必须原样保留（单角色、不允许并排三视角、武器必须保留、纯色背景）。
- 任何失败都退回原提示词——优化是加分项，不能因为它跑不了图。
- 结果按「阶段 + 原文」哈希缓存进项目目录，同一份提示词只花钱改一次。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

DEFAULT_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM = """你在为「2D 游戏精灵」生成流水线改写图像模型提示词。

输入是一段中文提示词（给 Qwen-Image 系列模型用，可能是文生图，也可能是图生图）。
请把它改写得更利于模型执行，但必须遵守：

1. 只改表达，不改事实。不得新增输入里没有的角色特征、服装、道具、配色、画风。
2. 全文只用肯定句，只写要什么：要哪个视角、这个人物、这些道具、这个姿态、这个背景。
   把「不要 / 不能 / 没有 / 别」这类否定说法改写成对应的肯定描述
   （例：「不要地面阴影」→「背景是一整块干净的纯色」）。
3. 这些正向信息一条都不能丢：单个人物、与输入图是同一角色同一套服装道具、
   武器原样保留、背景纯色的颜色（保留色名和色值）、全身立绘与居中构图。
4. 去掉重复和互相矛盾的说法；把最关键的约束放前面。
5. 输出纯提示词正文：不要解释、不要编号、不要 markdown、不要引号包裹。
6. 精简：一句话说一个要求，长度不超过原文。
"""


class LLMError(RuntimeError):
    pass


def optimize(text: str, *, api_key: str, kind: str = "", url: str = DEFAULT_URL,
             model: str = DEFAULT_MODEL, timeout: float = 40.0,
             log: Callable[[str], None] = lambda _: None) -> str:
    """把 text 交给 DeepSeek 改写一版；kind 只是给模型的上下文提示。"""
    text = (text or "").strip()
    if not text:
        return text
    if not api_key:
        raise LLMError("没有配置 deepseek_api_key")

    hint = {
        "front": "这是第①步「正面角色设定立绘」的文生图提示词。",
        "frame": "这是第②步「拿正面立绘派生其他视角」的图生图提示词，输入是一张正面立绘。",
        "edit": "这是在已有单帧上追加细节的图生图提示词。",
    }.get(kind.split(":")[0], "")

    # 所有会走改写的提示词都在第①②步，统一按「只写要什么」的口径改写
    system = SYSTEM

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{hint}\n\n原文：\n{text}"},
        ],
        "temperature": 0.3,
        "max_tokens": 900,
        "stream": False,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise LLMError(f"HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"请求失败：{exc}") from exc

    try:
        out = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("返回格式异常") from exc
    # 有的模型会自作主张包一层引号或 ```text 围栏
    out = out.strip().strip("`").strip()
    if out.startswith("```"):
        out = out.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    out = out.strip().strip('"').strip("“”").strip()
    if len(out) < 20:
        raise LLMError("改写结果过短，丢弃")
    log(f"  DeepSeek 已改写提示词（{len(text)} → {len(out)} 字）")
    return out
