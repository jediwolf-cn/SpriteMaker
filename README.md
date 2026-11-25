# SpriteMaker — 基于 ComfyUI 的 2D 精灵动画生成工具

从一段角色描述（或一张参考图）出发，走完「正面设定 → 四向单帧 → 循环视频 → 切帧 → 挑帧 → 抠图 → 导出」七步，产出可直接用于游戏的 Transparent PNG 序列、spritesheet、循环 GIF 和 ZIP 包。

## 前置要求

- **ComfyUI** 已安装并启动，默认监听 `http://127.0.0.1:8000`（可用 API 格式工作流，节点需包含下列模型）。
- **Python 3.10+**，唯一第三方依赖是 Pillow（`pip install Pillow`，`start.bat` 会自动安装）。其余全部使用标准库。
- **ffmpeg.exe** 放在 `tools/` 目录（该文件不入库，需自行准备；`python main.py check` 可确认）。
- 可选：填写 DeepSeek API Key 开启提示词自动改写；不填也能用内置中文模板。

工作流依赖的模型（需放在 ComfyUI 对应模型目录）：

| 工作流 | 需要的模型 |
| --- | --- |
| Qwen Image 2.1 Turbo 文生图 / 图生图 | `qwen_image_2.1_turbo_int8_convrot.safetensors`、`qwen3vl_8b_int8_convrot.safetensors`、`qwen_image_2.1_vae_bf16.safetensors`、对应的 t2i / i2i LoRA |
| Pika H3 Flex FL2VA（默认视频工作流） | `minimax_h3_fl2va_pruned_int8_convrot.safetensors`、`qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`、`minimax_h3_video_vae_int8_convrot.safetensors`、`minimax_h3_audio_vae_fp32.safetensors`、`pika_h3_flex_fl2va_v1_bf16.safetensors` |
| MiniMax H3 首尾帧（可选） | `minimax_h3_fl2va_pruned_w6a8.safetensors`、同上 CLIP / VAE，以及 8 步加速 LoRA |

## 快速开始

Windows 下双击 **`start.bat`**，或手动执行：

```bash
pip install Pillow          # 若未安装
python main.py web
```

浏览器打开 **<http://127.0.0.1:8787>**。右上角新建项目，填角色描述，然后按左侧流水线依次执行即可。

先跑一次依赖自检（会检查 ComfyUI、ffmpeg、工作流是否齐备）：

```bash
python main.py check
```

## 七步流水线

| 步骤 | 做什么 | 主要产物 |
| --- | --- | --- |
| ① 正面设定 | 生成一张正面全身立绘作为画风 / 角色基准；可上传底图走图生图 | `01_reference/` |
| ② 单帧 | 以正面图为底图派生 **正面 / 背面 / 左侧 / 右侧** 四个方向，可追加自定义角度；每张图可单独重绘 | `02_keyframe/*.png` |
| ③ 首尾帧循环视频 | 选首帧、可选尾帧（不选则首尾同帧无缝循环），设定动作 / 时长 / 种子 / 留白；每次点击都新增一段视频 | `03_video/` |
| ④ ffmpeg 切帧 | 选一段视频切成 PNG 帧组，每个视频段对应独立帧组 | `04_frames/<视频段>/` |
| ⑤ 挑帧 | 像翻书一样预览动作连贯性，勾选最终要重绘的帧 | 选择记录在 `project.json` |
| ⑥ 重绘 / 抠图去背景 | 图生图重绘或色键抠纯色背景，生成透明精灵帧；可按帧组单独处理 | `05_sprites_raw/`、`06_sprites/` |
| ⑦ 预览与导出 | 每组导出 spritesheet、循环 GIF、ZIP 包，页面内并排预览与下载 | `export/<帧组>/` |

**关键约定**

