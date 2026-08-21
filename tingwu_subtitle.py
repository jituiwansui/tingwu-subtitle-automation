#!/usr/bin/env python3
"""Upload media to Tongyi Tingwu, download SRT, then delete the remote task."""

from __future__ import annotations

import argparse
import getpass
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlencode, urlparse

import requests


BASE_URL = "https://tingwu.aliyun.com"
API_URL = BASE_URL + "/api"
if "__compiled__" in globals():
    executable_dir = Path(sys.argv[0]).resolve().parent
    PROGRAM_DIR = (
        executable_dir.parent
        if executable_dir.name.casefold() == "runtime"
        and Path(sys.argv[0]).stem.casefold() == "tingwusubtitlecore"
        else executable_dir
    )
else:
    PROGRAM_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = PROGRAM_DIR / "config.json"
DEFAULT_AUTH_FILE = PROGRAM_DIR / "auth.json"

VIDEO_EXTENSIONS = {
    ".mp4", ".wmv", ".m4v", ".flv", ".rmvb", ".dat", ".mov", ".mkv",
    ".webm", ".avi", ".mpeg", ".3gp", ".ogg",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".wma", ".aac", ".ogg", ".amr", ".flac", ".aiff",
}
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)


class TingwuError(RuntimeError):
    pass


class AuthenticationError(TingwuError):
    pass


class ApiError(TingwuError):
    pass


def load_auth_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("顶层必须是 JSON 对象")
        return state
    except Exception as exc:
        raise AuthenticationError(f"无法读取明文登录状态：{path} ({exc})") from exc


def save_auth_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("顶层必须是 JSON 对象")
        return config
    except Exception as exc:
        raise AuthenticationError(f"无法读取明文账号配置：{path} ({exc})") from exc


def cookie_to_dict(cookie: requests.cookies.Cookie) -> dict[str, Any]:
    return {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path,
        "secure": cookie.secure,
        "expires": cookie.expires,
    }


def restore_cookies(session: requests.Session, cookies: list[dict[str, Any]]) -> None:
    for item in cookies:
        session.cookies.set(
            item["name"],
            item["value"],
            domain=item.get("domain"),
            path=item.get("path", "/"),
            secure=bool(item.get("secure", False)),
            expires=item.get("expires"),
        )


def extract_json(html: str, variable: str) -> dict[str, Any]:
    match = re.search(rf"window\.{re.escape(variable)}\s*=\s*(\{{.*?\}});", html)
    if not match:
        raise AuthenticationError(f"阿里云登录页缺少 {variable}，登录协议可能已更新")
    return json.loads(match.group(1))


def rsa_pkcs1_v15_hex(message: str, modulus_hex: str, exponent_hex: str) -> str:
    raw = message.encode("utf-8")
    modulus = int(modulus_hex, 16)
    exponent = int(exponent_hex, 16)
    size = (modulus.bit_length() + 7) // 8
    padding_len = size - len(raw) - 3
    if padding_len < 8:
        raise AuthenticationError("密码长度超过阿里云 RSA 公钥限制")
    padding = bytearray()
    while len(padding) < padding_len:
        padding.extend(value for value in secrets.token_bytes(padding_len) if value)
    encoded = b"\x00\x02" + bytes(padding[:padding_len]) + b"\x00" + raw
    encrypted = pow(int.from_bytes(encoded, "big"), exponent, modulus)
    return encrypted.to_bytes(size, "big").hex()


