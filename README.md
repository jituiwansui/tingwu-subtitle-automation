# 通义听悟字幕自动化工具

这个工具直接调用通义听悟网页实际使用的 HTTP 接口，完成以下闭环：

1. 创建转写任务；
2. 将本地音视频 PUT 到听悟签发的阿里云 OSS 地址；
3. 通知听悟开始处理并轮询转写状态；
4. 创建 SRT 导出任务并轮询下载地址；
5. 下载到同目录的 `.part` 临时文件；
6. 校验 HTTP、文件非空、UTF-8 编码和 SRT 时间轴结构；
7. 原子重命名为最终 `.srt`；
8. 只有以上步骤全部成功，才永久删除听悟中的对应记录；
9. 再查一次任务列表，确认记录确实已删除。

上传、轮询、导出、下载和删除全程不启动、不控制浏览器。

## 运行环境

- Windows 10/11 x64
- 使用 `TingwuSubtitle.exe` 时不需要安装 Python
- 使用源码时需要 Python 3.10 或更高版本和 `requests` 2.x
- 可选：`ffprobe`。安装后程序会把媒体时长一并回传；没有它也能运行。
- 可选：Python 包 `py-mini-racer`（`pip install py-mini-racer`，已写入 `requirements.txt`）。安装后，当默认的降级值登录被阿里云风控拒绝时，程序会自动运行官方风控 SDK 生成真实令牌重试；不安装也能正常运行。

只运行 EXE 可跳过此步骤。使用 Python 源码时安装依赖：

```powershell
python -m pip install -r .\requirements.txt
```

程序所需的账号和登录材料全部放在程序根目录：

```text
TingwuSubtitle.exe  # Windows x64 原生启动器
runtime/            # 预展开运行环境，不能删除、移动或改名
config.json         # 明文账号、密码
auth.json           # 明文 Cookie 登录状态（登录成功后程序自动写入）
sdk/                # 官方风控 SDK 缓存（运行时自动下载，可删除）
```

程序运行时自动读取这两个文件，不依赖当前工作目录，也不需要另传账号参数。按照本项目的部署要求，两者均不加密；复制整个目录即可同时复制登录能力。

根目录 `TingwuSubtitle.exe` 是 64 位原生启动器，负责把拖放和命令行参数原样交给 `runtime/TingwuSubtitleCore.exe`，并原样返回核心程序退出码。核心使用 Python 3.12 和 Nuitka 4.1.3，通过 MinGW64/LTO 编译；Python Runtime、OpenSSL、扩展模块和 CA 证书都已提前展开在 `runtime/`，第一次启动不再解包，目标电脑也无需安装 Python 或 `requests`。请复制或解压整个目录，不能只拿走根目录 EXE。目标系统应为 Windows 10/11 x64。程序未做商业代码签名，其他电脑上的 SmartScreen 或杀毒软件可能要求用户确认允许运行。

> **重要安全提示：** `config.json` 和 `auth.json` 都是明文敏感文件。任何能读取本目录或压缩包的人，都可能取得账号密码或复用登录会话。请仅保存在可信电脑，不要上传网盘、Git 仓库、聊天群或交给无关人员；账号停用、泄露或交付他人后，应立即修改密码并退出所有会话。

## 最快用法：拖放运行

本工具不再包含 BAT 入口。直接使用根目录 `TingwuSubtitle.exe`；不要直接移动或重命名 `runtime/` 中的文件。也可运行源码 `tingwu_subtitle.py`。

方式一：把一个或多个音视频文件直接拖到 `TingwuSubtitle.exe` 图标上；使用源码时也可以拖到 `tingwu_subtitle.py` 图标上。程序随即开始上传和处理。

方式二：直接双击 `TingwuSubtitle.exe` 或 `tingwu_subtitle.py`。打开控制台后，把一个或多个音视频文件拖入黑色控制台窗口，再按 Enter 开始。处理结束后按 Enter 关闭窗口。

以上两种方式都不需要填写字幕目录。默认在每个源视频所在目录生成同名 `.srt`：

```text
D:\课程\第01讲.mp4
D:\课程\第01讲.srt
```

如果电脑双击 `.py` 时不是由 Python 打开，请先安装 Python，并让 `.py` 文件关联到 Python；也可以在本目录打开 PowerShell 运行：

```powershell
python -X utf8 .\tingwu_subtitle.py "D:\视频\课程.mp4"
```

批量处理多个文件时按顺序执行；每个文件分别输出到自己的源文件目录，并独立遵守“字幕成功后才删除”的规则：

```powershell
python -X utf8 .\tingwu_subtitle.py "D:\视频\01.mp4" "E:\课程\02.mp4"
```

默认行为：

- 语言：中文；
- 不区分发言人；
- 导出的 SRT 保留发言人和时间戳字段；
- 字幕默认输出到对应音视频文件的同一目录；
- 字幕验证成功后永久删除远端记录（不是只移到回收站）；
- 输入视频始终保留，不会被程序删除或修改。
- 控制台始终只显示一条连续的总进度条；上传、转写、导出、下载、校验和删除状态都在同一条进度上更新。

## 常用参数