- 第 ② 步默认四向：**正面 / 背面 / 左侧 / 右侧**；左右侧都是正侧面，区别只在面朝画面哪一边。有武器时可在面板里指定「武器在哪只手」（自动 / 左手 / 右手），提示词会按视角交代近远关系。
- 第 ③ 步默认静音：提示词写明无配乐无人声，同时断开 `CreateVideo` 的音频输入（MiniMax H3 会原生配音，只写提示词关不掉）。视频种子默认按段递增，可固定；每段卡片可查看种子并「用于下次生成」。
- 第 ④ 步选不同视频段时，⑤ 的帧序列会一起切到对应的帧组；未切帧的段会显示空态提示。
- 第 ⑥ ⑦ 步的「处理范围 / 导出范围」可按帧组单独执行；删除也是按组删（项目级删除在页面右上角）。重绘会覆盖同名文件。
- 透明帧统一按「统一基准」缩放，单组重绘也不会忽大忽小。

## 配置

所有设置都在页面右上角「设置」里，或直接改 **`config.json`**。

> ⚠️ `config.json` 会**整份覆盖**代码里的默认值。改完默认参数后，已经存在的项目不会自动变化——参数保存在 `projects/<项目>/project.json`，需要点前端快捷按钮或手动改参数才会写入。

常用项：

| 配置项 | 说明 |
| --- | --- |
| `comfy_url` / `comfy_user` / `comfy_password` | ComfyUI 地址与认证；远程 502 / 超时会自动重试 |
| `comfy_subfolder` | 上传图片放到 ComfyUI input 的子目录，默认 `spritemaker` |
| `workflow_dir` | 工作流目录，默认 `workfllow/`（目录名就是这个拼写） |
| `workflows.i2v` | 默认视频工作流文件名；③ 的下拉可选择任意一份兼容的工作流 |
| `prompt_optimize` / `deepseek_*` | 提示词自动改写（把模板改写成更自然的中文正文，硬性约束会保留） |
| `defaults.*` | 新建项目的默认参数：画布、背景色、四个方向、武器手、视频时长 / 清晰度 / 留白 / 静音、抠图阈值、导出列数等 |

把新的 ComfyUI API 格式工作流放进 `workfllow/` 刷新页面即可；节点 ID 不同也能自动识别，不兼容的会在下拉里标出原因。

## 目录结构

```
SpriteMaker/
├── main.py                # CLI 入口（web / check / new / run / select / ...）
├── config.json            # 全局配置与默认参数（覆盖代码默认值）
├── workfllow/             # ComfyUI API 格式工作流（注意拼写）
├── projects/<项目名>/
│   ├── project.json       # 该项目参数、选择与各阶段记录
│   ├── 00_source/ 01_reference/ 02_keyframe/ 03_video/
│   ├── 04_frames/<视频段>/        # 每段视频各自的帧组
│   ├── 05_sprites_raw/<帧组>/     # 抠图原图（统一缩放基准）
│   ├── 06_sprites/<帧组>/         # 透明精灵帧 + alpha_report.json
│   ├── export/<帧组>/             # spritesheet.png/.json、preview.gif、ZIP
│   └── logs/                      # 每次任务的日志
├── tools/ffmpeg.exe
└── web/                   # 前端页面（静态文件，改动后刷新即可）
```

## 命令行

Web UI 之外也能跑流水线，适合批处理：

```bash
python main.py new --name 测试 --character "银发女剑士" --style "像素风"
python main.py presets                        # 查看内置中文动作预设
python main.py run --name 测试 --stage reference
python main.py run --name 测试 --stage all    # 跑完整流水线
python main.py select --name 测试 --group <帧组> --every 2 --limit 8
python main.py videos --name 测试             # 列出视频段与帧组
python main.py status --name 测试
python main.py params --name 测试 --set video_seconds=2
```

`python main.py <命令> --help` 查看完整参数。

## 常见问题

- **点「取消」后远程还在跑？** 新版本会按本任务的 prompt_id 中断 ComfyUI 里的排队项和运行中任务，不会影响你手动提交的其它任务；本地步骤也会立即停。
- **成片有音轨？** 确认③的「视频静音」已勾选；它除了提示词约束，还会断开 `CreateVideo.audio`。
- **视频分辨率和填的 MP 不一致？** 默认工作流带像素缩放节点（`ImageScaleToTotalPixels`）并按「清晰度跟随画布」提高 MP；旧工作流或不带缩放节点的工作流会直接用扩画布后的首帧尺寸。
- **端口 8787 起不来？** 多半是旧进程还占着端口，结束占用该端口的 Python 进程后重启。
