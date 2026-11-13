"""提示词模板（中文）。

背景策略
--------
* transparent：用 Qwen-Image 2.1 官方 RGBA 句式（来自 CardGame/ui_assets_source.py）
  输出透明 PNG，头尾两句固定英文是丢/留 Alpha 的硬约束，不能翻译。
* solid：不套 RGBA 句式，直接要求均匀纯色背景。视频模型没有 Alpha 通道，
  ComfyUI 的 LoadImage 会把透明区变成纯黑（实测），黑底 + 黑边容易让模型
  脑补出紫色背景与暗色描边，所以默认改成纯色背景。

无论哪种策略，视频阶段都会把首尾帧先合成到纯色再上传；最终精灵帧始终用
RGBA 句式输出透明图，游戏里直接用。
Qwen-Image / MiniMax H3 均原生支持中文提示词。
"""
from __future__ import annotations

# 官方固定句式：技术性硬约束，必须原样保留英文，详见模块说明
RGBA_PREFIX = "This is an RGBA image with transparency."
RGBA_SUFFIX = "The image has alpha channel and the background is transparent."

# 视频输入的默认纯色背景（无 Alpha 通道，必须自己给一个确定的颜色）
DEFAULT_BG = "#00FF00"

COLOR_NAMES = {
    "#00FF00": "纯绿色",
    "#FF00FF": "纯品红色",
    "#FFFFFF": "纯白色",
    "#000000": "纯黑色",
    "#808080": "纯中灰色",
    "#0000FF": "纯蓝色",
    "#FF0000": "纯红色",
    "#FFFF00": "纯黄色",
    "#00FFFF": "纯青色",
}

VIEW_PHRASES = {
    "front": "正面视角，面向观众，左右对称的全身站姿",
    "back": "背面视角，从正后方看，全身站姿",
    "left": "严格的正侧面视角，90 度侧身朝左，角色面朝画面左边，全身站姿",
    "right": "严格的正侧面视角，90 度侧身朝右，角色面朝画面右边，全身站姿",
}

VIEW_LABELS = {"front": "正面", "back": "背面", "left": "左侧", "right": "右侧"}

# 默认的四个方向，按这个顺序生成（正面先生成，其余方向拿正面图派生）
VIEW_ORDER = ("front", "back", "left", "right")

# 老项目里只有一个「侧面」（旧提示词是 90 度朝右），升级后按右侧处理，
# 这样已存在的项目重跑第 ② 步不会凭空多出一张图，也不会因为 id 不认识而丢方向。
LEGACY_VIEWS = {"side": "right"}


def norm_view(view: str) -> str:
    """方向 id 归一：side → right；不认识的方向返回空串。"""
    v = LEGACY_VIEWS.get(str(view or "").strip(), str(view or "").strip())
    return v if v in VIEW_LABELS else ""

# 动作描述预设：Web 界面第 ③ 步渲染成快捷按钮，CLI 可用 `python main.py presets` 查看
ACTION_PRESETS: list[dict] = [
    {"key": "idle", "label": "站立待机",
     "motion": "角色原地站立待机，身体轻轻左右晃动，胸部呼吸式的挤压与拉伸，手臂微微摆动，一个周期后回到起始姿态"},
    {"key": "walk", "label": "走路",
     "motion": "角色原地做行走循环，左右脚交替迈步、双臂自然反向摆动，身体轻微上下起伏，保持在画面原位，一周期后回到起始姿态"},
    {"key": "run", "label": "奔跑",
     "motion": "角色原地做奔跑循环，双腿大幅交替前迈与后蹬，双臂用力摆动，身体前倾并有明显上下起伏，头发和衣摆随动作飘动，一周期后回到起始姿态"},
    {"key": "attack", "label": "攻击",
     "motion": "角色原地做一次挥砍攻击循环，抬手蓄力、向前挥出武器、收势回到起始站姿，动作干脆有力，一周期后回到起始姿态"},
    {"key": "jump", "label": "跳跃",
     "motion": "角色原地做跳跃循环，下蹲蓄力、腾空伸展、落地屈膝缓冲、再站直回到起始姿态，保持在画面原位"},
    {"key": "cast", "label": "施法",
     "motion": "角色原地做施法循环，抬手聚气、双手向前推出释放法术、收手回到起始站姿，只有身体与手臂动作，不要额外特效"},
    {"key": "hurt", "label": "受击",
     "motion": "角色原地做受击循环，身体向后一震、微微后仰并缩肩，随后稳住身体回到起始站姿，幅度克制，不要倒地"},
    {"key": "cheer", "label": "庆祝",
     "motion": "角色原地做庆祝循环，双手举起挥动、身体轻快蹦跳并左右轻摆，最后落回起始站姿，气氛欢快"},
]

