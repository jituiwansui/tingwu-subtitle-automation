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

try:
    from py_mini_racer import MiniRacer
except ImportError:
    MiniRacer = None


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
BX_SDK_DIR = PROGRAM_DIR / "sdk"

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


def harden_private_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise AuthenticationError(f"无法收紧敏感文件权限：{path} ({exc})") from exc
    if os.name != "nt":
        return
    try:
        identity = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
        match = re.search(r"\bS-\d-(?:\d+-)+\d+\b", identity.stdout)
        if not match:
            raise OSError("无法解析当前 Windows 用户 SID")
        acl = subprocess.run(
            [
                "icacls.exe",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{match.group(0)}:(F)",
                "*S-1-5-18:(F)",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
        if acl.returncode != 0:
            raise OSError(f"icacls 退出码 {acl.returncode}")
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthenticationError(f"无法收紧敏感文件权限：{path} ({exc})") from exc


def save_auth_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    for attempt in range(3):
        try:
            os.replace(temporary, path)
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.3)
    harden_private_file_permissions(path)


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


AWSC_CDN = "https://g.alicdn.com/"


def ensure_sdk_files(refresh: bool = False) -> None:
    """下载阿里云官方风控 SDK 到本地缓存（fireyejs/et 版本从 awsc.js 动态解析）。

    refresh=True 时强制重新下载入口文件，用于官方 SDK 更新后的自愈。
    """
    BX_SDK_DIR.mkdir(parents=True, exist_ok=True)

    def fetch(path: str, dest: Path) -> None:
        response = requests.get(AWSC_CDN + path, headers={"User-Agent": UA}, timeout=30)
        response.raise_for_status()
        dest.write_bytes(response.content)

    awsc_file = BX_SDK_DIR / "awsc.js"
    if refresh or not awsc_file.exists():
        fetch("AWSC/AWSC/awsc.js", awsc_file)
    baxia_file = BX_SDK_DIR / "baxiaCommon.js"
    if refresh or not baxia_file.exists():
        fetch("sd/baxia/2.5.4/baxiaCommon.js", baxia_file)
    awsc_text = awsc_file.read_text(encoding="utf-8", errors="replace")
    for pattern, name in (
        (r"AWSC/fireyejs/[\d.]+/fireyejs\.js", "fireyejs.js"),
        (r"AWSC/et/[\d.]+/et_f\.js", "et_f.js"),
    ):
        match = re.search(pattern, awsc_text)
        if not match:
            raise AuthenticationError(f"awsc.js 中未找到 {name} 的版本路径，SDK 结构可能已更新")
        versioned = match.group(0)
        marker = BX_SDK_DIR / (name + ".version")
        dest = BX_SDK_DIR / name
        current = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        if not dest.exists() or current != versioned:
            fetch(versioned, dest)
            marker.write_text(versioned, encoding="utf-8")


def get_baxia_version() -> str:
    """从缓存的 baxiaCommon.js 中解析风控协议版本（登录接口 _bx-v 参数）。"""
    try:
        text = (BX_SDK_DIR / "baxiaCommon.js").read_text(encoding="utf-8", errors="replace")
        match = re.search(r'version="([\d.]+)"', text)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "2.5.11"


def generate_bx_tokens_local(page_url: str) -> tuple[str, str] | None:
    """用内嵌 V8（py_mini_racer）运行阿里云官方风控 SDK，本地生成 bx-ua / bx-umidtoken。

    为兼容官方 SDK 更新：第一次失败后会强制刷新 SDK 缓存再试一次；
    任何一步不可用都返回 None，由调用方走兜底。
    """
    if MiniRacer is None:
        return None
    for attempt in range(2):
        try:
            ensure_sdk_files(refresh=attempt > 0)
        except (AuthenticationError, requests.RequestException, OSError):
            return None
        tokens = _run_awsc_generation(page_url)
        if tokens:
            return tokens
    return None


def _run_awsc_generation(page_url: str) -> tuple[str, str] | None:
    """执行一次官方 SDK 生成：浏览器环境垫片（BX_ENV_JS，纯 JS）+ 官方 SDK
    （awsc.js/fireyejs.js）在 V8 中执行；SDK 对外的网络请求（umid 令牌接口
    等）由 Python 代理转发回注。"""
    try:
        ctx = MiniRacer()
        ctx.eval(BX_ENV_JS)
        ctx.eval(
            f"__setOptions({json.dumps(page_url)}, {json.dumps(UA)}, "
            "\"https://tingwu.aliyun.com/home\")"
        )
        ctx.eval("__setRandomPool(" + json.dumps(list(secrets.token_bytes(4096))) + ")")
        sources = {
            "AWSC/AWSC/awsc.js": (BX_SDK_DIR / "awsc.js").read_text(encoding="utf-8"),
            "fireyejs": (BX_SDK_DIR / "fireyejs.js").read_text(encoding="utf-8"),
            "AWSC/et/": (BX_SDK_DIR / "et_f.js").read_text(encoding="utf-8"),
        }
        ctx.eval("__scriptSources = " + json.dumps(sources))
        ctx.eval(
            "(function(){var el=document.createElement(\"script\");"
            "el.src=\"https://g.alicdn.com/AWSC/AWSC/awsc.js\";"
            "document.head.appendChild(el);})()"
        )
        if ctx.eval("typeof AWSC") != "object":
            return None
        fy_options = json.dumps(
            {"noProxy": True, "location": "cn", "MaxMTLog": 20,
             "MaxNGPLog": 10, "MaxKSLog": 5, "MaxFocusLog": 3},
            separators=(",", ":"),
        )
        ctx.eval("__startFy(" + json.dumps(fy_options) + ")")
        net = requests.Session()
        net.headers.update({"User-Agent": UA, "Referer": page_url})
        fetched: set[str] = set()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ctx.eval("__pump()")
            for req in json.loads(ctx.eval("__fetchExternal()")):
                if req["url"] in fetched:
                    continue
                fetched.add(req["url"])
                try:
                    resp = net.request(
                        req.get("method") or "GET", req["url"],
                        data=req.get("body"), timeout=15,
                    )
                    ctx.eval(
                        "__provideExternal(%d, %d, %s, %s)"
                        % (
                            req["id"], resp.status_code,
                            json.dumps(json.dumps(dict(resp.headers))),
                            json.dumps(resp.text),
                        )
                    )
                except requests.RequestException:
                    ctx.eval("__provideExternal(%d, 0, \"{}\", \"\")" % req["id"])
            state = json.loads(ctx.eval("__fyState()"))
            bx_ua = state.get("bxUa")
            bx_umid = state.get("bxUmidToken")
            if (
                bx_ua and bx_umid
                and not str(bx_ua).startswith("default")
                and not str(bx_umid).startswith("default")
            ):
                return str(bx_ua), str(bx_umid)
            time.sleep(0.2)
    except Exception:
        if os.environ.get("TINGWU_DEBUG_BX"):
            import traceback
            traceback.print_exc()
        return None
    return None


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

    def _password_login_once(
        self, username: str, password: str, login_url: str, bx_ua: str, bx_umid: str
    ) -> None:
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
                "umidToken": "" if bx_umid == "not_loaded" else bx_umid,
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
            "https://passport.aliyun.com/havanaone/loginLegacy/password/login.do"
            f"?_bx-v={get_baxia_version()}",
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
                message = "阿里云触发了登录风控校验"
            raise AuthenticationError(f"阿里云登录失败：{message or '未知错误'}")
        return_url = result.get("returnUrl")
        if not return_url:
            raise AuthenticationError("阿里云登录成功响应缺少 returnUrl")
        exchange = self.session.get(return_url, allow_redirects=True, timeout=self.request_timeout)
        exchange.raise_for_status()

    def password_login(self, username: str, password: str) -> None:
        # bx-ua / bx-umidtoken 令牌策略：
        # 1. 环境变量（TINGWU_BX_UA/TINGWU_BX_UMIDTOKEN）或 auth.json 中手工注入
        #    的令牌优先，直接使用；
        # 2. 否则先用官方 SDK 的降级值 "not_loaded" 直接登录（最快，服务端接受）；
        # 3. 登录被拒绝时，再用内嵌 V8 运行官方风控 SDK 生成真实令牌重试；
        #    成功后记住 loginMode=official，之后直接走官方生成。
        login_url = build_login_url()
        risk = self.auth_state.get("risk") or {}
        bx_ua = os.environ.get("TINGWU_BX_UA")
        bx_umid = os.environ.get("TINGWU_BX_UMIDTOKEN")
        if not (bx_ua and bx_umid) and risk.get("bxUa") and risk.get("bxUmidToken") and not risk.get("generated"):
            bx_ua, bx_umid = risk["bxUa"], risk["bxUmidToken"]
        if bx_ua and bx_umid:
            self._password_login_once(username, password, login_url, bx_ua, bx_umid)
            return

        if self.auth_state.get("loginMode") == "official":
            # 此前 not_loaded 已被风控拒绝过，直接走官方生成
            generated = generate_bx_tokens_local(login_url)
            if generated:
                try:
                    self._password_login_once(username, password, login_url, *generated)
                    return
                except AuthenticationError:
                    pass  # 官方令牌也可能因 SDK 更新失效，退回降级值再试
            self._password_login_once(username, password, login_url, "not_loaded", "not_loaded")
            self.auth_state.pop("loginMode", None)
            return

        try:
            self._password_login_once(username, password, login_url, "not_loaded", "not_loaded")
            print("风控令牌：使用官方降级值 not_loaded 登录成功")
            return
        except AuthenticationError as first_error:
            print("风控令牌：not_loaded 登录被拒绝，尝试官方 SDK 本地生成…")
            generated = generate_bx_tokens_local(login_url)
            if not generated:
                raise first_error
            try:
                self._password_login_once(username, password, login_url, *generated)
            except AuthenticationError as second_error:
                raise AuthenticationError(
                    "阿里云登录失败（降级值与官方生成令牌均未通过）；"
                    f"降级值错误：{first_error}；官方令牌错误：{second_error}"
                ) from second_error
            self.auth_state["loginMode"] = "official"
            print("风控令牌：已通过官方 SDK 生成令牌登录成功，后续登录将直接使用该方式")

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




# 纯 JS 浏览器环境垫片：让阿里云官方风控 SDK 在裸 V8（py_mini_racer）中运行。
# 等价于登录页里 baxiaCommon 所做的模块加载与初始化，不启动、不控制浏览器。
BX_ENV_JS = r"""
// bx_browser_env.js — 纯 JavaScript 的浏览器环境垫片（不依赖 Node/DOM 库）。
// 目标：让阿里云官方风控 SDK（awsc.js + fireyejs.js）在裸 V8/JS 引擎里运行，
// 产出官方格式的 bx-ua / bx-umidtoken。
//
// 宿主（Python 或 Node）只需要：
//   1. 先执行本文件；
//   2. 通过 __setOptions(pageUrl, userAgent, referrer) 设置页面信息；
//   3. 通过 __scriptSources 提供 CDN 脚本内容（URL 片段 -> 源码）；
//   4. 执行 awsc.js 源码，然后调用 __startFy(optionsJson)；
//   5. 反复调用 __pump() 驱动定时器/脚本队列，用 __fetchExternal() 取回
//      SDK 发起的跨域脚本请求（如 umid 服务），由宿主实际下载后调用
//      __provideExternal(url, body) 回注；
//   6. 读取 __fyState() 获取 {"bxUa":..., "bxUmidToken":...}。
"use strict";

(function () {
  var G = typeof globalThis !== "undefined" ? globalThis : this;

  // ---------- 随机数（宿主注入真随机池，JS 侧消费） ----------
  var __randomPool = [];
  var __randomIdx = 0;
  function __nextRandomByte() {
    if (__randomIdx < __randomPool.length) return __randomPool[__randomIdx++] & 255;
    // 池子耗尽后用 xorshift 续命（只影响非关键指纹抖动）
    __seed ^= __seed << 13; __seed ^= __seed >>> 17; __seed ^= __seed << 5;
    return __seed & 255;
  }
  var __seed = 0x2f6e2b1;

  // ---------- 定时器 ----------
  var __timers = [];
  var __timerSeq = 1;
  var __fakeNow = 1700000000000;
  function setTimeoutShim(cb, delay) {
    var id = __timerSeq++;
    __timers.push({ id: id, cb: cb, due: __fakeNow + (delay || 0), interval: 0, cleared: false });
    return id;
  }
  function setIntervalShim(cb, delay) {
    var id = __timerSeq++;
    __timers.push({ id: id, cb: cb, due: __fakeNow + (delay || 0), interval: delay || 10, cleared: false });
    return id;
  }
  function clearTimerShim(id) {
    for (var i = 0; i < __timers.length; i++) if (__timers[i].id === id) __timers[i].cleared = true;
  }
  function __pump(maxIter) {
    var n = 0;
    maxIter = maxIter || 100;
    while (n < maxIter) {
      var best = null;
      for (var i = 0; i < __timers.length; i++) {
        var t = __timers[i];
        if (!t.cleared && (!best || t.due < best.due)) best = t;
      }
      if (!best) break;
      if (best.interval) { best.due = __fakeNow + best.interval; } else { best.cleared = true; }
      __fakeNow = Math.max(__fakeNow, best.due);
      try { best.cb(); } catch (e) { __log("timer cb error: " + e); }
      n++;
    }
    return __timers.some(function (t) { return !t.cleared; }) ? 1 : 0;
  }

  function __log(msg) { if (G.__hostLog) G.__hostLog(String(msg)); }

  // ---------- 存储 ----------
  function makeStorage() {
    var data = {};
    return {
      getItem: function (k) { k = String(k); return k in data ? data[k] : null; },
      setItem: function (k, v) { data[String(k)] = String(v); },
      removeItem: function (k) { delete data[String(k)]; },
      clear: function () { data = {}; },
      key: function (i) { return Object.keys(data)[i] || null; },
      get length() { return Object.keys(data).length; },
    };
  }

  // ---------- DOM 元素 ----------
  var __elementsById = {};
  var __scriptElements = [];

  function makeClassList() {
    var set = {};
    return {
      add: function () { for (var i = 0; i < arguments.length; i++) set[arguments[i]] = 1; },
      remove: function () { for (var i = 0; i < arguments.length; i++) delete set[arguments[i]]; },
      contains: function (x) { return !!set[x]; },
      toggle: function (x) { if (set[x]) { delete set[x]; return false; } set[x] = 1; return true; },
      toString: function () { return Object.keys(set).join(" "); },
    };
  }

  function makeCtx2D(canvas) {
    // 假 canvas 2D 上下文：所有方法返回固定但合法的数据
    return {
      canvas: canvas,
      fillStyle: "", strokeStyle: "", font: "", textBaseline: "", globalCompositeOperation: "",
      shadowBlur: 0, shadowColor: "", lineWidth: 1, lineCap: "", lineJoin: "",
      fillRect: function () {}, strokeRect: function () {}, clearRect: function () {},
      fillText: function () {}, strokeText: function () {},
      measureText: function () { return { width: 42.5, actualBoundingBoxAscent: 10, actualBoundingBoxDescent: 2 }; },
      getImageData: function (x, y, w, h) {
        var d = new Uint8Array(Math.max(4, (w | 0) * (h | 0) * 4));
        for (var i = 0; i < d.length; i++) d[i] = (i * 7 + 13) & 255;
        return { data: d, width: w, height: h };
      },
      putImageData: function () {}, drawImage: function () {},
      createLinearGradient: function () { return { addColorStop: function () {} }; },
      createRadialGradient: function () { return { addColorStop: function () {} }; },
      createPattern: function () { return {}; },
      beginPath: function () {}, closePath: function () {}, moveTo: function () {}, lineTo: function () {},
      arc: function () {}, arcTo: function () {}, bezierCurveTo: function () {}, quadraticCurveTo: function () {},
      rect: function () {}, fill: function () {}, stroke: function () {}, clip: function () {},
      save: function () {}, restore: function () {}, translate: function () {}, rotate: function () {},
      scale: function () {}, transform: function () {}, setTransform: function () {},
      isPointInPath: function () { return false; },
    };
  }

  function makeElement(tag) {
    tag = String(tag).toLowerCase();
    var el = {
      tagName: tag.toUpperCase(),
      nodeType: 1,
      style: {},
      classList: makeClassList(),
      children: [], childNodes: [], attributes: {},
      innerHTML: "", innerText: "", textContent: "", value: "",
      parentNode: null, firstChild: null, nextSibling: null, offsetWidth: 100, offsetHeight: 20,
      clientWidth: 100, clientHeight: 20, scrollWidth: 100, scrollHeight: 20,
      addEventListener: function (type, cb) { (el.__listeners = el.__listeners || {})[type] = cb; },
      removeEventListener: function () {},
      dispatchEvent: function (ev) {
        var l = el.__listeners && el.__listeners[ev.type];
        if (l) { try { l(ev); } catch (e) {} }
        return true;
      },
      appendChild: function (c) {
        el.children.push(c); el.childNodes.push(c);
        if (c && typeof c === "object") c.parentNode = el;
        if (!el.firstChild) el.firstChild = c;
        if (c && c.tagName === "SCRIPT" && c.src) __loadScriptElement(c);
        return c;
      },
      insertBefore: function (c) { return el.appendChild(c); },
      removeChild: function (c) { return c; },
      replaceChild: function (n) { return n; },
      remove: function () {},
      setAttribute: function (k, v) { el.attributes[k] = String(v); if (k === "src") el.src = String(v); if (k === "id") { el.id = String(v); __elementsById[el.id] = el; } },
      getAttribute: function (k) { return k in el.attributes ? el.attributes[k] : (k === "src" && el.src ? el.src : null); },
      hasAttribute: function (k) { return k in el.attributes || (k === "src" && !!el.src); },
      getElementsByTagName: function () { return []; },
      querySelector: function () { return null; },
      querySelectorAll: function () { return []; },
      getBoundingClientRect: function () { return { x: 0, y: 0, top: 0, left: 0, right: 100, bottom: 20, width: 100, height: 20 }; },
      focus: function () {}, blur: function () {}, click: function () {}, select: function () {},
      cloneNode: function () { return makeElement(tag); },
      contains: function () { return false; },
      attachEvent: function () {}, detachEvent: function () {},
    };
    Object.defineProperty(el, "id", {
      get: function () { return el.attributes.id || ""; },
      set: function (v) { el.attributes.id = String(v); __elementsById[String(v)] = el; },
      configurable: true,
    });
    if (tag === "canvas") {
      el.width = 300; el.height = 150;
      el.getContext = function (type) {
        if (type === "2d") return makeCtx2D(el);
        return null; // webgl 等按不支持处理
      };
      el.toDataURL = function () {
        return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
      };
    }
    if (tag === "iframe") {
      el.contentWindow = null;
      el.contentDocument = null;
      Object.defineProperty(el, "src", {
        get: function () { return el.attributes.src || ""; },
        set: function (v) {
          el.attributes.src = String(v);
          __log("iframe src: " + String(v).slice(0, 300));
        },
        configurable: true,
      });
    }
    if (tag === "link") { el.sheet = { cssRules: [] }; }
    if (tag === "video" || tag === "audio") {
      // 模拟 Chrome 的 canPlayType 应答
      el.canPlayType = function (t) {
        t = String(t).toLowerCase();
        if (/codecs=/.test(t)) {
          if (/(mp4|avc1|h264|vp8|vp9|av01|mp4a|aac|opus|hvc1|hev1)/.test(t)) return "probably";
          if (/(theora|vorbis)/.test(t)) return "maybe";
          return "";
        }
        if (/(video\/(mp4|webm|ogg)|audio\/(mpeg|mp4|ogg|wav|webm|aac|flac))/.test(t)) return "maybe";
        return "";
      };
      el.play = function () { return Promise.resolve(); };
      el.pause = function () {};
    }
    if (tag === "style") {
      Object.defineProperty(el, "textContent", {
        get: function () { return el.__text || ""; },
        set: function (v) { el.__text = String(v); },
        configurable: true,
      });
      el.sheet = { cssRules: [], insertRule: function () {}, deleteRule: function () {} };
    }
    if (tag === "script") {
      __scriptElements.push(el);
    }
    return el;
  }

  // ---------- 脚本加载 ----------
  // 宿主提供的脚本源表：URL 片段 -> 源码字符串
  G.__scriptSources = G.__scriptSources || {};

  function __findSource(src) {
    for (var frag in G.__scriptSources) {
      if (src.indexOf(frag) > -1) return G.__scriptSources[frag];
    }
    return null;
  }

  function __loadScriptElement(el) {
    var src = el.src || "";
    var code = __findSource(src);
    if (code != null) {
      __currentScript = el;
      try {
        (0, eval)(code + "\n//# sourceURL=" + src);
      } catch (e) {
        __log("script exec error " + src.slice(0, 80) + ": " + (e && e.message));
      }
      __currentScript = null;
      if (typeof el.onload === "function") { try { el.onload(); } catch (e) {} }
    } else {
      // 外部脚本（umid 服务等）：记录下来，等宿主回注
      __queueScriptRequest(src, el);
    }
  }

  // ---------- 宿主网络桥：脚本请求与 fetch 统一排队 ----------
  var __netQueue = [];
  var __netSeq = 0;

  function __queueScriptRequest(url, el) {
    __netQueue.push({ id: ++__netSeq, kind: "script", url: String(url), el: el });
    __log("net script: " + String(url).slice(0, 200));
  }

  function __queueFetch(url, opts, resolve, reject) {
    __netQueue.push({
      id: ++__netSeq, kind: "fetch", url: String(url),
      method: (opts && opts.method) || "GET",
      body: opts && opts.body != null ? String(opts.body) : null,
      resolve: resolve, reject: reject,
    });
    __log("net fetch: " + String(url).slice(0, 200));
  }

  // 宿主取出待处理请求
  function __fetchExternal() {
    var out = [];
    for (var i = 0; i < __netQueue.length; i++) {
      var q = __netQueue[i];
      if (q.reported) continue;
      q.reported = true;
      out.push({ id: q.id, kind: q.kind, url: q.url, method: q.method || "GET", body: q.body });
    }
    return JSON.stringify(out);
  }

  // 宿主回注响应：script → eval body + onload；fetch → resolve Response 垫片
  function __provideExternal(id, status, headersJson, body) {
    var q = null;
    for (var i = 0; i < __netQueue.length; i++) {
      if (__netQueue[i].id === id) { q = __netQueue.splice(i, 1)[0]; break; }
    }
    if (!q) return false;
    var headers = {};
    try { headers = JSON.parse(headersJson || "{}"); } catch (e) {}
    if (q.kind === "script") {
      try { (0, eval)(String(body) + "\n//# sourceURL=" + q.url); } catch (e) {
        __log("external exec error " + q.url.slice(0, 80) + ": " + (e && e.message));
      }
      if (q.el && typeof q.el.onload === "function") { try { q.el.onload(); } catch (e) {} }
      return true;
    }
    var respHeaders = {
      get: function (k) {
        k = String(k).toLowerCase();
        for (var h in headers) if (h.toLowerCase() === k) return headers[h];
        return null;
      },
      has: function (k) { return respHeaders.get(k) !== null; },
      forEach: function (cb) { for (var h in headers) cb(headers[h], h); },
    };
    var resp = {
      status: status, ok: status >= 200 && status < 300, statusText: "",
      headers: respHeaders,
      url: q.url,
      text: function () { return Promise.resolve(String(body)); },
      json: function () { return Promise.resolve(JSON.parse(body)); },
      arrayBuffer: function () {
        var s = String(body), b = new Uint8Array(s.length);
        for (var i = 0; i < s.length; i++) b[i] = s.charCodeAt(i) & 255;
        return Promise.resolve(b.buffer);
      },
      clone: function () { return resp; },
    };
    q.resolve(resp);
    return true;
  }

  // ---------- document ----------
  var __currentScript = null;
  var headEl = makeElement("head");
  var bodyEl = makeElement("body");
  var docEl = makeElement("html");

  var documentShim = {
    nodeType: 9,
    readyState: "complete",
    referrer: "",
    title: "",
    characterSet: "UTF-8",
    charset: "UTF-8",
    cookie: "",
    hidden: false,
    visibilityState: "visible",
    compatMode: "CSS1Compat",
    designMode: "off",
    documentElement: docEl,
    head: headEl,
    body: bodyEl,
    forms: [],
    images: [],
    links: [],
    scripts: __scriptElements,
    createElement: function (t) { return makeElement(t); },
    createElementNS: function (ns, t) { return makeElement(t); },
    createTextNode: function (t) { return { nodeType: 3, textContent: String(t) }; },
    createDocumentFragment: function () { return makeElement("fragment"); },
    createEvent: function () {
      return { initEvent: function (type) { this.type = type; }, type: "", bubbles: true, cancelable: true };
    },
    getElementById: function (id) { return __elementsById[id] || null; },
    getElementsByTagName: function (t) {
      t = String(t).toLowerCase();
      if (t === "head") return [headEl];
      if (t === "body") return [bodyEl];
      if (t === "html") return [docEl];
      if (t === "script") return __scriptElements.slice();
      return [];
    },
    getElementsByClassName: function () { return []; },
    getElementsByName: function () { return []; },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    addEventListener: function () {}, removeEventListener: function () {},
    attachEvent: function () {}, detachEvent: function () {},
    dispatchEvent: function () { return true; },
    write: function (html) {
      // 支持 <script src=...></script> 同步写入
      var m = /<script[^>]*src=["']?([^"'>\s]+)/i.exec(String(html));
      if (m) {
        var el = makeElement("script");
        el.src = m[1];
        __loadScriptElement(el);
      }
    },
    writeln: function (html) { documentShim.write(html); },
    open: function () {}, close: function () {},
  };
  Object.defineProperty(documentShim, "currentScript", {
    get: function () { return __currentScript; },
    configurable: true,
  });
  docEl.appendChild(headEl);
  docEl.appendChild(bodyEl);

  // ---------- navigator / screen / location / history ----------
  var __opts = { url: "https://passport.aliyun.com/", ua: "", referrer: "" };

  var navigatorShim = {
    appCodeName: "Mozilla",
    appName: "Netscape",
    appVersion: "",
    platform: "Win32",
    product: "Gecko",
    productSub: "20030107",
    vendor: "Google Inc.",
    vendorSub: "",
    language: "zh-CN",
    languages: ["zh-CN", "zh"],
    cookieEnabled: true,
    doNotTrack: null,
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    onLine: true,
    webdriver: false,
    pdfViewerEnabled: true,
    plugins: { length: 0, item: function () { return null; }, namedItem: function () { return null; }, refresh: function () {} },
    mimeTypes: { length: 0, item: function () { return null; }, namedItem: function () { return null; } },
    connection: { effectiveType: "4g", downlink: 10, rtt: 50 },
    javaEnabled: function () { return false; },
    sendBeacon: function () { return true; },
    getBattery: function () { return Promise.resolve({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1 }); },
  };

  var screenShim = {
    width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
    availLeft: 0, availTop: 0, colorDepth: 24, pixelDepth: 24,
    orientation: { angle: 0, type: "landscape-primary", onchange: null },
  };

  var locationShim = {};
  function __applyUrl(url) {
    var m = /^(https?):\/\/([^\/:?#]+)(?::(\d+))?([^?#]*)(\?[^#]*)?(#.*)?$/.exec(url);
    locationShim.href = url;
    locationShim.protocol = (m && m[1] ? m[1] : "https") + ":";
    locationShim.host = m && m[2] ? m[2] : "";
    locationShim.hostname = locationShim.host;
    locationShim.port = m && m[3] ? m[3] : "";
    locationShim.pathname = m && m[4] ? m[4] : "/";
    locationShim.search = m && m[5] ? m[5] : "";
    locationShim.hash = m && m[6] ? m[6] : "";
    locationShim.origin = locationShim.protocol + "//" + locationShim.host;
    locationShim.ancestorOrigins = { length: 0, item: function () { return null; }, contains: function () { return false; } };
  }
  locationShim.assign = function () {};
  locationShim.replace = function () {};
  locationShim.reload = function () {};
  locationShim.toString = function () { return locationShim.href; };

  var historyShim = { length: 2, state: null, pushState: function () {}, replaceState: function () {}, back: function () {}, forward: function () {}, go: function () {} };

  // ---------- XHR / Image / fetch（网络一律不外发） ----------
  function XHRShim() {
    this.readyState = 0; this.status = 0; this.responseText = ""; this.headers = {};
    this.onreadystatechange = null; this.onload = null; this.onerror = null;
  }
  XHRShim.prototype.open = function (m, u) { this.method = m; this.url = u; };
  XHRShim.prototype.setRequestHeader = function (k, v) { this.headers[k] = v; };
  XHRShim.prototype.getAllResponseHeaders = function () { return ""; };
  XHRShim.prototype.getResponseHeader = function () { return null; };
  XHRShim.prototype.send = function () {
    __log("xhr blocked: " + String(this.method) + " " + String(this.url).slice(0, 120));
  };
  XHRShim.prototype.abort = function () {};
  XHRShim.prototype.addEventListener = function () {};

  function ImageShim() {
    this.onload = null; this.onerror = null; this.width = 0; this.height = 0;
  }
  Object.defineProperty(ImageShim.prototype, "src", {
    set: function (v) { __log("img: " + String(v).slice(0, 2000)); this.__src = v; },
    get: function () { return this.__src || ""; },
  });

  // ---------- window ----------
  var winListeners = {};
  var windowShim = {
    innerWidth: 1920, innerHeight: 945, outerWidth: 1920, outerHeight: 1040,
    devicePixelRatio: 1, pageXOffset: 0, pageYOffset: 0, screenX: 0, screenY: 0,
    screenLeft: 0, screenTop: 0,
    name: "",
    closed: false,
    status: "",
    screen: screenShim,
    navigator: navigatorShim,
    location: locationShim,
    document: documentShim,
    history: historyShim,
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    XMLHttpRequest: XHRShim,
    Image: ImageShim,
    setTimeout: setTimeoutShim,
    clearTimeout: clearTimerShim,
    setInterval: setIntervalShim,
    clearInterval: clearTimerShim,
    queueMicrotask: function (cb) { Promise.resolve().then(cb); },
    requestAnimationFrame: function (cb) { return setTimeoutShim(function () { cb(__fakeNow); }, 16); },
    cancelAnimationFrame: clearTimerShim,
    addEventListener: function (type, cb) { (winListeners[type] = winListeners[type] || []).push(cb); },
    removeEventListener: function () {},
    attachEvent: function (type, cb) { (winListeners[type.replace(/^on/, "")] = winListeners[type.replace(/^on/, "")] || []).push(cb); },
    detachEvent: function () {},
    dispatchEvent: function (ev) {
      var ls = winListeners[ev && ev.type] || [];
      for (var i = 0; i < ls.length; i++) { try { ls[i](ev); } catch (e) {} }
      return true;
    },
    matchMedia: function (query) {
      return {
        matches: false, media: String(query), onchange: null,
        addListener: function () {}, removeListener: function () {},
        addEventListener: function () {}, removeEventListener: function () {},
        dispatchEvent: function () { return false; },
      };
    },
    getComputedStyle: function () {
      return new Proxy({}, { get: function (t, k) { if (k === "getPropertyValue") return function () { return ""; }; return ""; } });
    },
    postMessage: function () {},
    open: function () { return null; },
    close: function () {}, focus: function () {}, blur: function () {},
    alert: function () {}, confirm: function () { return false; }, prompt: function () { return null; },
    print: function () {},
    scroll: function () {}, scrollTo: function () {}, scrollBy: function () {},
    stop: function () {},
    // umid JSONP 回调（官方 wu.json 响应固定调用 umx.wu(token)）
    umx: {
      wu: function (t) { G.__capturedUmidToken = String(t); },
    },
    // 裸引擎没有 console
    console: {
      log: function () {}, error: function () {}, warn: function () {},
      info: function () {}, debug: function () {}, trace: function () {},
      group: function () {}, groupEnd: function () {}, time: function () {}, timeEnd: function () {},
    },
    // fetch 挂起并转交宿主执行（SDK 的 umid 请求走这里）
    fetch: function (url, opts) {
      return new Promise(function (resolve, reject) {
        __queueFetch(url, opts, resolve, reject);
      });
    },
    btoa: function (s) {
      var chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
      var out = "", i = 0;
      s = String(s);
      while (i < s.length) {
        var c1 = s.charCodeAt(i++) & 255, c2 = s.charCodeAt(i++), c3 = s.charCodeAt(i++);
        var e1 = c1 >> 2, e2 = ((c1 & 3) << 4) | ((c2 || 0) >> 4);
        var e3 = isNaN(c2) ? 64 : (((c2 & 15) << 2) | ((c3 || 0) >> 6));
        var e4 = isNaN(c3) ? 64 : (c3 & 63);
        out += chars.charAt(e1) + chars.charAt(e2) + (e3 === 64 ? "=" : chars.charAt(e3)) + (e4 === 64 ? "=" : chars.charAt(e4));
      }
      return out;
    },
    atob: function (s) {
      var chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
      var out = "", i = 0;
      s = String(s).replace(/=+$/, "");
      while (i < s.length) {
        var e1 = chars.indexOf(s.charAt(i++)), e2 = chars.indexOf(s.charAt(i++));
        var e3 = chars.indexOf(s.charAt(i++)), e4 = chars.indexOf(s.charAt(i++));
        var c1 = (e1 << 2) | (e2 >> 4), c2 = ((e2 & 15) << 4) | (e3 >> 2), c3 = ((e3 & 3) << 6) | e4;
        out += String.fromCharCode(c1);
        if (e3 !== -1 && e3 !== 64) out += String.fromCharCode(c2);
        if (e4 !== -1 && e4 !== 64) out += String.fromCharCode(c3);
      }
      return out;
    },
    performance: {
      timeOrigin: 1700000000000,
      now: function () { __fakeNow += 3; return __fakeNow - 1700000000000; },
      timing: { navigationStart: 1700000000000, fetchStart: 1700000000001, domainLookupStart: 1700000000002, domainLookupEnd: 1700000000003, connectStart: 1700000000004, connectEnd: 1700000000005, requestStart: 1700000000010, responseStart: 1700000000020, responseEnd: 1700000000030, domLoading: 1700000000040, domInteractive: 1700000000100, domContentLoadedEventStart: 1700000000110, domContentLoadedEventEnd: 1700000000120, domComplete: 1700000000200, loadEventStart: 1700000000210, loadEventEnd: 1700000000220 },
      navigation: { type: 0, redirectCount: 0 },
      getEntriesByType: function () { return []; },
      getEntries: function () { return []; },
      mark: function () {}, measure: function () {}, clearMarks: function () {}, clearMeasures: function () {},
      toJSON: function () { return {}; },
    },
    crypto: {
      getRandomValues: function (arr) {
        for (var i = 0; i < arr.length; i++) arr[i] = __nextRandomByte();
        return arr;
      },
      randomUUID: function () {
        var h = "";
        for (var i = 0; i < 32; i++) h += "0123456789abcdef".charAt(__nextRandomByte() & 15);
        return h.slice(0, 8) + "-" + h.slice(8, 12) + "-4" + h.slice(13, 16) + "-a" + h.slice(17, 20) + "-" + h.slice(20, 32);
      },
    },
  };

  // TextEncoder/TextDecoder 纯 JS 兜底（mini_racer 没有）
  if (typeof G.TextEncoder === "undefined") {
    windowShim.TextEncoder = function () {};
    windowShim.TextEncoder.prototype.encode = function (str) {
      str = String(str);
      var out = [];
      for (var i = 0; i < str.length; i++) {
        var code = str.charCodeAt(i);
        if (code < 0x80) out.push(code);
        else if (code < 0x800) { out.push(0xc0 | (code >> 6), 0x80 | (code & 63)); }
        else if (code >= 0xd800 && code <= 0xdbff && i + 1 < str.length) {
          var lo = str.charCodeAt(++i);
          var cp = 0x10000 + ((code & 0x3ff) << 10) + (lo & 0x3ff);
          out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 63), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
        } else { out.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 63), 0x80 | (code & 63)); }
      }
      return new Uint8Array(out);
    };
    windowShim.TextEncoder.prototype.encoding = "utf-8";
  }
  if (typeof G.TextDecoder === "undefined") {
    windowShim.TextDecoder = function () {};
    windowShim.TextDecoder.prototype.decode = function (buf) {
      var bytes = new Uint8Array(buf.buffer || buf);
      var out = "", i = 0;
      while (i < bytes.length) {
        var b = bytes[i];
        if (b < 0x80) { out += String.fromCharCode(b); i += 1; }
        else if (b < 0xe0) { out += String.fromCharCode(((b & 31) << 6) | (bytes[i + 1] & 63)); i += 2; }
        else if (b < 0xf0) { out += String.fromCharCode(((b & 15) << 12) | ((bytes[i + 1] & 63) << 6) | (bytes[i + 2] & 63)); i += 3; }
        else {
          var cp = ((b & 7) << 18) | ((bytes[i + 1] & 63) << 12) | ((bytes[i + 2] & 63) << 6) | (bytes[i + 3] & 63);
          cp -= 0x10000;
          out += String.fromCharCode(0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff));
          i += 4;
        }
      }
      return out;
    };
    windowShim.TextDecoder.prototype.encoding = "utf-8";
  }

  // 常见构造器与原型（官方 SDK 会做原型链内省）
  function makeClass(name, proto) {
    var fn = function () {};
    try { Object.defineProperty(fn, "name", { value: name }); } catch (e) {}
    fn.prototype = proto || {};
    try { Object.defineProperty(fn.prototype, "constructor", { value: fn, writable: true, configurable: true }); } catch (e2) {}
    return fn;
  }
  var EventTarget = makeClass("EventTarget");
  var NodeC = makeClass("Node");
  var ElementC = makeClass("Element");
  var HTMLElementC = makeClass("HTMLElement");
  var HTMLDocumentC = makeClass("HTMLDocument");
  var DocumentC = makeClass("Document");
  var NavigatorC = makeClass("Navigator");
  var ScreenC = makeClass("Screen");
  var LocationC = makeClass("Location");
  var HistoryC = makeClass("History");
  var StorageC = makeClass("Storage");
  var WindowC = makeClass("Window");
  var HTMLCanvasElementC = makeClass("HTMLCanvasElement");
  var HTMLScriptElementC = makeClass("HTMLScriptElement");
  var HTMLIFrameElementC = makeClass("HTMLIFrameElement");
  // Document.prototype.head 等常见 getter
  try {
    Object.defineProperty(DocumentC.prototype, "head", { get: function () { return headEl; }, configurable: true });
    Object.defineProperty(DocumentC.prototype, "body", { get: function () { return bodyEl; }, configurable: true });
    Object.defineProperty(DocumentC.prototype, "documentElement", { get: function () { return docEl; }, configurable: true });
  } catch (e) {}

  windowShim.EventTarget = EventTarget;
  windowShim.Node = NodeC;
  windowShim.Element = ElementC;
  windowShim.HTMLElement = HTMLElementC;
  windowShim.HTMLDocument = HTMLDocumentC;
  windowShim.Document = DocumentC;
  windowShim.Navigator = NavigatorC;
  windowShim.Screen = ScreenC;
  windowShim.Location = LocationC;
  windowShim.History = HistoryC;
  windowShim.Storage = StorageC;
  windowShim.Window = WindowC;
  windowShim.HTMLCanvasElement = HTMLCanvasElementC;
  windowShim.HTMLScriptElement = HTMLScriptElementC;
  windowShim.HTMLIFrameElement = HTMLIFrameElementC;
  windowShim.MouseEvent = makeClass("MouseEvent");
  windowShim.KeyboardEvent = makeClass("KeyboardEvent");
  windowShim.Event = makeClass("Event");
  windowShim.CustomEvent = makeClass("CustomEvent");
  windowShim.PromiseRejectionEvent = makeClass("PromiseRejectionEvent");

  // ---------- 挂到全局 ----------
  for (var key in windowShim) {
    if (!(key in G)) G[key] = windowShim[key];
  }
  // window 系列自引用必须指向全局本身
  G.window = G;
  G.self = G;
  G.top = G;
  G.parent = G;
  G.frames = G;
  G.globalThis = G;

  // ---------- 对外控制接口 ----------
  G.__setOptions = function (pageUrl, userAgent, referrer) {
    __opts.url = pageUrl;
    __opts.ua = userAgent;
    __applyUrl(pageUrl);
    navigatorShim.userAgent = userAgent;
    navigatorShim.appVersion = String(userAgent).replace(/^Mozilla\//, "");
    documentShim.referrer = referrer || "";
    documentShim.title = "阿里云登录";
  };

  G.__setRandomPool = function (arr) { __randomPool = arr; __randomIdx = 0; };
  G.__pump = __pump;
  G.__fetchExternal = __fetchExternal;
  G.__provideExternal = __provideExternal;

  // fireyejs 初始化与令牌获取
  var __fyMod = null;
  var __fyError = null;
  G.__startFy = function (optionsJson) {
    var opts = JSON.parse(optionsJson);
    if (!G.AWSC || typeof G.AWSC.use !== "function") { __fyError = "AWSC 未初始化"; return false; }
    G.AWSC.use("fy", function (status, mod) {
      if (status !== "loaded") { __fyError = "fy 加载失败: " + status; return; }
      try {
        mod.init(opts, function () { __fyMod = mod; });
      } catch (e) {
        __fyError = "fy init 异常: " + (e && e.message);
      }
    }, { timeout: 10000 });
    return true;
  };

  G.__fyState = function () {
    var out = { bxUa: null, bxUmidToken: null, error: __fyError, ready: !!__fyMod };
    if (__fyMod) {
      var opts = { noProxy: true, location: "cn", MaxMTLog: 20, MaxNGPLog: 10, MaxKSLog: 5, MaxFocusLog: 3 };
      try { out.bxUa = __fyMod.getFYToken(opts); } catch (e) { out.uaError = String(e && e.message); }
      try { out.bxUmidToken = __fyMod.getUidToken(opts); } catch (e) { out.umidError = String(e && e.message); }
    }
    // umid 令牌以 wu.json JSONP 回调捕获值为准（与官方 getUidToken 返回值一致）
    if (G.__capturedUmidToken) out.bxUmidToken = G.__capturedUmidToken;
    if (out.bxUmidToken === "" || out.bxUmidToken == null) delete out.bxUmidToken;
    return JSON.stringify(out);
  };
})();

"""


if __name__ == "__main__":
    raise SystemExit(main())