```powershell
# 英语音频
python -X utf8 .\tingwu_subtitle.py "D:\audio\meeting.mp3" --lang en

# 2 人对话
python -X utf8 .\tingwu_subtitle.py "D:\audio\interview.mp3" --role-split-num 2

# 调试：字幕成功后暂时保留网页记录
python -X utf8 .\tingwu_subtitle.py "D:\video\test.mp4" --keep-remote

# 导出内容中不带发言人字段
python -X utf8 .\tingwu_subtitle.py "D:\video\test.mp4" --no-speaker

# 自定义最长转写等待时间（秒）
python -X utf8 .\tingwu_subtitle.py "D:\video\long.mp4" --transcribe-timeout 14400

# 可选：把字幕集中输出到指定目录
python -X utf8 .\tingwu_subtitle.py "D:\video\test.mp4" -o "D:\统一字幕"
```

语言参数：

| 参数 | 语言 |
|---|---|
| `cn` | 中文 |
| `en` | 英语 |
| `ja` | 日语 |
| `yue` | 粤语 |

发言人数参数：

| 参数 | 含义 |
|---|---|
| `-1` | 暂不区分，默认 |
| `1` | 单人演讲 |
| `2` | 2 人对话 |
| `0` | 多人讨论 |

## 登录状态

检查根目录 `auth.json` 中的会话是否有效：

```powershell
python -X utf8 .\tingwu_subtitle.py auth-status
```

如会话失效，直接刷新即可。程序会自动读取 `config.json` 中的账号和密码：

```powershell
python -X utf8 .\tingwu_subtitle.py auth-login
```

如需临时覆盖 `config.json` 中的密码，仍可使用环境变量：

```powershell
$env:TINGWU_PASSWORD = Read-Host "阿里云密码" -MaskInput
python -X utf8 .\tingwu_subtitle.py auth-login
Remove-Item Env:TINGWU_PASSWORD
```

账号也可通过 `--username` 临时覆盖。环境变量和命令行账号仅影响本次运行；登录刷新后，新的 Cookie 会原子写回根目录 `auth.json`。

阿里云账号密码登录所需的风控字段（`bx-ua`、`bx-umidtoken`）按以下策略自动处理：

1. 默认使用官方风控 SDK 自身的降级值 `not_loaded` 直接登录（服务端接受该值，速度最快，无任何额外依赖）；
2. 如果登录被风控拒绝，程序自动改用内嵌 V8 引擎（`py-mini-racer`）运行阿里云官方风控 SDK（awsc/fireyejs，运行时从阿里云 CDN 下载并缓存到 `sdk/`，版本变化自动跟进），在本地生成真实令牌后重试；成功后会记住该模式，后续登录直接走官方生成；
3. 全程不启动、不控制浏览器，不需要 Node.js 等任何额外软件，也不必预先在 `auth.json` 准备任何材料；环境变量 `TINGWU_BX_UA` / `TINGWU_BX_UMIDTOKEN` 仍可显式覆盖令牌。

如果两种方式都失败（例如阿里云主动触发额外风控：新设备、异地登录、滑块验证），程序会明确报错，按提示在浏览器手动登录一次该账号后重试即可。

如果移动或重命名配置文件，可在子命令前指定：

```powershell
python -X utf8 .\tingwu_subtitle.py --config-file "D:\safe\config.json" --auth-file "D:\safe\auth.json" process "D:\视频\课程.mp4"
```

## 安全与失败处理

- 账号密码保存在根目录明文 `config.json`；Cookie 登录状态保存在根目录明文 `auth.json`。
- 程序不会把密码、Cookie、风控字段、OSS 上传签名或字幕下载签名打印到日志。
- `.gitignore` 已忽略 `config.json` 和 `auth.json`，但这不能阻止手动复制、压缩或误传文件；请把整个目录和 ZIP 都按账号凭证管理。
- OSS 上传地址、字幕下载地址都是短期签名 URL，程序不会打印或保存这些地址。
- 在上传、转写、导出、下载、SRT 校验中的任何一步失败，程序都不会删除远端记录，并会打印 `transId` 便于手动处理。
- 本地最终字幕已经成功写入，但远端删除失败时，字幕仍会保留，程序返回非零退出码并报告错误。
- 默认永久删除是不可恢复操作，但只针对本次程序刚创建、且已经成功下载字幕的 `transId`。
- 当前上传实现使用 OSS 单次 PUT，单个文件上限为 5 GiB。听悟网页虽然允许部分视频达到 6 GB，但超过 5 GiB 的文件需要另行实现 OSS 分片上传；程序会在上传前停止，不会创建错误的“下载后删除”假象。

## 支持格式

视频：`mp4`、`wmv`、`m4v`、`flv`、`rmvb`、`dat`、`mov`、`mkv`、`webm`、`avi`、`mpeg`、`3gp`、`ogg`

音频：`mp3`、`wav`、`m4a`、`wma`、`aac`、`ogg`、`amr`、`flac`、`aiff`

## 退出码

- `0`：所有文件处理成功；
- `1`：登录失败、接口失败、文件校验失败、删除复查失败，或批次中至少一个文件失败。
- `2`：命令行参数错误，或非交互环境中没有提供参数。

建议在计划任务中检查 `%ERRORLEVEL%`，只把 `0` 当成完整成功。

供 AI Agent 或自动化脚本调用时，请直接阅读同目录的 `AI_CLI_GUIDE.md`；其中包含完整语法、参数顺序、退出码、失败语义和 PowerShell 示例。