DEFAULT_MOTION = ACTION_PRESETS[0]["motion"]


# ---------------------------------------------------------------- 背景策略
def norm_color(value: str) -> str:
    """把用户填的颜色规范成 #RRGGBB；无法识别时用默认绿幕。"""
    s = str(value or "").strip().upper()
    if s.startswith("#") and len(s) == 7:
        try:
            int(s[1:], 16)
            return s
        except ValueError:
            pass
    if s.startswith("#") and len(s) == 4:
        return "#" + "".join(c * 2 for c in s[1:])
    return DEFAULT_BG


def color_name(value: str) -> str:
    c = norm_color(value)
    return COLOR_NAMES.get(c, f"RGB({int(c[1:3], 16)}, {int(c[3:5], 16)}, {int(c[5:7], 16)}) 纯色")


def background_mode(params: dict) -> str:
    return "solid" if str(params.get("background_mode") or "solid") == "solid" else "transparent"


def video_background(params: dict) -> str:
    """视频输入 / 锁定用的纯色背景。"""
    return norm_color(params.get("background_color") or DEFAULT_BG)


def rgba(body: str) -> str:
    """用官方句式包裹主体描述；已包裹过则原样返回（幂等）。"""
    t = body.strip()
    if t.startswith(RGBA_PREFIX) and t.endswith(RGBA_SUFFIX):
        return t
    if t.startswith(RGBA_PREFIX):
        t = t[len(RGBA_PREFIX):].strip()
    if t.endswith(RGBA_SUFFIX):
        t = t[:-len(RGBA_SUFFIX)].strip()
    return f"{RGBA_PREFIX} {t} {RGBA_SUFFIX}"


def apply_background(body: str, params: dict) -> str:
    """参考图 / 微调阶段按背景策略收尾：纯色不套英文句式，透明套 RGBA 句式。"""
    t = clean(body)
    if background_mode(params) == "transparent":
        return rgba(t)
    c = video_background(params)
    return clean(
        f"{t} 背景是铺满整个画面的一整块均匀{color_name(c)}（{c}），"
        f"颜色绝对统一，没有渐变、没有纹理、没有地面、没有阴影、没有光晕，"
        f"角色边缘干净，不要白色或黑色描边。"
    )


def clean(text: str) -> str:
    return " ".join((text or "").split())


# ---------------------------------------------------------------- 视频音频
# H3 的官方提示词要素是「主体 + 动作 + 环境 + 风格 + 镜头 + 声音」，声音是其中的
# 一个自然语言段落，不是独立参数（ComfyUI 的 MiniMaxH3ImageToVideo 节点只有 prompt 一个
# 文本入口，音频是模型原生生成的）。官方示例里「声音：安静室内环境声。」就是一句话，
# 官方模板的结尾也照常写「无液体溢出、无人物、无 Logo」这类否定，所以这里可以直写排除项。
# 关键是必须写：整段不提声音时，模型会自己补一段随机配乐和杂音。
SILENT_AUDIO_LINE = (
    "声音：只有极轻的室内底噪；没有配乐，没有背景音乐，没有歌声，"
    "没有人声对白，没有说话，没有旁白解说，也没有音效。"
)


