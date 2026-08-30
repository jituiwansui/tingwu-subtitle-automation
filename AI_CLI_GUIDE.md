# 通义听悟字幕工具：AI 命令行调用规范

本文档供 AI Agent、自动化脚本或命令行调用方阅读。目标程序为 Windows x64 目录版可执行程序 `TingwuSubtitle.exe`；也可用 Python 3.10+ 直接执行 `tingwu_subtitle.py`。

## 1. 工作目录与配套文件

交付目录必须整体保留，关键结构如下：

```text
TingwuSubtitle.exe
config.json
auth.json
runtime/
  TingwuSubtitleCore.exe
  python312.dll
  ...其他 DLL、PYD 和证书文件
```

- `config.json`：明文阿里云账号和密码。
- `auth.json`：明文 Cookie 和阿里云登录风控材料。
- `runtime/`：预展开运行环境。AI 不得删除、移动、重命名或只复制其中部分文件。
- 根目录 EXE 是参数转发启动器；它会等待核心进程结束，并原样返回核心退出码。
- EXE 会始终读取自身所在目录中的这两个文件，不依赖调用方当前工作目录。
- 这两个 JSON 都是敏感凭证。AI 不应读取、回显、记录或上传其内容。
- 使用 EXE 时不需要安装 Python，也不需要安装 `requests`。
- 第一次启动不会再把运行环境解压到用户缓存；ZIP 解压完成后即可直接运行。

## 2. 首选调用语法

处理一个文件，字幕默认写到源文件同目录：

```powershell
& "C:\工具\TingwuSubtitle.exe" "D:\视频\课程.mp4"
```

处理多个文件：

```powershell
& "C:\工具\TingwuSubtitle.exe" "D:\视频\01.mp4" "E:\录播\02.mkv"
```

程序会自动把“第一个参数是文件路径”的调用转换为 `process` 子命令。AI 优先使用这种简写形式。

等价的完整形式：

```powershell
& "C:\工具\TingwuSubtitle.exe" process "D:\视频\课程.mp4"
```

Python 源码调用形式：

```powershell
python -X utf8 "C:\工具\tingwu_subtitle.py" "D:\视频\课程.mp4"
```

## 3. 默认行为

对每个输入文件，程序按以下顺序执行：

1. 检查格式和文件大小；
2. 检查登录状态，必要时读取根目录配置自动登录；
3. 创建听悟任务并上传音视频；
4. 等待转写；
5. 导出、下载并校验 SRT；
6. 把最终字幕原子写入本地；
7. 永久删除本次创建的听悟远端记录；
8. 再查一次远端列表，确认删除成功。

默认输出规则：

```text
D:\课程\第01讲.mp4 -> D:\课程\第01讲.srt
```

程序只显示一条连续的总进度条。输入视频永远不会被程序删除或修改。

只要上传、转写、导出、下载或 SRT 校验任一步失败，程序就不会主动删除远端任务，并会在错误信息中给出 `transId`。如果字幕已落盘但远端删除失败，本地字幕仍会保留。

## 4. process 参数

```text
TingwuSubtitle.exe [FILE ...] [PROCESS_OPTIONS]
TingwuSubtitle.exe process FILE [FILE ...] [PROCESS_OPTIONS]
```

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `FILE ...` | 必填 | 一个或多个本地音视频文件；含空格的路径必须加引号。 |
| `-o DIR` / `--output-dir DIR` | 源文件目录 | 把所有 SRT 输出到指定目录。省略时每个字幕写到对应源文件目录。 |
| `--username USER` | `config.json` | 临时覆盖配置中的阿里云账号。通常不要传。 |
| `--password-env NAME` | `TINGWU_PASSWORD` | 指定读取密码的环境变量名。环境变量优先于 `config.json`。 |
| `--lang cn` | `cn` | 中文普通话。 |
| `--lang en` | — | 英语。 |
| `--lang ja` | — | 日语。 |
| `--lang yue` | — | 粤语。 |
| `--role-split-num -1` | `-1` | 暂不区分发言人。 |
| `--role-split-num 1` | — | 单人演讲。 |
| `--role-split-num 2` | — | 两人对话。 |
| `--role-split-num 0` | — | 多人讨论。 |
| `--transcribe-timeout SEC` | `7200` | 最长转写等待秒数。 |
| `--export-timeout SEC` | `300` | 最长字幕导出等待秒数。 |
| `--poll-interval SEC` | `3.0` | 转写状态查询间隔。 |
| `--no-speaker` | 关闭 | 导出内容不附带发言人字段。 |
| `--no-timestamp` | 关闭 | 导出内容不附带时间戳字段。 |
| `--keep-remote` | 关闭 | 调试选项：字幕成功后不删除听悟远端记录。除非用户明确要求，否则 AI 不应使用。 |