def build_login_url() -> str:
    return_url = (
        "https://account.aliyun.com/login/login_aliyun?resType=html"
        "&boxEntrance=mini&loginScene=fastLogin"
        "&oauth_callback=https://tingwu.aliyun.com/home"
        "&log_channel=dialog&log_platform=pc"
        "&login_log_entrance=official&login_method=pwd_login"
        "&log_biz=tingwu"
        "&bizPassParams=%7B%22isDialogReg%22%3Atrue%2C%22action%22%3A%22login%22%2C%22tenantName%22%3A%22tingwu%22%7D"
    )
    return "https://passport.aliyun.com/havanaone/login/login.htm?" + urlencode(
        {
            "lang": "zh_CN",
            "appName": "gpt",
            "appEntrance": "tingwu",
            "styleType": "vertical",
            "bizParams": "",
            "notLoadSsoView": "true",
            "notKeepLogin": "false",
            "isMobile": "false",
            "returnUrl": return_url,
            "cssUrl": "",
            "regUrl": "https://account.aliyun.com/register/qr_register.htm?oauth_callback=https%3A%2F%2Ftingwu.aliyun.com%2Fhome",
            "bizPassParams": json.dumps(
                {"isDialogReg": True, "action": "login", "tenantName": "tingwu"},
                separators=(",", ":"),
            ),
            "redirectType": "iframeRedirect",
        }
    )


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def show_progress(percent: float, detail: str = "", *, finished: bool = False) -> None:
    percent = max(0.0, min(100.0, percent))
    width = 28
    completed = int(width * percent / 100)
    bar = "#" * completed + "-" * (width - completed)
    line = f"  总进度 [{bar}] {percent:6.2f}%  {detail}"
    print("\r" + line.ljust(105), end="\n" if finished else "", flush=True)


class PipelineProgress:
    def __init__(self) -> None:
        self.percent = 0.0

    def update(self, percent: float, detail: str, *, finished: bool = False) -> None:
        self.percent = max(self.percent, min(100.0, percent))
        show_progress(self.percent, detail, finished=finished)

    def segment(self, local_percent: float, start: float, end: float, detail: str) -> None:
        local_percent = max(0.0, min(100.0, local_percent))
        self.update(start + (end - start) * local_percent / 100, detail)


class ProgressReader:
    def __init__(self, file: BinaryIO, total: int, progress: PipelineProgress) -> None:
        self.file = file
        self.total = total
        self.progress = progress
        self.read_bytes = 0
        self.last_report = 0.0
        self.started = time.monotonic()
        self.finished_reported = False

    def __len__(self) -> int:
        return self.total

    def read(self, size: int = -1) -> bytes:
        chunk = self.file.read(size)
        self.read_bytes += len(chunk)
        now = time.monotonic()
        reached_end = self.read_bytes >= self.total
        if (now - self.last_report >= 0.2 or reached_end) and not (
            reached_end and self.finished_reported
        ):
            percent = 100 * self.read_bytes / self.total if self.total else 100
            elapsed = max(now - self.started, 0.001)
            speed = self.read_bytes / elapsed
            self.progress.segment(
                percent,
                8,
                30,
                f"正在上传 · {format_bytes(self.read_bytes)}/{format_bytes(self.total)} · {format_bytes(int(speed))}/s",
            )
            self.last_report = now
            self.finished_reported = reached_end
        return chunk