def has_audio_section(text: str) -> bool:
    low = (text or "").lower()
    return "声音：" in low or "声音:" in low or "sound:" in low


def with_silent_audio(text: str, params: dict) -> str:
    """给最终提示词补上「不要音乐、不要人声」的声音段；用户自己写过就不再动。"""
    body = (text or "").strip()
    if not body or not bool(params.get("video_silent_audio", True)):
        return body
    if has_audio_section(body):
        return body
    return f"{body} {SILENT_AUDIO_LINE}"




def preset_motion(key: str) -> str:
    """按 key 取动作预设描述；找不到时返回空串。"""
    for item in ACTION_PRESETS:
        if item["key"] == key:
            return item["motion"]
    return ""


# ---------------------------------------------------------------- 各阶段模板
# 第 ① 步只出一张正面全身立绘：一个角色、一个视角，模型不用在三个立绘之间
# 分配注意力，画风、配色、武器最容易画准；其余视角在第 ② 步拿这张正面图
# 逐个派生（同一张底图，其余方向的美术自然一致）。
FRONT_RULES = (
    "单人全身立绘，面向观众的正平视角，左右对称的自然待机站姿。"
    "竖直方向占满画面高度，水平方向居于画面正中，脚底完整，头顶留出均匀空白。"
    "站姿放松：一只手握着武器、自然垂在身体一侧，武器尖端朝下；另一只手自然垂在身侧。"
    "手里的武器和身上装备完整地画出来，从手一直延伸到身体外侧，轮廓清楚可见。"
)


def front_view(params: dict) -> str:
    """文生图：一张正面全身立绘，作为整条流水线的角色基准。"""
    body = (
        f"2D 游戏精灵的角色设定立绘。"
        f"角色设定：{clean(params.get('character', ''))}。"
        f"{FRONT_RULES}"
        f"画风：{clean(params.get('style', ''))}。"
        f"轮廓干净，配色统一。"
    )
    return background_only(body, params)


def front_view_from_image(params: dict) -> str:
    """图生图：以用户上传的底图为角色设定，产出正面全身立绘。"""
    body = (
        f"以输入图中的角色为唯一的角色设定依据，画出同一个角色的正面全身立绘。"
        f"外形、配色、服装、道具、画风与输入图一致。"
        f"{FRONT_RULES}"
        f"画风：{clean(params.get('style', ''))}。"
        f"轮廓干净，配色统一。"
    )
    return background_only(body, params)


# 第 ② 步拿正面图逐个派生其余视角。写清「转成哪个方向、身体朝哪」，
# 模型才会真的转身，而不是把正面照抄一遍。左侧 / 右侧是镜像关系，
# 所以「朝哪边」必须单独说清楚，否则两张画出来会是同一个方向。
VIEW_DERIVE = {
    "front": ("正面", "身体正面朝向观众"),
    "back": ("背面", "身体转向背对观众"),
    "left": ("左侧", "身体转向画面左侧，以正侧面对着观众，脸和胸口朝画面左边"),
    "right": ("右侧", "身体转向画面右侧，以正侧面对着观众，脸和胸口朝画面右边"),
}


# 武器握在哪只手：left / right / auto（跟随输入图）。左右侧是镜像关系，
# 提示词不写死手，模型经常把武器换到另一只手上，或者干脆镜像过去。
WEAPON_HANDS = {"auto": "自动", "left": "左手", "right": "右手"}

# 侧视时挨着观众的那只手：角色朝画面左 → 看到的是左侧身体，左手在近处
SIDE_NEAR_HAND = {"left": "left", "right": "right", "side": "right"}


