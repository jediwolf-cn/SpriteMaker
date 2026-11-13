# -*- coding: utf-8 -*-
"""SpriteMaker 命令行入口。

  python main.py web                      启动 Web UI（推荐）
  python main.py check                    检查 ComfyUI / ffmpeg / 工作流
  python main.py new --name slime --character "..."   新建项目
  python main.py run --name slime --stage all        全自动跑完整流水线
  python main.py run --name slime --stage keyframe   从正面立绘派生各方向单帧
  python main.py run --name slime --stage sprites --force
  python main.py select --name slime --every 3       按间隔挑帧
  python main.py videos --name slime                 列出单帧 / 视频段 / 帧组
  python main.py run --name slime --stage frames --frame-source walk_front_20261105_120000.mp4
  python main.py status --name slime                 查看阶段状态
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from spritemaker import config, ffmpeg_tools, prompts, workflows  # noqa: E402
from spritemaker.comfy import ComfyClient  # noqa: E402
from spritemaker.pipeline import Pipeline  # noqa: E402
from spritemaker.project import create_project, get_project, list_projects  # noqa: E402
from spritemaker.server import health, serve  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def cmd_check(_args) -> int:
    h = health()
    print("=" * 60)
    print(f"ComfyUI   : {h['comfy']}  {h['comfy_url']}"
          + (f"  (v{h.get('comfy_version', '?')})" if h["comfy"] == "ok" else ""))
    print(f"ffmpeg    : {h['ffmpeg']}  {h.get('ffmpeg_path', '')}")
    print(f"工作流    : {json.dumps(h['workflows'], ensure_ascii=False)}")
    for w in h.get("video_workflows") or []:
        mark = "默认" if w["default"] else "可选"
        print(f"  视频工作流 [{mark}] {w['name']} ({w['file']}) "
              f"{'可用' if w['compatible'] else '不兼容：' + w['reason']}")
    for e in h["errors"]:
        print("  ! " + e)
    ok = h["comfy"] == "ok" and h["ffmpeg"] == "ok" and len(h["workflows"]) == 3 and not h["errors"]
    print("=" * 60)
    print("自检通过，可以开始生成。" if ok else "自检未通过，请按上面提示修复。")
    return 0 if ok else 1


def cmd_web(args) -> int:
    serve(args.host, args.port)
    return 0


def cmd_presets(args) -> int:
    """列出中文动作预设；可用 --key 取单条，配合 --name 直接写入项目。"""
    items = prompts.ACTION_PRESETS
    if args.key:
        text = prompts.preset_motion(args.key)
        if not text:
            raise SystemExit(f"未知动作：{args.key}（可选：{', '.join(x['key'] for x in items)}）")
        if args.name:
            get_project(args.name).update_params({"video_motion": text})
            p = next(x for x in items if x["key"] == args.key)
            log(f"项目 {args.name} 的动作已设为「{p['label']}」")
        else:
            print(text)
        return 0
    for item in items:
        print(f"  {item['key']:<10} {item['label']:<8} {item['motion']}")
    print("\n应用：python main.py presets --key run --name 项目名")
    return 0


def cmd_new(args) -> int:
    params = {}
    if args.character:
        params["character"] = args.character
    if args.style:
        params["style"] = args.style
    if args.detail:
        # 追加细节的编辑指令：默认对四个方向都生效
        params["frame_edit_prompts"] = {d: args.detail for d in ("front", "back", "left", "right")}
    for kv in args.set or []:
        if "=" not in kv:
            raise SystemExit(f"--set 需要 key=value 格式：{kv}")
        k, v = kv.split("=", 1)
        try:
            params[k] = json.loads(v)
        except json.JSONDecodeError:
            params[k] = v
    p = create_project(args.name, params)
    log(f"项目已创建：{p.dir}")
    return 0


def _project(args):
    return get_project(args.name)


def cmd_source(args) -> int:
    """上传角色底图：正面设定阶段可据此改用图生图生成正面立绘。"""
    p = _project(args)
    src = Path(args.file)
    if not src.is_file():
        raise SystemExit(f"找不到底图文件：{src}")
    p.save_reference_image(src.read_bytes())
    log(f"底图已保存：{p.dir / '00_source' / 'base.png'}（已开启底图图生图）")
    return 0


def cmd_params(args) -> int:
    p = _project(args)
    patch = {}
    for kv in args.set or []:
        if "=" not in kv:
            raise SystemExit(f"--set 需要 key=value 格式：{kv}")
        k, v = kv.split("=", 1)
        try:
            patch[k] = json.loads(v)
        except json.JSONDecodeError:
            patch[k] = v
    p.update_params(patch)
    print(json.dumps(p.params, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args) -> int:
    p = _project(args)
    patch = {}
    if args.video_workflow is not None:
        patch["video_workflow"] = args.video_workflow
    if args.video_label is not None:
        patch["video_label"] = args.video_label
    if args.frame_source is not None:
        patch["frame_source"] = args.frame_source
    if patch:
        p.update_params(patch)
    pipe = Pipeline(p, log=log, force=args.force)
    try:
        if args.stage == "all":
            done = pipe.run_all()
            log(f"全部阶段完成：{' → '.join(done)}")
        else:
            result = pipe.run(args.stage)
            if args.stage == "sprites" and result.get("groups"):
                for g in result["groups"]:
                    log(f"  · {g['label']}：{g['count']} 帧，不透明 {g['opaque_avg']:.1%}"
                        f" → 06_sprites/{g['group'] + '/' if g['group'] else ''}")
                log(f"去背景完成：{len(result['groups'])} 组 / {result['count']} 帧（各组独立目录）")
            elif args.stage == "export" and result.get("groups"):
                for g in result["groups"]:
                    log(f"  · {g['label']}：{g['frames']} 帧 → {g['zip_path']}")
                log(f"导出完成：{result['count']} 组 / {result['frames']} 帧（每组一套 spritesheet / GIF / ZIP）")
            else:
                log(f"阶段 {args.stage} 完成：{json.dumps(result, ensure_ascii=False)[:400]}")
    except Exception as exc:  # noqa: BLE001
        log(f"失败：{exc}")
        return 2
    return 0


def cmd_select(args) -> int:
    p = _project(args)
    root = p.dir_of("frames")
    frames = sorted(x.relative_to(root).as_posix() for x in root.rglob("frame_*.png"))
    if args.group is not None:
        frames = [f for f in frames
                  if (f.startswith(args.group + "/") if args.group else "/" not in f)]
    if not frames:
        raise SystemExit("还没有切帧，请先执行：python main.py run --stage frames")
    if args.frames:
        picked = [f.strip() for f in args.frames.split(",") if f.strip()]
    elif args.every:
        picked = frames[:: max(1, args.every)][: args.limit or 999]
    else:
        picked = frames
    chosen = p.set_selection(picked)
    log(f"已选中 {len(chosen)} 帧：{', '.join(chosen[:12])}{' ...' if len(chosen) > 12 else ''}")
    return 0


def cmd_videos(args) -> int:
    """列出单帧、视频段与帧组，便于在 CLI 选首尾帧和切帧源。"""
    p = _project(args)
    keys = p.keys()
    if keys:
        cur_first = str(p.params.get("first_frame") or "")
        cur_last = str(p.params.get("last_frame") or "")
        first = cur_first or keys[0]["id"]
        print(f"单帧（首帧：{first} / 尾帧：{cur_last or first + '（同首帧）'}）：")
        for one in keys:
            marks = []
            if one["id"] == first:
                marks.append("首帧")
            if one["id"] == (cur_last or first):
                marks.append("尾帧")
            tail = f"  ← {', '.join(marks)}" if marks else ""
            print(f"  · {one['label']}（{one['id']}）{one['file']}{tail}")
    else:
        print("还没有单帧，请先执行：python main.py run --stage keyframe")
    records = p.video_records()
    if not records:
        print("还没有视频段，请先执行：python main.py run --stage video")
        return 0
    cur = str(p.params.get("frame_source") or "")
    print(f"项目 {p.name} 共 {len(records)} 段视频（当前切帧源：{cur or '最新一段'}）")
    for i, r in enumerate(records, 1):
        flag = " ← 切帧源" if r["file"] == cur or (not cur and i == len(records)) else ""
        print(f"  {i:>2}. {r.get('label', r['file'])}"
              f"\n      file={r['file']}  工作流={r.get('workflow_label', '') or r.get('workflow', '')}"
              f"  方向={r.get('direction', '')}  时间={r.get('created', '')}{flag}")
    groups = p.frame_groups()
    if groups:
        print("帧组：" + "，".join(f"{g['id'] or '旧版帧序列'}({g['count']} 帧)" for g in groups))
    print("\n指定切帧源：python main.py run --name 项目名 --stage frames --frame-source <file>")
    return 0


def cmd_status(args) -> int:
    if args.name:
        p = _project(args)
        d = p.manifest()
        print(f"项目：{p.name}  目录：{p.dir}")
        print(f"角色：{p.params.get('character', '')}")
        for stage, st in d["stages"].items():
            count = len(d["files"].get(stage if stage != "sprites_raw" else "sprites_raw", []))
            print(f"  {stage:<10} {st.get('status', 'idle'):<8} 文件 {count:>3}"
                  + (f"  {st.get('error', '')[:80]}" if st.get("error") else ""))
        print(f"已选帧：{len(d['selection'])}  ({', '.join(d['selection'][:8])} ...)")
    else:
        for item in list_projects():
            print(f"  {item['name']:<28} {item['updated']}  {item['character'][:40]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="2D 游戏精灵动画制作（ComfyUI + ffmpeg）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("web", help="启动 Web UI")
    w.add_argument("--host", default=None)
    w.add_argument("--port", type=int, default=None)
    w.set_defaults(func=cmd_web)

    sub.add_parser("check", help="检查依赖与远程 ComfyUI").set_defaults(func=cmd_check)

    pr = sub.add_parser("presets", help="查看/应用中文动作预设")
    pr.add_argument("--key", default="", help="动作 key，如 idle / walk / run / attack")
    pr.add_argument("--name", default="", help="把该动作写入指定项目")
    pr.set_defaults(func=cmd_presets)

    n = sub.add_parser("new", help="新建项目")
    n.add_argument("--name", required=True)
    n.add_argument("--character", default="")
    n.add_argument("--style", default="")
    n.add_argument("--detail", default="", help="追加细节的编辑指令（如：手里加一把发光的长剑）")
    n.add_argument("--set", action="append", help="覆盖任意参数 key=value（可重复）")
    n.set_defaults(func=cmd_new)

    r = sub.add_parser("run", help="执行流水线阶段")
    r.add_argument("--name", required=True)
    r.add_argument("--stage", default="all",
                   choices=["all", "reference", "keyframe", "video", "frames", "sprites", "export"])
    r.add_argument("--force", action="store_true", help="强制重跑，覆盖已有产物")
    r.add_argument("--video-workflow", default=None, help="指定 workfllow/ 下的视频工作流文件名")
    r.add_argument("--video-label", default=None, help="新视频段的自定义名称")
    r.add_argument("--frame-source", default=None, help="切帧所选视频文件名（留空=最新一段）")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("select", help="挑选帧")
    s.add_argument("--name", required=True)
    s.add_argument("--every", type=int, default=0)
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--frames", default="", help="逗号分隔的帧相对路径")
    s.add_argument("--group", default=None, help="只操作某个帧组（视频段 id）")
    s.set_defaults(func=cmd_select)

    vs = sub.add_parser("videos", help="列出视频段与帧组")
    vs.add_argument("--name", required=True)
    vs.set_defaults(func=cmd_videos)

    st = sub.add_parser("status", help="查看项目状态")
    st.add_argument("--name", default="")
    st.set_defaults(func=cmd_status)

    src = sub.add_parser("source", help="设置角色底图（参考图改用图生图）")
    src.add_argument("--name", required=True)
    src.add_argument("--file", required=True, help="本地图片路径")
    src.set_defaults(func=cmd_source)

    pp = sub.add_parser("params", help="修改项目参数")
    pp.add_argument("--name", required=True)
    pp.add_argument("--set", action="append", required=True)
    pp.set_defaults(func=cmd_params)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
