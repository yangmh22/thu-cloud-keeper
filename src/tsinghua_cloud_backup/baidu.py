from __future__ import annotations

import json
import mimetypes
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


PAN_BASE_URL = "https://pan.baidu.com"
PCS_UPLOAD_BASE_URL = "https://d.pcs.baidu.com"
OAUTH_BASE_URL = "https://openapi.baidu.com/oauth/2.0"
DEVICE_VERIFY_URL = "https://openapi.baidu.com/device"
BAIDU_USER_AGENT = "pan.baidu.com"
BAIDU_NETDISK_APP_ID = "250528"


class BaiduApiError(Exception):
    def __init__(self, message: str, payload: dict | None = None, status: int | None = None):
        super().__init__(message)
        self.payload = payload or {}
        self.status = status


class BaiduAuthClient:
    def __init__(self, timeout: int = 60, retries: int = 3):
        self.timeout = timeout
        self.retries = retries

    def get_device_code(self, client_id: str) -> dict:
        return self.get_json(
            f"{OAUTH_BASE_URL}/device/code",
            {
                "response_type": "device_code",
                "client_id": client_id,
                "scope": "basic,netdisk",
            },
        )

    def poll_device_token(
        self,
        client_id: str,
        client_secret: str,
        device_code: str,
        interval: int = 5,
        expires_in: int = 300,
    ) -> dict:
        deadline = time.time() + max(1, expires_in)
        delay = max(5, int(interval or 5))
        last_payload: dict | None = None
        while time.time() < deadline:
            payload = self.get_json(
                f"{OAUTH_BASE_URL}/token",
                {
                    "grant_type": "device_token",
                    "code": device_code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                raise_for_errno=False,
            )
            if payload.get("access_token"):
                return payload
            last_payload = payload
            error = str(payload.get("error") or payload.get("error_description") or "")
            if error and "authorization_pending" not in error and "slow_down" not in error:
                raise BaiduApiError(f"百度授权失败：{payload}", payload)
            time.sleep(delay)
        raise BaiduApiError(f"百度授权超时：{last_payload or {}}", last_payload or {})

    def refresh_access_token(self, client_id: str, client_secret: str, refresh_token: str) -> dict:
        return self.get_json(
            f"{OAUTH_BASE_URL}/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )

    def get_json(self, url: str, params: dict | None = None, raise_for_errno: bool = True) -> dict:
        query = urllib.parse.urlencode(params or {})
        if query:
            url = f"{url}?{query}"
        with self.open(url) as response:
            payload = json.load(response)
        if raise_for_errno:
            check_errno(payload)
        return payload

    def open(self, url: str):
        request = urllib.request.Request(url, headers={"User-Agent": BAIDU_USER_AGENT})
        last_error = None
        delay = 2
        for attempt in range(1, self.retries + 1):
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in (429, 500, 502, 503, 504):
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise last_error


class BaiduNetdiskClient:
    def __init__(self, access_token: str, timeout: int = 120, retries: int = 4):
        self.access_token = access_token
        self.timeout = timeout
        self.retries = retries

    def user_info(self) -> dict:
        return self.get_json("/rest/2.0/xpan/nas", {"method": "uinfo", "vip_version": "v2"})

    def quota(self) -> dict:
        return self.get_json("/api/quota", {"checkfree": "1", "checkexpire": "1"})

    def list_dir(self, remote_dir: str, start: int = 0, limit: int = 1000) -> dict:
        return self.get_json(
            "/rest/2.0/xpan/file",
            {
                "method": "list",
                "dir": remote_dir,
                "order": "name",
                "start": str(start),
                "limit": str(limit),
                "web": "1",
                "folder": "0",
                "desc": "0",
            },
        )

    def create_dir(self, remote_path: str) -> dict:
        payload = self.post_form(
            "/rest/2.0/xpan/file",
            {"method": "create"},
            {"path": remote_path, "isdir": "1", "rtype": "0"},
            raise_for_errno=False,
        )
        errno = int(payload.get("errno", 0) or 0)
        if errno not in (0, -8):
            raise BaiduApiError(f"百度网盘创建目录失败：{remote_path} | {payload}", payload)
        return payload

    def precreate(self, remote_path: str, size: int, block_md5s: list[str], rtype: int = 2) -> dict:
        payload = self.post_form(
            "/rest/2.0/xpan/file",
            {"method": "precreate"},
            {
                "path": remote_path,
                "size": str(size),
                "isdir": "0",
                "autoinit": "1",
                "rtype": str(rtype),
                "block_list": json.dumps(block_md5s, separators=(",", ":")),
            },
        )
        if "uploadid" not in payload:
            raise BaiduApiError(f"百度网盘预上传未返回 uploadid：{remote_path} | {payload}", payload)
        return payload

    def locate_upload(self, remote_path: str, uploadid: str) -> str:
        payload = self.get_json(
            "/rest/2.0/pcs/file",
            {
                "method": "locateupload",
                "appid": BAIDU_NETDISK_APP_ID,
                "path": remote_path,
                "uploadid": uploadid,
                "upload_version": "2.0",
            },
            base_url=PCS_UPLOAD_BASE_URL,
            raise_for_errno=False,
        )
        servers = payload.get("servers") or []
        fallback = ""
        for server in servers:
            if isinstance(server, str):
                value = server.rstrip("/")
                if value.startswith("https://"):
                    return value
                fallback = fallback or value
            if isinstance(server, dict):
                value = server.get("server") or server.get("host") or server.get("url")
                if value:
                    value = str(value).rstrip("/")
                    if value.startswith("https://"):
                        return value
                    fallback = fallback or value
        return fallback or "https://c3.pcs.baidu.com"

    def upload_part(self, upload_base_url: str, remote_path: str, uploadid: str, partseq: int, part_path: Path) -> dict:
        params = {
            "method": "upload",
            "type": "tmpfile",
            "path": remote_path,
            "uploadid": uploadid,
            "partseq": str(partseq),
        }
        return self.post_multipart(
            "/rest/2.0/pcs/superfile2",
            params,
            file_field="file",
            file_path=part_path,
            base_url=upload_base_url,
        )

    def create_file(self, remote_path: str, size: int, block_md5s: list[str], uploadid: str, rtype: int = 2) -> dict:
        return self.post_form(
            "/rest/2.0/xpan/file",
            {"method": "create"},
            {
                "path": remote_path,
                "size": str(size),
                "isdir": "0",
                "rtype": str(rtype),
                "uploadid": uploadid,
                "block_list": json.dumps(block_md5s, separators=(",", ":")),
            },
        )

    def get_json(
        self,
        path: str,
        params: dict | None = None,
        base_url: str = PAN_BASE_URL,
        raise_for_errno: bool = True,
    ) -> dict:
        query_params = dict(params or {})
        query_params["access_token"] = self.access_token
        url = f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode(query_params)}"
        try:
            with self.open(url) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise baidu_http_error("GET", safe_url(base_url, path, query_params), exc) from exc
        if raise_for_errno:
            check_errno(payload)
        return payload

    def post_form(
        self,
        path: str,
        params: dict | None,
        form: dict,
        base_url: str = PAN_BASE_URL,
        raise_for_errno: bool = True,
    ) -> dict:
        query_params = dict(params or {})
        query_params["access_token"] = self.access_token
        url = f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode(query_params)}"
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers = {
            "User-Agent": BAIDU_USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            with self.open(url, data=body, headers=headers) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise baidu_http_error("POST", safe_url(base_url, path, query_params), exc) from exc
        if raise_for_errno:
            check_errno(payload)
        return payload

    def post_multipart(
        self,
        path: str,
        params: dict | None,
        file_field: str,
        file_path: Path,
        base_url: str,
    ) -> dict:
        query_params = dict(params or {})
        query_params["access_token"] = self.access_token
        url = f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode(query_params)}"
        boundary = f"----thu-cloud-keeper-{uuid.uuid4().hex}"
        filename = file_path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = prefix + file_path.read_bytes() + suffix
        headers = {
            "User-Agent": BAIDU_USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        try:
            with self.open(url, data=body, headers=headers, timeout=max(self.timeout, 1800)) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise baidu_http_error("POST", safe_url(base_url, path, query_params), exc) from exc
        check_errno(payload)
        return payload

    def open(self, url: str, data: bytes | None = None, headers: dict | None = None, timeout: int | None = None):
        request_headers = {"User-Agent": BAIDU_USER_AGENT}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, data=data, headers=request_headers)
        last_error = None
        delay = 2
        for attempt in range(1, self.retries + 1):
            try:
                return urllib.request.urlopen(request, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in (429, 500, 502, 503, 504):
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise last_error


def check_errno(payload: dict) -> None:
    errno = payload.get("errno")
    error_code = payload.get("error_code")
    if errno not in (None, 0):
        raise BaiduApiError(f"百度网盘接口错误：{payload}", payload)
    if error_code not in (None, 0):
        raise BaiduApiError(f"百度 OAuth 接口错误：{payload}", payload)


def safe_url(base_url: str, path: str, query_params: dict) -> str:
    params = {key: value for key, value in query_params.items() if key != "access_token"}
    query = urllib.parse.urlencode(params)
    suffix = f"?{query}" if query else ""
    return f"{base_url.rstrip('/')}{path}{suffix}"


def baidu_http_error(method: str, endpoint: str, exc: urllib.error.HTTPError) -> BaiduApiError:
    body = ""
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    if len(body) > 800:
        body = body[:800] + "...[truncated]"
    detail = f" | body={body}" if body else ""
    return BaiduApiError(f"百度网盘 HTTP {exc.code}：{method} {endpoint} | {exc.reason}{detail}", status=exc.code)


def baidu_auth_url(user_code: str, verification_url: str = DEVICE_VERIFY_URL) -> str:
    return f"{verification_url}?{urllib.parse.urlencode({'display': 'mobile', 'code': user_code})}"