def _weapon_clause(view: str, params: dict) -> str:
    """武器段：写死握在哪只手，侧视再交代它在近侧还是被身体挡住的远侧。

    近侧的手武器压在身体前方、整把可见；远侧的手武器容易被身体吞掉，所以要
    单独说明「沿身体外侧露出」。不写清楚时，模型会把武器镜像到顺手的那一侧，
    左右两张图的手就对不上了。
    """
    hand = str(params.get("weapon_hand") or "auto")
    named = "左" if hand == "left" else "右"
    base = "手里的武器和身上装备原样保留：相同的形状、尺寸、配色、纹样，握法相同"
    # 第 ② 步的正文只写肯定句（否定词容易被告模型抄进画面），所以这里也用
    # 「和输入图是同一只手」「完整露出」这种正着说的写法
    if hand in ("left", "right"):
        hold = f"武器始终握在角色的{named}手上，和输入图是同一只手"
    else:
        hold = ("武器始终握在输入图握它的那只手上：输入图里在左手就画在左手，"
                "在右手就画在右手")

    if view in SIDE_NEAR_HAND:
        near = SIDE_NEAR_HAND[view]
        if hand in ("left", "right"):
            place = ("这只手在靠近观众的一侧，武器完整地画在身体前方，"
                     "从手一直延伸到画面下方"
                     if hand == near else
                     "这只手在身体的另一侧，武器沿身体外侧完整露出，"
                     "手、手臂到武器尖端都落在画面里")
        else:
            place = ("这只手在近侧时武器画在身体前方，在另一侧时沿身体外侧完整露出，"
                     "两种情况下都是同一只手、整把武器都完整可见")
        return f"{base}，{hold}；{place}。"

    tail = "整把武器从手一直延伸到身体外侧，轮廓在画面里清楚可见"
    if hand in ("left", "right"):
        # 正面 / 背面与观众是镜像：说清从观众看过去落在画面哪一边
        screen = "右" if (view == "front" and hand == "left") or (
            view == "back" and hand == "right") else "左"
        return f"{base}，{hold}（从观众看过去在画面{screen}侧），{tail}。"
    return f"{base}，{hold}，{tail}。"


def background_only(body: str, params: dict) -> str:
    """单帧用的一句话说清背景：纯色就交代颜色，透明走官方 RGBA 句式。

    和 apply_background 的区别是这里没有任何否定式补充说明——单帧提示词
    全文只写要什么，需要排除的东西交给负向提示词。
    """
    if background_mode(params) == "transparent":
        return rgba(body)
    c = video_background(params)
    return clean(f"{body} 背景是铺满整个画面的一整块均匀{color_name(c)}（{c}）。")


def frame_view(params: dict, label: str, view: str = "", view_note: str = "",
               note: str = "") -> str:
    """图生图：拿第 ① 步的正面立绘当输入，派生指定方向的单帧立绘。

    只在正面图上做文章是刻意的：三视图里三个立绘各占三分之一，切出来信息
    不全、不切又容易被照抄成一张三视图；单张正面图信息完整、构图唯一，
    模型只需要「转身」，不需要「取景」，产出稳定得多。

    提示词只写「要什么」：一条条都是肯定句（这个视角、这个人物、这些道具、
    这个背景），否定式要求全部交给负向提示词，正文里不出现「不要」——
    模型对否定词只会抄进画面，正着说一遍反而更稳。
    """
    name, pose = VIEW_DERIVE.get(view, (f"{label}", ""))
    if view in VIEW_DERIVE:
        pick = (f"以输入图中这个正面站立的角色为原型，画出同一个角色的{name}视角全身立绘，"
                f"{pose}。")
    else:
        pick = f"以输入图中这个正面站立的角色为原型，画出同一个角色的{label}视角全身立绘。"
    extra = f"视角要求：{clean(view_note)}。" if clean(view_note) else ""
    add = f"另外还要：{clean(note)}。" if clean(note) else ""
    keep = ("人物与输入图是同一个角色：外形、比例、身高、配色、服装、描边、画风全部一致。"
            + _weapon_clause(view, params))
    body = (
        f"{pick}{extra}{add}"
        f"{keep}"
        f"单人全身立绘，竖直方向占满画面高度，水平方向居于画面正中，脚底完整，"
        f"自然放松的待机站姿。"
        f"画风：{clean(params.get('style', ''))}。"
        f"轮廓干净，配色统一。"
    )
    return background_only(body, params)