示例：

```powershell
# 英语视频，字幕仍写到视频同目录
& "C:\工具\TingwuSubtitle.exe" "D:\video\lesson.mp4" --lang en

# 两人对话
& "C:\工具\TingwuSubtitle.exe" "D:\访谈\采访.mp3" --role-split-num 2

# 统一字幕目录
& "C:\工具\TingwuSubtitle.exe" "D:\视频\01.mp4" "D:\视频\02.mp4" -o "D:\字幕"

# 最长等待四小时
& "C:\工具\TingwuSubtitle.exe" "D:\视频\长课.mp4" --transcribe-timeout 14400
```

## 5. 登录相关子命令

检查当前会话：

```powershell
& "C:\工具\TingwuSubtitle.exe" auth-status
```

使用根目录 `config.json` 中的账号密码刷新会话：

```powershell
& "C:\工具\TingwuSubtitle.exe" auth-login
```

临时用环境变量覆盖密码：

```powershell
$env:TINGWU_PASSWORD = "临时密码"
try {
    & "C:\工具\TingwuSubtitle.exe" auth-login
} finally {
    Remove-Item Env:TINGWU_PASSWORD -ErrorAction SilentlyContinue
}
```

AI 不应把密码直接拼入命令行参数；程序也没有 `--password` 参数。

## 6. 全局配置参数

全局参数必须放在子命令之前：

```powershell
& "C:\工具\TingwuSubtitle.exe" `
  --config-file "D:\安全目录\config.json" `
  --auth-file "D:\安全目录\auth.json" `
  process "D:\视频\课程.mp4"
```

| 参数 | 说明 |
|---|---|
| `--config-file PATH` | 覆盖默认明文账号密码文件路径。 |
| `--auth-file PATH` | 覆盖默认明文 Cookie/风控材料文件路径。 |

使用自定义全局参数时必须显式写出 `process`，不要使用文件路径简写。

## 7. 支持格式与限制

视频格式：`mp4`、`wmv`、`m4v`、`flv`、`rmvb`、`dat`、`mov`、`mkv`、`webm`、`avi`、`mpeg`、`3gp`、`ogg`

音频格式：`mp3`、`wav`、`m4a`、`wma`、`aac`、`ogg`、`amr`、`flac`、`aiff`

- 单文件最大 5 GiB；超过限制时会在创建远端任务前停止。
- 必须能访问听悟、阿里云登录和阿里云 OSS 网络地址；如启用了官方风控令牌生成（安装了 Python 包 `py-mini-racer`），还需能访问阿里云 CDN `g.alicdn.com` 与 `ynuf.aliapp.org`。
- 风控字段 `bx-ua` / `bx-umidtoken` 无需准备：程序优先用官方 SDK 在内嵌 V8 中本地生成，条件不满足时自动使用官方降级值 `not_loaded`。
- 复制到新电脑后，如果阿里云判断 Cookie 或风控材料失效，自动账号密码登录仍可能触发额外风控；此时程序会明确报错，不会绕过阿里云验证。

## 8. 退出码与自动化判定

| 退出码 | 含义 |
|---:|---|
| `0` | 所有输入文件均完成字幕下载、SRT 校验，以及默认情况下的远端删除复查。 |
| `1` | 登录、接口、上传、转写、导出、下载、校验、删除或批处理中至少一个文件失败。 |
| `2` | 命令行参数错误，或非交互环境中没有提供参数。 |

PowerShell 自动化示例：

```powershell
& "C:\工具\TingwuSubtitle.exe" "D:\视频\课程.mp4"
if ($LASTEXITCODE -ne 0) {
    throw "听悟字幕处理失败，退出码：$LASTEXITCODE"
}
```

AI 必须以进程退出码为最终成功判据，不要仅根据控制台中出现“下载”或“100%”字样判断成功。默认模式下，只有字幕校验与远端删除复查都完成后才返回 `0`。
