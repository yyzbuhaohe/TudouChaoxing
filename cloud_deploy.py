# -*- coding: utf-8 -*-
"""把本地配置一键部署到 GitHub —— 给别人用的时候，不用他碰命令行。

干四件事，全走 GitHub REST API：
    1. 认仓库           GET  /repos/{owner}/{repo}
    2. 传 Secrets       PUT  /repos/{owner}/{repo}/actions/secrets/{name}（libsodium 密封盒加密）
    3. 传 workflow      PUT  /repos/{owner}/{repo}/contents/.github/workflows/main.yml
    4. 立刻跑一次       POST /repos/{owner}/{repo}/actions/workflows/main.yml/dispatches

依赖：requests（本来就有）+ pynacl（加密 Secrets 用，纯 wheel）。
pynacl 没装时不会崩，只是"上传 Secrets"这一步会明确告诉你缺什么。

错误一律翻译成人话，不要让用户去猜 401/403/404 是啥意思。
"""
import base64
import json
import os
import time

import requests

# 有些人的机器上跑着加速器 / 安全软件（Steam++、Watt Toolkit、部分杀软），
# 它们会给所有 HTTPS 做中间人，根证书装在 Windows 证书存储里。
# Python 默认只信 certifi 那套 CA 包，不认系统证书，于是直接报
# SSLCertVerificationError —— 表现就是「Token 明明是对的，却连不上 GitHub」。
# truststore 把 ssl 切到系统证书存储，一行解决；没装也不影响，退回默认行为。
try:
    import truststore

    truststore.inject_into_ssl()
    SSL_BACKEND = "system"
except Exception:  # noqa: BLE001
    SSL_BACKEND = "certifi"

API = "https://api.github.com"
UA = "tudou-cloud-deploy"
TIMEOUT = 30


class DeployError(Exception):
    """带人话提示的部署错误"""


def _headers(token):
    return {
        "Authorization": "Bearer " + (token or "").strip(),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }


HINT_401 = "Token 无效、过期，或者复制时少了字符。重新生成一个再试。"
HINT_404 = "找不到这个仓库。核对两件事：用户名/仓库名拼对了吗、Token 有没有勾 repo 权限。"
HINT_403 = ("权限不够。生成 Token 时要把 repo 和 workflow 这两个都勾上；"
            "如果已经勾了，可能是短时间内请求太多，等一分钟再来。")


# ---------------- 一键登录（OAuth Device Flow） ----------------
# 为什么用它：Personal Access Token 的路径太深（Settings → Developer settings →
# Tokens (classic) → Generate → 勾 scope → 复制），新手十个有八个卡在这。
# Device Flow 只要"点一下 → 浏览器输入 6 位码 → 点 Authorize"，没有比这更短的。
#
# client_id 是 GitHub CLI 的公开客户端，device flow 本身就是为"没有回调地址的
# 命令行/桌面工具"设计的；它不携带任何密钥，只用来标识发起方。
DEVICE_CLIENT_ID = "178c6fc778ccc68e1d6a"
DEVICE_SCOPE = "repo workflow"
DEVICE_CODE_URL = "https://github.com/login/device/code"
DEVICE_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEVICE_VERIFY_URL = "https://github.com/login/device"


class DeviceFlowError(DeployError):
    """一键登录过程中的错误（过期、被拒绝、网络）"""


def device_start(scope=DEVICE_SCOPE, client_id=DEVICE_CLIENT_ID, session=None):
    """申请一个设备码。返回 dict：user_code / device_code / interval / expires_in / verification_uri"""
    s = session or requests.Session()
    try:
        resp = s.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": scope},
                      headers={"Accept": "application/json", "User-Agent": UA}, timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        raise DeviceFlowError("连不上 GitHub 授权服务：%s" % str(exc)[:120]) from exc
    if resp.status_code != 200:
        raise DeviceFlowError("GitHub 拒绝了登录请求（HTTP %s）" % resp.status_code)
    data = resp.json()
    if data.get("error"):
        raise DeviceFlowError("GitHub 返回错误：%s" % data.get("error_description") or data.get("error"))
    return {
        "user_code": data.get("user_code", ""),
        "device_code": data.get("device_code", ""),
        "interval": int(data.get("interval") or 5),
        "expires_in": int(data.get("expires_in") or 900),
        "verification_uri": data.get("verification_uri") or DEVICE_VERIFY_URL,
    }