# 第 ② 步的提示词一律正着说：正文里出现这些否定词，说明改写没按口径来
NEGATION_WORDS = ("不要", "不能", "不得", "不准", "禁止", "没有")


def has_negation(text: str) -> bool:
    return any(w in (text or "") for w in NEGATION_WORDS)


def frame_edit(params: dict, label: str, note: str) -> str:
    """图生图：在这张单帧上追加细节（换装备、加道具、改配色）。"""
    body = (
        f"保持输入图的角色、{label}视角、姿态、比例、构图和画风。"
        f"这一处改动：{clean(note)}。"
        f"单人全身立绘，居中构图，脚底完整，背景干净。"
        f"画风：{clean(params.get('style', ''))}。"
        f"轮廓干净，配色统一。"
    )
    return background_only(body, params)


def video(params: dict, *, loop: bool = True) -> str:
    """首尾帧循环视频：强调回到起始姿态、镜头固定，背景颜色保持不变。"""
    motion = clean(params.get("video_motion", "")) or DEFAULT_MOTION
    loop_hint = (
        "动画必须无缝循环：结尾的姿态与构图和第一帧完全一致。"
        if loop else "动作从首帧平滑过渡到尾帧。"
    )
    c = video_background(params)
    # 背景只作正面描述：一长串「不要变成紫色」这类否定列举反而会把紫色写进去
    # （实测中间几帧背景漂成了紫色），改成「每一帧都是同一块绿色」更稳。
    bg_hint = (
        f"整段视频每一帧的背景都是与首帧完全相同的一整块均匀{color_name(c)}（{c}），"
        f"从头到尾铺满画面、颜色始终如一，没有渐变、纹理、地面、阴影、场景和光效。"
    )
    # 动作幅度是视频阶段最容易翻车的地方：模型只想着把动作做大，一挥手
    # 武器就顶到画面边缘、一跳头顶就被切掉，切出来的帧直接废。
    room = (
        "整段动画的每一帧里，角色都完整地落在画面之内：头顶上方和脚下始终留着空白，"
        "手臂、武器、头发、衣摆伸到最远处时与画面四边仍有空隙，"
        "动作幅度收在画面范围里，做完整套动作也不出画。"
    )
    return clean(
        f"以首帧中的角色为主角的 2D 游戏精灵循环动画。{motion}。{loop_hint} "
        f"角色始终停留在画面同一位置、保持同样大小，只有身体和四肢在动。"
        f"{room}"
        f"镜头完全固定，构图不变，不要推拉、摇移、变焦、剪辑或场景切换。"
        f"{bg_hint}"
        f"不要额外角色、不要文字、不要字幕、不要粒子特效、不要动态模糊。"
        f"干净的 2D 游戏动画。"
    )


def sprite_redraw(params: dict) -> str:
    """图生图重绘：把视频帧重画成干净的透明精灵帧（始终输出 Alpha）。"""
    body = (
        "把输入图中的角色重绘成一张干净的 2D 游戏精灵动画帧。"
        "严格保持输入图里角色的姿态、轮廓、比例、配色、服装、五官和描边。"
        "彻底去除视频背景、地面、阴影、光晕、粒子、渐变和动态模糊。"
        "边缘清晰且抗锯齿，光照均匀，只有一个角色并居中。"
        f"画风：{clean(params.get('style', ''))}。"
        "不要文字、不要边框、不要重复的角色。"
    )
    return rgba(body)


def negative_for(params: dict, extra: str = "") -> str:
    base = clean(params.get("negative", ""))
    if background_mode(params) == "solid":
        base += ", 透明背景, 透明通道, 棋盘格, 半透明, 背景渐变, 背景纹理, 背景景物"
    return clean(base + (", " + extra if extra else ""))
