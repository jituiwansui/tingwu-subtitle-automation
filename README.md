# 通义听悟字幕自动化工具

上传本地音视频到通义听悟，自动转写、下载 SRT 字幕，成功后永久删除远端记录。全程不启动、不控制浏览器。

对每个文件依次完成以下闭环：

1. 创建转写任务；
2. 将本地文件 PUT 到听悟签发的阿里云 OSS 地址；
3. 通知听悟开始处理并轮询转写状态；
4. 创建 SRT 导出任务并轮询下载地址；
5. 下载到同目录的 `.part` 临时文件，校验 HTTP、非空、UTF-8 编码和 SRT 时间轴结构；
6. 原子重命名为最终 `.srt`；
7. 只有以上全部成功，才永久删除听悟中的对应记录，并再查一次任务列表确认删除。
8. 输入为 TS 时，再次校验最终 SRT 非空、UTF-8 且具有合法时间轴，确认无误后删除原始 TS；同名 MP4 永久保留。

## 运行环境

- Windows 10/11 x64
- 使用编译好的 EXE 时**不需要安装 Python 或任何其他软件**（内嵌 V8 引擎等依赖已全部打包；TS 转换所需的 `ffmpeg.exe` 已放在程序根目录）
- 使用源码时需要 Python 3.10+，并安装 `requirements.txt` 中的依赖：

```powershell
python -m pip install -r .\requirements.txt
```

随程序提供的组件：

- `ffmpeg.exe`：根目录中的独立便携版，仅在输入为 `.ts` 时用于无损换封装为 MP4；分发程序时必须一起复制。

可选组件（没有也能运行）：

- `ffprobe`：安装后程序会把媒体时长一并回传。
- Python 包 `py-mini-racer`（已写入 `requirements.txt`，仅源码运行需要手动安装）：当默认的降级值登录被阿里云风控拒绝时，程序会用它运行官方风控 SDK 生成真实令牌重试。

## 文件结构

```text
TingwuSubtitle.exe        # Nuitka 编译的单文件程序
ffmpeg.exe                # 便携式 FFmpeg；TS 极速转 MP4 必需，和 EXE 放在同一目录
FFmpeg-LICENSE.txt        # FFmpeg 的 GPLv3 许可证
FFmpeg-NOTICE.txt         # FFmpeg 版本、来源与源码地址
tingwu_subtitle.py        # 源码
config.json               # 明文账号、密码
auth.json                 # 明文 Cookie 登录状态（登录成功后程序自动写入）
sdk/                      # 官方风控 SDK 缓存（需要时自动下载，可删除）
requirements.txt          # 源码运行的 pip 依赖
```

程序始终把 `config.json`、`auth.json`、`sdk/` 放在 EXE（或源码）所在目录读写，不依赖当前工作目录。分发时建议复制整个目录；支持 TS 的最小集合是 **`TingwuSubtitle.exe` + `ffmpeg.exe` + `config.json` + 两个 FFmpeg 说明文件**。`auth.json` 和 `sdk/` 不存在时会自动登录、自动下载。

程序未做商业代码签名，其他电脑上的 SmartScreen 或杀毒软件可能要求用户确认允许运行。当前 Nuitka 单文件已包含 Python 所需的 VC 运行库，目标系统为 Windows 10/11 x64，不需要另外安装运行环境。

> **重要安全提示：** `config.json` 和 `auth.json` 都是明文敏感文件。任何能读取本目录或压缩包的人，都可能取得账号密码或复用登录会话。请仅保存在可信电脑，不要上传网盘、Git 仓库、聊天群或交给无关人员；账号停用、泄露或交付他人后，应立即修改密码并退出所有会话。

## 快速开始

方式一：把一个或多个音视频文件**拖到 EXE（或 `tingwu_subtitle.py`）图标上**，程序随即开始上传处理。

方式二：**双击** EXE 或源码，在打开的控制台窗口里把文件拖进去，按 Enter 开始；结束后按 Enter 关闭窗口。

默认在每个源文件所在目录生成同名 `.srt`：

```text
D:\课程\第01讲.mp4  →  D:\课程\第01讲.srt
D:\课程\第02讲.ts   →  D:\课程\第02讲.mp4 + D:\课程\第02讲.srt，成功后删除原 TS
```