def device_poll(device_code, interval=5, expires_in=900, client_id=DEVICE_CLIENT_ID,
                session=None, should_stop=None, on_tick=None):
    """等用户在浏览器里点 Authorize。

    GitHub 的规矩：没授权前返回 error=authorization_pending，要按 interval 轮询；
    轮太快会回 slow_down。这里都处理了，调用方只要等 token 或异常。
    """
    s = session or requests.Session()
    waited = 0
    step = max(5, int(interval))
    while waited < int(expires_in):
        if should_stop and should_stop():
            raise DeviceFlowError("已取消登录")
        try:
            resp = s.post(DEVICE_TOKEN_URL,
                          data={"client_id": client_id, "device_code": device_code,
                                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
                          headers={"Accept": "application/json", "User-Agent": UA}, timeout=TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise DeviceFlowError("等待授权时网络断了：%s" % str(exc)[:120]) from exc
        data = {}
        try:
            data = resp.json()
        except ValueError:
            pass
        token = data.get("access_token")
        if token:
            return token
        err = data.get("error") or ""
        if err == "authorization_pending":
            pass
        elif err == "slow_down":
            step += 5
            if on_tick:
                on_tick("请求太频，等久一点…")
        elif err == "expired_token":
            raise DeviceFlowError("验证码过期了，重新点一次「一键登录」")
        elif err == "access_denied":
            raise DeviceFlowError("你在浏览器里点了取消，所以没登录上")
        elif err:
            raise DeviceFlowError("GitHub 返回：%s" % (data.get("error_description") or err))
        time.sleep(step)
        waited += step
    raise DeviceFlowError("等了 %d 分钟还没授权，验证码过期了，重新点一次" % (expires_in // 60))


def has_pynacl():
    try:
        import nacl  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


class GitHubDeploy:
    def __init__(self, owner, repo, token, session=None, log=None):
        self.owner = (owner or "").strip()
        self.repo = (repo or "").strip()
        self.token = (token or "").strip()
        self.s = session or requests.Session()
        self.log = log or (lambda *_: None)

    # ---------------- 底层 ----------------

    @property
    def api(self):
        return "%s/repos/%s/%s" % (API, self.owner, self.repo)

    def _req(self, method, url, **kw):
        kw.setdefault("timeout", TIMEOUT)
        try:
            resp = self.s.request(method, url, headers=_headers(self.token), **kw)
        except requests.exceptions.RequestException as exc:
            raise DeployError("连不上 GitHub：%s（检查网络 / 代理）" % str(exc)[:120]) from exc
        if resp.status_code >= 400:
            raise DeployError(self._translate(resp))
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:200]}

    @staticmethod
    def _translate(resp):
        try:
            body = resp.json()
            detail = str(body.get("message") or "")[:160]
        except ValueError:
            detail = (resp.text or "")[:120]
        code = resp.status_code
        if code == 401:
            return "%s（%s）" % (HINT_401, detail)
        if code == 403:
            return "%s（%s）" % (HINT_403, detail)
        if code == 404:
            return "%s（%s）" % (HINT_404, detail)
        if code == 422:
            return "这个请求被拒绝了：%s" % detail
        if code == 409:
            return "远端文件刚被改过，冲突了。再点一次就行。"
        return "GitHub 返回 %s：%s" % (code, detail)

    # ---------------- 具体动作 ----------------

    def whoami(self):
        return self._req("GET", API + "/user")

    def repo_info(self):
        """返回仓库信息；顺带判断是不是 fork、默认分支叫什么"""
        return self._req("GET", self.api)

    def list_secrets(self):
        data = self._req("GET", self.api + "/actions/secrets")
        return [s.get("name") for s in (data.get("secrets") or [])]

    def put_secret(self, name, value):
        if not has_pynacl():
            raise DeployError(
                "缺 pynacl，Secrets 传不上去。点「⑤ 环境 → 一键安装依赖」它会一起装上，"
                "或者手动 pip install pynacl。"
            )
        from nacl import encoding, public

        pk = self._req("GET", self.api + "/actions/secrets/public-key")
        box = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
        payload = {
            "encrypted_value": base64.b64encode(box.encrypt(value.encode())).decode(),
            "key_id": pk["key_id"],
        }
        self._req("PUT", self.api + "/actions/secrets/" + name, json=payload)
        return True

    def get_file(self, path):
        """返回 (内容文本, blob sha)；文件不存在返回 (None, None)"""
        try:
            data = self._req("GET", self.api + "/contents/" + path)
        except DeployError as exc:
            if "找不到" in str(exc):
                return None, None
            raise
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", "ignore"), data.get("sha")
        return None, None

    def put_file(self, path, text, message):
        _, sha = self.get_file(path)
        payload = {"message": message, "content": base64.b64encode(text.encode()).decode()}
        if sha:
            payload["sha"] = sha
        self._req("PUT", self.api + "/contents/" + path, json=payload)
        return True

    def dispatch(self, workflow="main.yml", ref="main", inputs=None):
        body = {"ref": ref}
        if inputs:
            body["inputs"] = inputs
        self._req("POST", self.api + "/actions/workflows/%s/dispatches" % workflow, json=body)
        return True

    def latest_run(self, workflow="main.yml"):
        data = self._req("GET", self.api + "/actions/workflows/%s/runs?per_page=1" % workflow)
        runs = data.get("workflow_runs") or []
        if not runs:
            return {}
        r = runs[0]
        return {
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "url": r.get("html_url"),
            "created_at": r.get("created_at"),
            "event": r.get("event"),
        }

    def actions_url(self):
        return "https://github.com/%s/%s/actions" % (self.owner, self.repo)

    # ---------------- 一把梭 ----------------

    def deploy(self, secrets=None, workflow_path=None, workflow_text=None,
               inputs=None, run_now=True):
        """按顺序做完：验身份 → 查仓库 → 传 Secrets → 传 workflow → 触发一次。

        返回 (是否全部成功, 步骤日志列表)。任何一步失败都抛 DeployError，
        调用方把日志打给用户看就行。
        """
        steps = []

        def ok(text):
            steps.append("✅ " + text)
            self.log("✅ " + text)

        def info(text):
            steps.append("· " + text)
            self.log("· " + text)

        me = self.whoami()
        ok("身份验证通过：%s" % me.get("login", "?"))

        repo = self.repo_info()
        info("仓库：%s（默认分支 %s）" % (repo.get("full_name"), repo.get("default_branch", "main")))
        if repo.get("fork"):
            info("这是个 fork 仓库，提醒对方：fork 后要在 Actions 页手动点一次「I understand…」才会跑定时")
        has_actions = repo.get("has_actions", True)
        if not has_actions:
            raise DeployError("这个仓库把 Actions 关掉了。到 Settings → Actions → General 里打开。")

        ref = repo.get("default_branch") or "main"

        if secrets:
            missing = [k for k in ("CHX_USER", "CHX_PASS") if not (secrets.get(k) or "").strip()]
            if missing:
                raise DeployError("账号或密码是空的，先回去把 ① 账号页填上（缺 %s）" % ", ".join(missing))
            if not (secrets.get("CHX_LLM_KEY") or "").strip():
                info("没填大模型 Key，云端会退化成随机作答，正确率不保证")
            for name, value in secrets.items():
                if value is None:
                    continue
                self.put_secret(name, str(value))
                ok("Secret 已写入：%s" % name)

        if workflow_text:
            path = workflow_path or ".github/workflows/main.yml"
            remote, _ = self.get_file(path)
            if remote is not None and remote == workflow_text:
                info("workflow 和远端一致，跳过上传")
            else:
                self.put_file(path, workflow_text, "chore: 用配置工具更新云端定时与超时")
                ok("workflow 已上传：%s" % path)

        if run_now:
            try:
                self.dispatch(workflow="main.yml", ref=ref, inputs=inputs or {})
                ok("已触发一次运行（去 Actions 页面看进度）")
            except DeployError as exc:
                # 触发失败不影响前面已经写好的东西，只提醒手动点
                info("自动触发没成功（%s），去 Actions 页面手动 Run workflow 也一样" % str(exc)[:80])

        return True, steps


def load_profile(profile_path):
    """读取本机记住的 GitHub 部署信息（token 明文存在本机，已 gitignore）"""
    try:
        with open(profile_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_profile(profile_path, data):
    try:
        with open(profile_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False