class TingwuClient:
    def __init__(self, auth_file: Path, request_timeout: int = 60) -> None:
        self.auth_file = auth_file
        self.request_timeout = request_timeout
        self.auth_state = load_auth_state(auth_file)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/home",
            }
        )
        restore_cookies(self.session, self.auth_state.get("cookies", []))

    def save(self) -> None:
        self.auth_state["cookies"] = [cookie_to_dict(c) for c in self.session.cookies]
        self.auth_state["updatedAt"] = int(time.time())
        save_auth_state(self.auth_file, self.auth_state)

    def user_info(self) -> dict[str, Any] | None:
        try:
            response = self.session.get(
                API_URL + "/account/v2/user/info?c=web", timeout=self.request_timeout
            )
            payload = response.json()
        except (requests.RequestException, ValueError):
            return None
        if response.ok and payload.get("code") == "0":
            return payload.get("data") or {}
        return None

    def ensure_login(self, username: str | None, password: str | None) -> dict[str, Any]:
        info = self.user_info()
        if info is not None:
            return info
        username = username or self.auth_state.get("username")
        if not username:
            raise AuthenticationError("登录状态已失效，且没有提供 --username")
        if password is None:
            password = os.environ.get("TINGWU_PASSWORD")
        if password is None and sys.stdin.isatty():
            password = getpass.getpass("阿里云登录密码: ")
        if not password:
            raise AuthenticationError(
                "登录状态已失效；请在 config.json 填写 password、设置 TINGWU_PASSWORD，或交互输入密码"
            )
        self.password_login(username, password)
        info = self.user_info()
        if info is None:
            raise AuthenticationError("账号密码登录完成，但听悟仍返回未登录")
        self.auth_state["username"] = username
        self.save()
        return info

    def password_login(self, username: str, password: str) -> None:
        risk = self.auth_state.get("risk") or {}
        bx_ua = os.environ.get("TINGWU_BX_UA") or risk.get("bxUa")
        bx_umid = os.environ.get("TINGWU_BX_UMIDTOKEN") or risk.get("bxUmidToken")
        if not bx_ua or not bx_umid:
            raise AuthenticationError(
                "缺少阿里云登录风控配置。请先把有效风控材料写入程序根目录 auth.json。"
            )
        login_url = build_login_url()
        page = self.session.get(login_url, timeout=self.request_timeout)
        page.raise_for_status()
        config = extract_json(page.text, "viewConfig")
        data = extract_json(page.text, "viewData")
        form = dict(data["loginFormData"])
        form.update(
            {
                "loginId": username,
                "password2": rsa_pkcs1_v15_hex(
                    password, config["rsaModulus"], config["rsaExponent"]
                ),
                "keepLogin": "true",
                "isIframe": "false",
                "banThirdPartyCookie": "false",
                "documentReferer": "",
                "ua": "",
                "umidGetStatusVal": "",
                "screenPixel": "1440x900",
                "navlanguage": "zh-CN",
                "navUserAgent": UA,
                "navPlatform": "Win32",
                "hitRSA2048Gray": "true",
                "umidToken": "",
                "umidTag": "NOT_INIT",
                "weiBoMpBridge": "",
                "ssoParams": "",
                "deviceId": "",
                "pageTraceId": secrets.token_hex(16),
                "bx-ua": bx_ua,
                "bx-umidtoken": bx_umid,
            }
        )
        response = self.session.post(
            "https://passport.aliyun.com/havanaone/loginLegacy/password/login.do?_bx-v=2.5.11",
            data=form,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://passport.aliyun.com",
                "Referer": login_url,
            },
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("content", {}).get("data", {})
        if result.get("loginResult") != "success":
            ret = payload.get("ret") or []
            message = result.get("message") or result.get("titleMsg") or "; ".join(ret)
            if "FAIL_SYS_USER_VALIDATE" in str(ret):
                message = "阿里云风控配置已失效，需要重新初始化登录配置"
            raise AuthenticationError(f"阿里云登录失败：{message or '未知错误'}")
        return_url = result.get("returnUrl")
        if not return_url:
            raise AuthenticationError("阿里云登录成功响应缺少 returnUrl")
        exchange = self.session.get(return_url, allow_redirects=True, timeout=self.request_timeout)
        exchange.raise_for_status()

    def api_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            API_URL + path,
            json=body,
            timeout=self.request_timeout,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError(f"接口返回非 JSON：{path} HTTP {response.status_code}") from exc
        if not response.ok or payload.get("code") != "0" or payload.get("success") is False:
            raise ApiError(
                f"接口失败 {path}: {payload.get('code')} {payload.get('message')} "
                f"(requestId={payload.get('requestId')})"
            )
        return payload

    def generate_upload(self, media: Path, lang: str, role_split_num: str) -> dict[str, Any]:
        extension = media.suffix.lower()
        mime_type = mimetypes.guess_type(media.name)[0] or "application/octet-stream"
        if extension == ".mp4":
            mime_type = "video/mp4"
        task_id = f"codex-upload-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        title = media.stem[:150]
        body = {
            "action": "generatePutLink",
            "version": "1.0",
            "taskId": task_id,
            "useSts": 0,
            "fileSize": media.stat().st_size,
            "dirId": 0,
            "fileContentType": mime_type,
            "tag": {
                "showName": title,
                "fileFormat": extension.lstrip("."),
                "fileType": "local",
                "lang": lang,
                "roleSplitNum": role_split_num,
                "translateSwitch": "0",
                "transTargetValue": "0",
                "originalTag": json.dumps(
                    {"isVideo": 1 if extension in VIDEO_EXTENSIONS else 0},
                    separators=(",", ":"),
                ),
                "client": "web",
            },
        }
        payload = self.api_post("/trans/request?generatePutLink&c=web", body)
        data = payload.get("data") or {}
        if not data.get("putLink") or not data.get("getLink") or not data.get("transId"):
            raise ApiError("generatePutLink 响应缺少上传地址或 transId")
        data["taskId"] = data.get("taskId") or task_id
        data["mimeType"] = mime_type
        return data

    def upload_file(
        self, media: Path, upload: dict[str, Any], progress: PipelineProgress
    ) -> None:
        size = media.stat().st_size
        if size > 5 * 1024**3:
            raise ApiError("当前版本使用 OSS 单次 PUT，暂不支持超过 5 GiB 的文件")
        headers = {"Content-Type": upload["mimeType"], "Content-Length": str(size)}
        with media.open("rb") as file:
            reader = ProgressReader(file, size, progress)
            response = requests.put(
                upload["putLink"],
                data=reader,
                headers=headers,
                timeout=(30, 7200),
            )
        if response.status_code != 200:
            raise ApiError(f"OSS 上传失败：HTTP {response.status_code}")

    def sync_upload(self, media: Path, upload: dict[str, Any]) -> None:
        duration = probe_duration(media)
        body: dict[str, Any] = {
            "action": "syncPutLink",
            "version": "1.0",
            "fileLink": upload["getLink"],
            "fileSize": media.stat().st_size,
            "transId": upload["transId"],
        }
        if duration is not None:
            body["duration"] = duration
        self.api_post("/trans/request?syncPutLink&c=web", body)

    def get_status(self, trans_id: str, task_id: str) -> dict[str, Any] | None:
        payload = self.api_post(
            "/trans/request?getTransStatus&c=web",
            {
                "action": "getTransStatus",
                "version": "1.0",
                "userId": "",
                "transIds": [trans_id],
                "taskIds": [task_id],
                "preview": 1,
            },
        )
        data = payload.get("data") or []
        return data[0] if data else None

    def wait_for_transcript(
        self,
        trans_id: str,
        task_id: str,
        timeout: int,
        interval: float,
        progress: PipelineProgress,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        highest = 1.0
        while time.monotonic() < deadline:
            item = self.get_status(trans_id, task_id)
            if item:
                status = item.get("status")
                if status == 0:
                    progress.update(75, "转写完成")
                    return item
                if status not in {1, 2, 3, 4, 11}:
                    raise ApiError(
                        f"转写失败：status={status}, statusMsg={item.get('statusMsg')}"
                    )
                raw_progress = item.get("progress")
                try:
                    reported = float(raw_progress)
                    if 0 <= reported <= 1:
                        reported *= 100
                    elif reported > 100:
                        reported /= 100
                except (TypeError, ValueError):
                    reported = 0.0
                elapsed = time.monotonic() - started
                estimated = min(94.0, 3.0 + elapsed / max(timeout, 1) * 91.0)
                try:
                    server_time = float(item.get("serverCurrentTime"))
                    trans_start = float(item.get("transStartTime"))
                    forecast_done = float(item.get("forecastTransDoneTime"))
                    forecast = (server_time - trans_start) * 100 / (forecast_done - trans_start)
                    forecast = max(0.0, min(95.0, forecast))
                except (TypeError, ValueError, ZeroDivisionError):
                    forecast = 0.0
                highest = max(highest, min(reported, 99.0), forecast, estimated)
                progress.segment(
                    highest,
                    35,
                    75,
                    f"正在转写 · status={status} · 已等待 {elapsed:.0f} 秒",
                )
            time.sleep(interval)
        raise ApiError(f"等待转写超时（{timeout} 秒），远端任务保留，transId={trans_id}")

    def create_srt_export(
        self, trans_id: str, user_id: int, with_speaker: bool, with_timestamp: bool
    ) -> str:
        payload = self.api_post(
            "/export/request?c=web",
            {
                "action": "exportTrans",
                "transIds": [trans_id],
                "userId": user_id,
                "exportDetails": [
                    {
                        "docType": 1,
                        "fileType": 2,
                        "withSpeaker": with_speaker,
                        "withTimeStamp": with_timestamp,
                    }
                ],
            },
        )
        task_id = (payload.get("data") or {}).get("exportTaskId")
        if not task_id:
            raise ApiError("exportTrans 响应缺少 exportTaskId")
        return task_id

    def wait_for_export(
        self,
        export_task_id: str,
        trans_id: str,
        timeout: int,
        progress: PipelineProgress,
    ) -> str:
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        while time.monotonic() < deadline:
            payload = self.api_post(
                "/export/request?c=web",
                {"action": "getExportStatus", "exportTaskId": export_task_id},
            )
            data = payload.get("data") or {}
            status = data.get("exportStatus")
            if status == 1:
                for item in data.get("exportUrls") or []:
                    if item.get("success") and item.get("transIdStr") == trans_id and item.get("url"):
                        progress.update(85, "字幕导出完成")
                        return item["url"]
                raise ApiError("SRT 导出完成，但响应中没有匹配的下载地址")
            if status not in {0, None}:
                raise ApiError(f"SRT 导出失败：exportStatus={status}")
            elapsed = time.monotonic() - started
            percent = min(95.0, 10.0 + elapsed / max(timeout, 1) * 85.0)
            progress.segment(percent, 75, 85, f"正在导出字幕 · 已等待 {elapsed:.0f} 秒")
            time.sleep(1)
        raise ApiError(f"等待 SRT 导出超时（{timeout} 秒）")

    def download_srt(
        self, url: str, destination: Path, progress: PipelineProgress
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with requests.get(url, stream=True, timeout=(30, 300)) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                started = time.monotonic()
                with temporary.open("wb") as file:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            file.write(chunk)
                            downloaded += len(chunk)
                            percent = downloaded * 100 / total if total else 95.0
                            progress.segment(
                                percent,
                                85,
                                94,
                                "正在下载字幕 · " + f"{format_bytes(downloaded)}" + (
                                    f"/{format_bytes(total)}" if total else ""
                                ),
                            )
            progress.update(95, "正在校验 SRT")
            validate_srt(temporary)
            os.replace(temporary, destination)
            elapsed = max(time.monotonic() - started, 0.001)
            progress.update(
                96,
                f"字幕已下载并校验 · {format_bytes(downloaded)} · {elapsed:.1f} 秒",
            )
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def delete_permanently(self, trans_id: str) -> None:
        self.api_post(
            "/trans/request?delTrans&c=web",
            {
                "action": "delTrans",
                "version": "1.0",
                "userId": "",
                "transIds": [trans_id],
                "deletePermanently": True,
            },
        )

    def verify_deleted(self, trans_id: str, title: str) -> bool:
        payload = self.api_post(
            "/trans/request?getTransList&c=web",
            {
                "action": "getTransList",
                "version": "1.0",
                "userId": "",
                "filter": {
                    "status": [0, 1, 2, 3],
                    "fileTypes": [],
                    "beginTime": "",
                    "mediaType": "",
                    "endTime": "",
                    "showName": title,
                    "read": "",
                    "lang": "",
                    "shareUserId": "",
                    "client": "",
                },
                "preview": 1,
                "pageNo": 1,
                "pageSize": 100,
            },
        )
        return all(item.get("transId") != trans_id for item in payload.get("data") or [])


def probe_duration(media: Path) -> int | None:
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(media),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return max(1, int(float(result.stdout.strip())))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def validate_srt(path: Path) -> None:
    raw = path.read_bytes()
    if not raw:
        raise ApiError("下载得到的 SRT 是空文件")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ApiError("下载文件不是 UTF-8 SRT") from exc
    timestamp = re.compile(
        r"(?m)^\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}\s*$"
    )
    if not re.search(r"(?m)^\d+\s*$", text) or not timestamp.search(text):
        raise ApiError("下载文件未通过 SRT 结构校验")


def safe_output_name(media: Path) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", media.stem).strip(" .")
    if not name:
        name = "subtitle"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if name.upper() in reserved:
        name = "_" + name
    return name + ".srt"


def process_one(
    client: TingwuClient,
    media: Path,
    output_dir: Path | None,
    args: argparse.Namespace,
) -> Path:
    media = media.resolve()
    if not media.is_file():
        raise TingwuError(f"输入文件不存在：{media}")
    if media.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise TingwuError(f"不支持的文件格式：{media.suffix}")
    if media.stat().st_size > 5 * 1024**3:
        raise TingwuError("当前版本使用 OSS 单次 PUT，暂不支持超过 5 GiB 的文件")
    destination_dir = output_dir.resolve() if output_dir is not None else media.parent
    destination = destination_dir / safe_output_name(media)
    progress = PipelineProgress()
    print(f"\n处理：{media}")
    print(f"  字幕：{destination}")
    progress.update(0, "检查输入文件")
    trans_id: str | None = None
    try:
        upload = client.generate_upload(media, args.lang, args.role_split_num)
        trans_id = upload["transId"]
        progress.update(8, "远端任务已创建")
        client.upload_file(media, upload, progress)
        progress.update(32, "正在提交上传结果")
        client.sync_upload(media, upload)
        progress.update(35, "等待转写")
        item = client.wait_for_transcript(
            trans_id,
            upload["taskId"],
            args.transcribe_timeout,
            args.poll_interval,
            progress,
        )
        user_id = int(item["userId"])
        progress.update(76, "创建 SRT 导出任务")
        export_task_id = client.create_srt_export(
            trans_id, user_id, not args.no_speaker, not args.no_timestamp
        )
        download_url = client.wait_for_export(
            export_task_id, trans_id, args.export_timeout, progress
        )
        client.download_srt(download_url, destination, progress)
    except Exception:
        progress.update(progress.percent, "处理失败，远端任务已保留", finished=True)
        if trans_id:
            print(f"  处理失败，未删除远端任务：{trans_id}", file=sys.stderr)
        else:
            print("  处理失败，远端任务尚未创建", file=sys.stderr)
        raise
    if args.keep_remote:
        progress.update(100, "处理完成 · 已按要求保留远端任务", finished=True)
        print(f"  SRT：{destination} ({destination.stat().st_size} bytes)")
        print(f"  已按 --keep-remote 保留远端任务：{trans_id}")
        client.save()
        return destination
    try:
        progress.update(97, "正在永久删除远端记录")
        client.delete_permanently(trans_id)
        progress.update(99, "正在复查远端删除结果")
        if not client.verify_deleted(trans_id, media.stem[:150]):
            raise ApiError(f"删除接口返回成功，但列表仍能查到远端任务：{trans_id}")
    except Exception:
        progress.update(progress.percent, "字幕已保存，但远端删除失败", finished=True)
        print(f"  SRT 已保留：{destination}", file=sys.stderr)
        raise
    progress.update(100, "处理完成 · 远端记录已删除", finished=True)
    print(f"  SRT：{destination} ({destination.stat().st_size} bytes)")
    print(f"  远端记录已永久删除并复查确认：{trans_id}")
    client.save()
    return destination


def password_from_args(args: argparse.Namespace, config: dict[str, Any]) -> str | None:
    if getattr(args, "password_env", None):
        value = os.environ.get(args.password_env)
        if value:
            return value
    return os.environ.get("TINGWU_PASSWORD") or config.get("password")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通义听悟：上传音视频，自动下载 SRT，成功后永久删除远端记录"
    )
    parser.add_argument(
        "--config-file", type=Path, default=DEFAULT_CONFIG_FILE,
        help=f"明文账号密码配置（默认：{DEFAULT_CONFIG_FILE}）",
    )
    parser.add_argument(
        "--auth-file", type=Path, default=DEFAULT_AUTH_FILE,
        help=f"明文 Cookie/风控登录文件（默认：{DEFAULT_AUTH_FILE}）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    process = subparsers.add_parser("process", help="处理一个或多个音视频")
    process.add_argument("files", nargs="+", type=Path)
    process.add_argument(
        "-o", "--output-dir", type=Path,
        help="字幕输出目录；不指定时输出到每个视频文件所在目录",
    )
    process.add_argument("--username", help="阿里云账号；未提供时读取 config.json")
    process.add_argument("--password-env", default="TINGWU_PASSWORD", help="密码环境变量名")
    process.add_argument("--lang", choices=["cn", "en", "ja", "yue"], default="cn")
    process.add_argument("--role-split-num", choices=["-1", "1", "2", "0"], default="-1")
    process.add_argument("--transcribe-timeout", type=int, default=7200)
    process.add_argument("--export-timeout", type=int, default=300)
    process.add_argument("--poll-interval", type=float, default=3.0)
    process.add_argument("--no-speaker", action="store_true")
    process.add_argument("--no-timestamp", action="store_true")
    process.add_argument("--keep-remote", action="store_true", help="调试用：下载后不删除远端记录")

    status = subparsers.add_parser("auth-status", help="检查根目录明文会话是否仍有效")
    status.set_defaults(command="auth-status")

    login = subparsers.add_parser("auth-login", help="使用 config.json 的账号密码刷新会话")
    login.add_argument("--username")
    login.add_argument("--password-env", default="TINGWU_PASSWORD")

    bootstrap = subparsers.add_parser(
        "auth-bootstrap", help=argparse.SUPPRESS
    )
    bootstrap.add_argument("--username")
    bootstrap.add_argument("--password-env", default="TINGWU_PASSWORD")
    bootstrap.add_argument("--bx-ua-env", default="TINGWU_BX_UA")
    bootstrap.add_argument("--bx-umid-env", default="TINGWU_BX_UMIDTOKEN")
    return parser


def run_command(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config_file)
        client = TingwuClient(args.auth_file)
        username = getattr(args, "username", None) or config.get("username")
        password = password_from_args(args, config)
        if args.command == "auth-status":
            info = client.user_info()
            if info is None:
                print(f"登录状态：无效\n登录材料：{args.auth_file}")
                return 1
            print(f"登录状态：有效\n账号：{info.get('aliyunUserName', '未知')}\n登录材料：{args.auth_file}")
            return 0

        if args.command == "auth-bootstrap":
            bx_ua = os.environ.get(args.bx_ua_env)
            bx_umid = os.environ.get(args.bx_umid_env)
            if not bx_ua or not bx_umid:
                raise AuthenticationError("auth-bootstrap 缺少风控环境变量")
            if not username:
                raise AuthenticationError("config.json 缺少 username")
            client.auth_state["username"] = username
            client.auth_state["risk"] = {"bxUa": bx_ua, "bxUmidToken": bx_umid}
            client.password_login(username, password or getpass.getpass("阿里云登录密码: "))
            if client.user_info() is None:
                raise AuthenticationError("初始化登录失败")
            client.save()
            print(f"登录材料已明文保存：{args.auth_file}")
            return 0

        if args.command == "auth-login":
            info = client.ensure_login(username, password)
            client.save()
            print(f"登录成功：{info.get('aliyunUserName', '未知')}")
            return 0

        info = client.ensure_login(username, password)
        print(f"登录成功：{info.get('aliyunUserName', '未知')}")
        failures = 0
        for media in args.files:
            try:
                process_one(client, media, args.output_dir, args)
            except Exception as exc:
                failures += 1
                print(f"错误：{exc}", file=sys.stderr)
        return 1 if failures else 0
    except (TingwuError, requests.RequestException, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


def parse_dropped_paths(raw: str) -> list[str]:
    try:
        tokens = shlex.split(raw.strip(), posix=False)
    except ValueError as exc:
        raise TingwuError(f"无法解析拖入的文件路径：{exc}") from exc
    paths: list[str] = []
    for token in tokens:
        token = token.strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
            token = token[1:-1]
        if token:
            paths.append(token)
    return paths


def collect_files_interactively() -> list[str]:
    print("通义听悟字幕自动化工具")
    print("请把一个或多个音视频文件拖入此窗口，然后按 Enter 开始。")
    print("多个文件可一次全部拖入；字幕默认保存到各自视频所在目录。")
    while True:
        try:
            raw = input("\n拖放文件到这里 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return []
        if not raw:
            print("没有检测到文件路径，请重新拖入；直接关闭窗口可取消。")
            continue
        try:
            paths = parse_dropped_paths(raw)
        except TingwuError as exc:
            print(f"错误：{exc}")
            continue
        if not paths:
            print("没有检测到文件路径，请重新拖入。")
            continue
        missing = [path for path in paths if not Path(path).is_file()]
        if missing:
            print(f"以下路径不是文件：{missing[0]}")
            continue
        return paths


def normalize_argv(argv: list[str]) -> tuple[list[str], bool]:
    if not argv:
        if not sys.stdin.isatty():
            return argv, False
        files = collect_files_interactively()
        return (["process", *files] if files else []), True
    commands = {"process", "auth-status", "auth-login", "auth-bootstrap"}
    if argv[0] not in commands and not argv[0].startswith("-"):
        return ["process", *argv], False
    return argv, False


def main(argv: list[str] | None = None) -> int:
    normalized, interactive = normalize_argv(list(sys.argv[1:] if argv is None else argv))
    if not normalized:
        if not interactive:
            build_parser().print_help()
            return 2
        return 0
    try:
        args = build_parser().parse_args(normalized)
        return run_command(args)
    finally:
        if interactive:
            try:
                input("\n处理结束，按 Enter 关闭窗口……")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