`.ts` 输入会在创建远端任务前，使用根目录 `ffmpeg.exe` 直接复制 H.264/AAC 码流为同目录、同名 MP4，不重新编码，因此不会损失画质或音质。MP4 会永久保留；只有上传、转写、SRT 下载及结构校验、远端清理全部成功后，程序才会再次确认最终 SRT 非空并删除原始 TS。任一步失败都会保留原始 TS 和已生成的 MP4。若同名 MP4 已存在，程序只会在文件不早于 TS 且时长匹配时安全复用，绝不会直接覆盖。

命令行方式（AI Agent、脚本调用也用这套语法）：

```powershell
# 处理一个文件（首选简写：第一个参数是文件路径即可）
.\TingwuSubtitle.exe "D:\视频\课程.mp4"

# 处理多个文件，各自输出到自己的源文件目录
.\TingwuSubtitle.exe "D:\视频\01.mp4" "E:\课程\02.mkv"

# 等价完整形式
.\TingwuSubtitle.exe process "D:\视频\课程.mp4"

# 源码方式
python -X utf8 .\tingwu_subtitle.py "D:\视频\课程.mp4"
```

默认行为：

- 语言中文、暂不区分发言人；
- SRT 保留发言人和时间戳字段；
- 字幕输出到源文件同目录；
- 字幕校验成功后永久删除远端记录（不是移回收站）；
- 非 TS 输入文件永远不会被程序删除或修改；
- TS 会先无损换封装为同目录、同名 MP4 并永久保留；最终 SRT 再次校验通过后删除原始 TS；
- 控制台只显示一条连续总进度条，上传、转写、导出、下载、校验、删除都在其上更新。

## 命令行参数

```text
TingwuSubtitle.exe [全局参数] <命令> [命令参数]
TingwuSubtitle.exe [FILE ...] [process 参数]          # 简写，最常用
TingwuSubtitle.exe process FILE [FILE ...] [参数]     # 完整形式
```

### process 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `FILE ...` | 必填 | 一个或多个本地音视频文件；含空格的路径必须加引号 |
| `-o DIR` / `--output-dir DIR` | 源文件目录 | 把所有 SRT 统一输出到指定目录 |
| `--lang cn` | `cn` | 中文普通话 |
| `--lang en` | — | 英语 |
| `--lang ja` | — | 日语 |
| `--lang yue` | — | 粤语 |
| `--role-split-num -1` | `-1` | 暂不区分发言人 |
| `--role-split-num 1` | — | 单人演讲 |
| `--role-split-num 2` | — | 两人对话 |
| `--role-split-num 0` | — | 多人讨论 |
| `--transcribe-timeout SEC` | `7200` | 最长转写等待秒数 |
| `--export-timeout SEC` | `300` | 最长字幕导出等待秒数 |
| `--poll-interval SEC` | `3.0` | 转写状态查询间隔 |
| `--no-speaker` | 关闭 | 导出内容不附带发言人字段 |
| `--no-timestamp` | 关闭 | 导出内容不附带时间戳字段 |
| `--username USER` | `config.json` | 临时覆盖阿里云账号，通常不要传 |
| `--password-env NAME` | `TINGWU_PASSWORD` | 指定读取密码的环境变量名，环境变量优先于 `config.json` |
| `--keep-remote` | 关闭 | 调试用：字幕成功后保留远端记录 |

示例：

```powershell
# 英语音频
.\TingwuSubtitle.exe "D:\audio\meeting.mp3" --lang en

# 两人对话
.\TingwuSubtitle.exe "D:\audio\interview.mp3" --role-split-num 2

# 字幕统一输出到指定目录
.\TingwuSubtitle.exe "D:\视频\01.mp4" "D:\视频\02.mp4" -o "D:\统一字幕"

# 长视频：最长等待四小时
.\TingwuSubtitle.exe "D:\视频\长课.mp4" --transcribe-timeout 14400

# 调试：保留网页端记录
.\TingwuSubtitle.exe "D:\video\test.mp4" --keep-remote
```

### 登录子命令

```powershell
# 检查 auth.json 中的会话是否有效（无效返回退出码 1）
.\TingwuSubtitle.exe auth-status

# 用 config.json 的账号密码刷新会话
.\TingwuSubtitle.exe auth-login
```

### 全局参数（必须放在子命令之前）

```powershell
.\TingwuSubtitle.exe --config-file "D:\safe\config.json" --auth-file "D:\safe\auth.json" process "D:\视频\课程.mp4"
```

| 参数 | 说明 |
|---|---|
| `--config-file PATH` | 覆盖默认明文账号密码文件路径 |
| `--auth-file PATH` | 覆盖默认明文 Cookie 登录文件路径 |

使用自定义全局参数时必须显式写出 `process`，不要使用文件路径简写。

## 登录与风控令牌机制

- 账号密码读自根目录 `config.json`；也可用环境变量临时覆盖密码（不影响本次以外的运行）：

```powershell
$env:TINGWU_PASSWORD = Read-Host "阿里云密码" -MaskInput
.\TingwuSubtitle.exe auth-login
Remove-Item Env:TINGWU_PASSWORD
```

- 登录刷新后，新的 Cookie 会原子写回 `auth.json`，并自动收紧为仅当前 Windows 用户可读。
- 阿里云登录的风控字段 `bx-ua` / `bx-umidtoken` 由程序自动处理，无需手工准备：
  1. 默认使用官方风控 SDK 自身的降级值 `not_loaded` 直接登录（服务端接受，速度最快）；
  2. 若登录被风控拒绝，自动改用内嵌 V8 引擎运行阿里云官方风控 SDK（awsc/fireyejs，运行时从阿里云 CDN 下载缓存到 `sdk/`，版本变化自动跟进）在本地生成真实令牌后重试；成功后记住该模式，后续登录直接走官方生成；
  3. 若官方生成也失败（例如官方更新了加密方式），程序会自动回退并明确报错。
- 环境变量 `TINGWU_BX_UA` / `TINGWU_BX_UMIDTOKEN` 可显式覆盖令牌（一般不需要）。
- 排查令牌生成问题可设 `TINGWU_DEBUG_BX=1` 打印内部异常。
- 只有当阿里云主动触发额外风控（新设备、异地登录等）时登录才可能失败；此时按提示在浏览器手动登录一次该账号再重试。

## 安全与失败处理

- 程序不会把密码、Cookie、风控字段、OSS 上传签名或字幕下载签名打印到日志。
- OSS 上传地址、字幕下载地址都是短期签名 URL，程序不打印、不保存。
- TS 转换失败时不会创建远端任务；上传、转写、导出、下载、校验任一步失败，都不会删除原始 TS 或远端记录，并打印 `transId` 便于手动处理；已成功转换的 MP4 会保留，便于下次直接复用。
- 本地字幕已成功写入但远端删除失败时，字幕仍会保留，程序返回非零退出码。
- 默认的永久删除不可恢复，但只针对本次程序刚创建、且已成功下载字幕的 `transId`。
- 上传使用 OSS 单次 PUT，单文件上限 5 GiB；超过限制会在创建远端任务前停止。
- `.gitignore` 已忽略 `config.json`、`auth.json`、`sdk/` 等敏感与缓存文件，但不能阻止手动复制或误传，请把整个目录和 ZIP 都按账号凭证管理。

## 支持格式

视频：`mp4`、`ts`（自动极速转 MP4）、`wmv`、`m4v`、`flv`、`rmvb`、`dat`、`mov`、`mkv`、`webm`、`avi`、`mpeg`、`3gp`、`ogg`

音频：`mp3`、`wav`、`m4a`、`wma`、`aac`、`ogg`、`amr`、`flac`、`aiff`

网络要求：必须能访问听悟、阿里云登录和阿里云 OSS 地址；启用官方风控令牌生成时还需访问阿里云 CDN `g.alicdn.com` 与 `ynuf.aliapp.org`。

## 退出码与自动化

| 退出码 | 含义 |
|---:|---|
| `0` | 所有文件完成字幕下载、SRT 校验，以及默认情况下的远端删除复查 |
| `1` | 登录、接口、上传、转写、导出、下载、校验、删除或批处理中至少一个文件失败 |
| `2` | 命令行参数错误，或非交互环境中没有提供参数 |

自动化时以退出码为最终成功判据，不要仅根据控制台出现"下载""100%"字样判断成功：

```powershell
.\TingwuSubtitle.exe "D:\视频\课程.mp4"
if ($LASTEXITCODE -ne 0) {
    throw "听悟字幕处理失败，退出码：$LASTEXITCODE"
}
```

## 从源码重新编译 EXE

```powershell
# Nuitka 单文件版（首次编译会自动下载 C 编译器）
python -m pip install nuitka
python -m nuitka --onefile --assume-yes-for-downloads --windows-console-mode=force --output-filename=TingwuSubtitle.exe --include-package=py_mini_racer --include-package-data=py_mini_racer tingwu_subtitle.py
```

重新编译后，把新 EXE 复制回项目根目录，并保持 `ffmpeg.exe`、`FFmpeg-LICENSE.txt`、`FFmpeg-NOTICE.txt` 与 EXE 同目录。程序会优先使用这个根目录版本，不要求接收方安装 FFmpeg 或配置 PATH。
