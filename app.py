"""Local HTTPS server for an iPhone Korean defense assistant."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from openai import APIConnectionError, AuthenticationError, BadRequestError, OpenAI, OpenAIError, RateLimitError
import qrcode


BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
LOCAL = BASE / ".local"
TOKEN = secrets.token_urlsafe(24)
COOKIE_NAME = "defense_session"
PORT = int(os.getenv("DEFENSE_PORT", "8443"))
SETUP_PORT = int(os.getenv("DEFENSE_SETUP_PORT", "8001"))
TRANSCRIBE_MODEL = os.getenv("DEFENSE_TRANSCRIBE_MODEL", "whisper-1")
ANSWER_MODEL = os.getenv("DEFENSE_ANSWER_MODEL", "gpt-4o-mini")


GROQ_TRANSCRIBE_MODELS = ("whisper-large-v3-turbo", "whisper-large-v3")
GROQ_ANSWER_MODELS = (
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
)


def normalize_api_key(key: str) -> str:
    key = key.strip().strip('"').strip("'")
    if ("..." in key) or ("…" in key):
        raise SystemExit(
            "粘贴的是控制台里的密钥前缀（例如 gsk_8jp3...），不能用。"
            "请点 Create API Key，在弹窗里立刻复制整段完整密钥（很长，以 gsk_ 开头）。"
        )
    if key.startswith("sk-"):
        return key
    if not key.startswith("gsk_"):
        raise SystemExit("密钥格式不对。请复制 Groq 弹窗中以 gsk_ 开头的完整 Secret Key，不要复制列表页的 ID。")
    if len(key) < 16:
        raise SystemExit(
            "密钥太短。列表页只显示前缀，不是完整密钥。"
            "请点 Create API Key，复制弹窗中整段 gsk_ 开头的密钥后再启动。"
        )
    return key


def pick_first(candidates: tuple[str, ...], available: set[str]) -> str:
    if available:
        for name in candidates:
            if name in available:
                return name
    return candidates[0]


def configure_client(key: str) -> None:
    global CLIENT, TRANSCRIBE_MODEL, ANSWER_MODEL
    key = normalize_api_key(key)
    use_groq = key.startswith("gsk_")

    if use_groq:
        base_url = "https://api.groq.com/openai/v1"
        CLIENT = OpenAI(api_key=key, base_url=base_url, timeout=25.0, max_retries=1)
        TRANSCRIBE_MODEL = os.getenv("DEFENSE_TRANSCRIBE_MODEL", GROQ_TRANSCRIBE_MODELS[0])
        ANSWER_MODEL = os.getenv("DEFENSE_ANSWER_MODEL", GROQ_ANSWER_MODELS[0])
        return

    TRANSCRIBE_MODEL = os.getenv("DEFENSE_TRANSCRIBE_MODEL", "whisper-1")
    ANSWER_MODEL = os.getenv("DEFENSE_ANSWER_MODEL", "gpt-4o-mini")
    CLIENT = OpenAI(api_key=key, timeout=25.0, max_retries=1)


def make_client() -> OpenAI:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    return OpenAI(api_key=key if key else "dummy-key-for-init", timeout=25.0, max_retries=1)


CLIENT: OpenAI = make_client()


PRIVATE_NETWORKS = [ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def save_status(message: str) -> None:
    # Store only a short stage/status message, never credentials, audio, speech, or the tunnel URL.
    (BASE / "last_status.txt").write_text(message + "\n", encoding="utf-8")


def validate_api_key() -> None:
    global TRANSCRIBE_MODEL, ANSWER_MODEL
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise SystemExit("缺少 API 密钥。请运行 start.ps1 并在提示框中粘贴 Groq 完整密钥。")
    if key.startswith("key_"):
        raise SystemExit(
            "当前输入的是 key_ 开头的密钥 ID，不能用于 API 调用。"
            "请到 https://console.groq.com/keys 点 Create API Key，复制完整 gsk_ 密钥。"
        )
    try:
        configure_client(key)
        model_list = CLIENT.models.list()
    except AuthenticationError:
        raise SystemExit("API 密钥验证失败（401）。请检查密钥是否有效，创建新密钥后重新启动。") from None
    except RateLimitError:
        raise SystemExit("API 请求过于频繁或免费额度已满（429）。请到服务商控制台查看 Limits。") from None
    except APIConnectionError:
        raise SystemExit("电脑无法连接 API。请检查网络后重试。") from None
    except OpenAIError as error:
        status = getattr(error, "status_code", "未知")
        raise SystemExit(f"API 验证失败（HTTP {status}，{type(error).__name__}）。请检查密钥权限和账户状态。") from None
    if key.startswith("gsk_"):
        available = {item.id for item in getattr(model_list, "data", model_list)}
        TRANSCRIBE_MODEL = os.getenv("DEFENSE_TRANSCRIBE_MODEL", pick_first(GROQ_TRANSCRIBE_MODELS, available))
        ANSWER_MODEL = os.getenv("DEFENSE_ANSWER_MODEL", pick_first(GROQ_ANSWER_MODELS, available))
        missing = [model for model in (TRANSCRIBE_MODEL, ANSWER_MODEL) if model not in available]
        if missing:
            raise SystemExit(f"Groq 密钥有效，但账户模型列表里没有：{', '.join(missing)}。请检查模型权限。")
    provider = "Groq" if key.startswith("gsk_") else "OpenAI"
    print(f"已启用 {provider}：转写 {TRANSCRIBE_MODEL}，回答 {ANSWER_MODEL}", flush=True)
    print("API 密钥验证通过，准备就绪。", flush=True)
    save_status(f"{provider} 密钥已验证；正在检查回答接口。")


def is_lan_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in network for network in PRIVATE_NETWORKS)
    except ValueError:
        return False


def lan_ips() -> list[str]:
    found: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            if is_lan_ip(address):
                found.add(address)
    except OSError:
        pass
    # UDP connect does not send data; it identifies the current LAN interface.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            address = sock.getsockname()[0]
            if is_lan_ip(address):
                found.add(address)
    except OSError:
        pass
    return sorted(found)


def write_private_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def ensure_certificates(addresses: list[str]) -> tuple[Path, Path, Path]:
    LOCAL.mkdir(exist_ok=True)
    ca_key_path = LOCAL / "ca.key"
    ca_path = LOCAL / "ca.cer"
    cert_path = LOCAL / "server.pem"
    key_path = LOCAL / "server.key"
    addresses_path = LOCAL / "addresses.json"
    certificate_record = {"version": 2, "addresses": addresses}
    if all(path.exists() for path in (ca_key_path, ca_path, cert_path, key_path, addresses_path)):
        if json.loads(addresses_path.read_text(encoding="utf-8")) == certificate_record:
            existing = x509.load_pem_x509_certificate(cert_path.read_bytes())
            if existing.not_valid_after_utc > datetime.now(timezone.utc) + timedelta(days=7):
                return ca_path, cert_path, key_path

    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Defense Assistant Local CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=730))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Defense Assistant LAN")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip)) for ip in addresses]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    write_private_key(ca_key_path, ca_key)
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.DER))
    write_private_key(key_path, server_key)
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    addresses_path.write_text(json.dumps(certificate_record), encoding="utf-8")
    return ca_path, cert_path, key_path


SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
        "question": {"type": "boolean"},
        "question_zh": {"type": "string"},
        "answer_ko": {"type": "string"},
        "answer_zh": {"type": "string"},
    },
    "required": ["valid", "question", "question_zh", "answer_ko", "answer_zh"],
    "additionalProperties": False,
}


def context_text(korean: str = "") -> str:
    context_path = BASE / "defense_context.txt"
    qa_path = BASE / "defense_qa.txt"
    context = context_path.read_text(encoding="utf-8").strip() if context_path.exists() else ""
    qa = qa_path.read_text(encoding="utf-8").strip() if qa_path.exists() else ""
    material = context[:5000]
    if qa:
        material += "\n\n参考问答：\n" + qa[:3000]
    return material.strip() or "尚未提供论文材料。只能回答一般研究方法，不得声称已有检验结果。"



def parse_json_content(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text or "{}")


def call_llm_json(instructions: str, user_input: str) -> dict:
    if hasattr(CLIENT, "chat") and hasattr(CLIENT.chat, "completions"):
        is_groq = "groq.com" in str(getattr(CLIENT, "base_url", ""))
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_input},
        ]
        schema_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "defense_answer",
                "strict": True,
                "schema": SCHEMA,
            },
        }
        options = {"reasoning_effort": "low"} if is_groq and ANSWER_MODEL.startswith("openai/gpt-oss-") else {}
        try:
            response = CLIENT.chat.completions.create(
                model=ANSWER_MODEL,
                messages=messages,
                response_format=schema_format,
                max_tokens=800,
                temperature=0.2,
                **options,
            )
        except BadRequestError:
            if not is_groq:
                raise
            fallback = (
                f"{instructions}\n\n"
                "只输出 JSON 对象，不要 markdown。字段："
                '{"valid":true或false,"question":true或false,"question_zh":"...","answer_ko":"...","answer_zh":"..."}'
            )
            response = CLIENT.chat.completions.create(
                model=ANSWER_MODEL,
                messages=[
                    {"role": "system", "content": fallback},
                    {"role": "user", "content": user_input},
                ],
                response_format={"type": "json_object"},
                max_tokens=800,
                temperature=0.2,
                **options,
            )
        content = response.choices[0].message.content or "{}"
        return parse_json_content(content)
    elif hasattr(CLIENT, "responses"):
        response = CLIENT.responses.create(
            model=ANSWER_MODEL,
            instructions=instructions,
            input=user_input,
            text={"format": {"type": "json_schema", "name": "defense_answer", "strict": True, "schema": SCHEMA}},
            max_output_tokens=400,
        )
        return json.loads(response.output_text)
    raise RuntimeError("OpenAI client does not support chat or responses API")


def call_llm_translate(text: str) -> str:
    prompt = "将收到的韩语语音识别片段忠实翻译为简短中文。片段可能未说完，只翻译可确定的内容。只输出译文，不回答问题。"
    if hasattr(CLIENT, "chat") and hasattr(CLIENT.chat, "completions"):
        is_groq_oss = "groq.com" in str(getattr(CLIENT, "base_url", "")) and ANSWER_MODEL.startswith("openai/gpt-oss-")
        response = CLIENT.chat.completions.create(
            model=ANSWER_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=256 if is_groq_oss else 120,
            temperature=0.2,
            **({"reasoning_effort": "low"} if is_groq_oss else {}),
        )
        return (response.choices[0].message.content or "").strip()
    elif hasattr(CLIENT, "responses"):
        response = CLIENT.responses.create(
            model=ANSWER_MODEL,
            instructions=prompt,
            input=text,
            max_output_tokens=120,
        )
        return response.output_text.strip()
    return ""


def clear_korean(text: str) -> bool:
    """Distinguish readable Korean speech from empty or noisy ASR output."""
    hangul = sum("가" <= char <= "힣" for char in text)
    return hangul >= 3


def looks_like_question(text: str) -> bool:
    if "?" in text or "？" in text:
        return True
    return bool(re.search(r"(?:습니까|십니까|나요|까요|인가요|는가|니)\s*[.!。]*$", text.strip()))


def obvious_topic_error(text: str) -> bool:
    """Reject clear ASR substitutions from an unrelated research field."""
    return False  # The friend may study any academic field.


def unsafe_answer(answer_ko: str, answer_zh: str) -> bool:
    if len(answer_ko) > 105 or len(answer_zh) > 140:
        return True
    return False


def four_line_answer(korean: str, chinese: str, answer_ko: str = "无", answer_zh: str = "无") -> dict:
    return {"output": (
        f"【提问韩文】：{korean}\n"
        f"【提问中文】：{chinese}\n"
        f"【回复韩文】：{answer_ko}\n"
        f"【回复中文】：{answer_zh}"
    )}


def make_answer(korean: str, partial: bool) -> dict:
    if not clear_korean(korean):
        return {"translation": ""} if partial else {"output": "识别失败"}
    if partial:
        translation = call_llm_translate(korean)
        return {"translation": translation}
    if obvious_topic_error(korean):
        return {"output": "识别失败"}

    instructions = (
        "你是韩语学术答辩的静音文字助手。输入是韩语语音识别文本。"
        "研究主题与事实以用户填写的研究背景为准；没有材料时只给一般方法思路。"
        "valid只表示韩文是否可读：清楚的问句、陈述、问候和点评都必须valid=true。"
        "不得仅因不是问句或与论文无关就设valid=false。question只表示是否在提问。"
        "问句必须忠实翻译为中文；生成一句适合口头说的韩语学术正式합쇼体回答，"
        "再忠实翻译该回答成中文。非问句只翻译陈述，question=false，回答两栏留空。"
        "韩语回答只用一句，尽量控制在70个韩文字符以内，最多105个字符；中文回答尽量短，最多140字。"
        "用容易读的常见韩语词，不堆术语、数字、英文缩写；必要的专业术语只保留一个。"
        "没有材料支持时请直接说需要进一步核查，不要声称检验已完成。"
        "如果问句的关键术语明显转写错误或与用户材料冲突，"
        "valid=false，不得把它解释成另一项研究并编造实验、设备、数据或结果。"
        "普通问候和陈述仍需翻译。听不清关键内容时valid=false；不要猜测。"
        "绝不编造研究数据、样本、系数、显著性、引文、因果结论或已完成的检验。"
        "缺少证据时承认边界并说明拟补充的检验。答案简短。\n研究背景：\n" + context_text(korean)
    )
    data = call_llm_json(instructions, korean)

    question_hint = looks_like_question(korean) or bool(data.get("question"))
    if data.get("valid") and question_hint and (
        not data.get("question") or not data.get("question_zh") or
        not data.get("answer_ko") or not data.get("answer_zh")
    ):
        retry = instructions + "\n韩文是可读的问句；请补齐中文问题和简短双语回答。"
        data = call_llm_json(retry, korean)
    elif not data.get("valid") and clear_korean(korean) and not question_hint:
        chinese = call_llm_translate(korean)
        if chinese:
            return four_line_answer(korean, chinese)
    if not data.get("valid"):
        return {"output": "识别失败"}
    question_zh = str(data.get("question_zh", "")).strip()
    is_question = bool(data.get("question", False))
    answer_ko = str(data.get("answer_ko", "")).strip() if is_question else "无"
    answer_zh = str(data.get("answer_zh", "")).strip() if is_question else "无"
    if not question_zh and clear_korean(korean) and not is_question and not looks_like_question(korean):
        question_zh = call_llm_translate(korean)
    if not question_zh or (is_question and (not answer_ko or not answer_zh)):
        return {"output": "识别失败"}
    if is_question and unsafe_answer(answer_ko, answer_zh):
        return {"output": "识别失败"}
    return four_line_answer(korean, question_zh, answer_ko, answer_zh)


class Handler(BaseHTTPRequestHandler):
    server_version = "DefenseAssistant/1.0"

    def log_message(self, format: str, *args) -> None:
        # Avoid logging the session token and academic speech.
        pass

    def send_bytes(self, status: int, data: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, obj: dict, extra: dict | None = None) -> None:
        self.send_bytes(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def authorized(self) -> bool:
        # 1. 优先校验前端 Header (防 Safari Cookie 策略拦截)
        token_hdr = self.headers.get("X-Defense-Token", "").strip()
        if token_hdr and secrets.compare_digest(token_hdr, TOKEN):
            return True
        # 2. 校验 Cookie
        cookie_hdr = self.headers.get("Cookie", "")
        if cookie_hdr:
            try:
                c = SimpleCookie()
                c.load(cookie_hdr)
                if COOKIE_NAME in c and secrets.compare_digest(c[COOKIE_NAME].value, TOKEN):
                    return True
            except Exception:
                pass
            if f"{COOKIE_NAME}={TOKEN}" in cookie_hdr:
                return True
        return False

    def do_GET(self) -> None:
        route = urlsplit(self.path)
        if getattr(self.server, "setup_only", False):
            if route.path == "/ca.cer":
                self.send_bytes(200, (LOCAL / "ca.cer").read_bytes(), "application/x-x509-ca-cert")
            else:
                self.send_bytes(404, b"Not found", "text/plain")
            return
        query_token = parse_qs(route.query).get("token", [""])[0]
        if (route.path == "/" or route.path == "/index.html") and query_token and secrets.compare_digest(query_token, TOKEN):
            save_status("手机页面已请求；等待收音或文字测试。")
            self.send_bytes(
                200,
                (STATIC / "index.html").read_bytes(),
                "text/html; charset=utf-8",
                {"Set-Cookie": f"{COOKIE_NAME}={TOKEN}; HttpOnly; Secure; SameSite=Lax; Path=/"},
            )
            return
        if not self.authorized():
            self.send_bytes(403, b"Access denied", "text/plain")
            return
        path = "/index.html" if route.path == "/" else route.path
        allowed = {
            "/index.html": "text/html; charset=utf-8",
            "/app.js": "application/javascript; charset=utf-8",
            "/style.css": "text/css; charset=utf-8",
        }
        if path not in allowed:
            self.send_bytes(404, b"Not found", "text/plain")
            return
        self.send_bytes(200, (STATIC / path[1:]).read_bytes(), allowed[path])

    def do_POST(self) -> None:
        if getattr(self.server, "setup_only", False) or not self.authorized():
            save_status("手机请求被拒绝（403）；请重新扫描当前二维码。")
            self.send_json(403, {"error": "访问未授权或口令已失效，请重新扫描电脑窗口中的二维码"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 5_000_000:
            self.send_json(413, {"error": "请求太大或为空"})
            return
        body = self.rfile.read(length)
        try:
            if self.path == "/api/transcribe":
                if "audio/wav" not in self.headers.get("Content-Type", ""):
                    self.send_json(415, {"error": "只接受 WAV 音频"})
                    return
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as file:
                    file.write(body)
                    temp_path = Path(file.name)
                try:
                    with temp_path.open("rb") as audio:
                        kwargs = {"model": TRANSCRIBE_MODEL, "file": audio, "language": "ko"}
                        if "whisper" in TRANSCRIBE_MODEL.lower():
                            kwargs["prompt"] = "한국어 학술 발표와 질의응답"
                        result = CLIENT.audio.transcriptions.create(**kwargs)
                    save_status("Groq/OpenAI 语音转写接口已返回；等待回答。")
                    self.send_json(200, {"korean": result.text.strip()})
                finally:
                    temp_path.unlink(missing_ok=True)
            elif self.path == "/api/answer":
                data = json.loads(body.decode("utf-8", errors="replace"))
                korean = str(data.get("korean", "")).strip()[:3000]
                if not korean:
                    self.send_json(200, {"output": "识别失败"})
                    return
                answer = make_answer(korean, bool(data.get("partial", False)))
                save_status("韩语文字回答接口已返回。")
                self.send_json(200, answer)
            else:
                self.send_json(404, {"error": "接口不存在"})
        except AuthenticationError:
            print("API authentication failed (401).", file=sys.stderr, flush=True)
            save_status("手机请求失败：API 密钥无效（401）。")
            self.send_json(401, {"error": "API 密钥失效（401）。请关闭电脑窗口，输入服务商新建的完整 Secret Key 后重启。"})
        except RateLimitError as error:
            print("API rate limit reached (429).", file=sys.stderr, flush=True)
            save_status("手机请求失败：API 速率或额度限制（429）。")
            retry_after = getattr(getattr(error, "response", None), "headers", {}).get("retry-after", "")
            self.send_json(
                429,
                {"error": "Groq 已限流（429）。已暂停收音，请按提示稍后重试；具体额度可查看 Groq Console 的 Limits。",
                 "retry_after": retry_after},
                {"Retry-After": retry_after} if retry_after else None,
            )
        except APIConnectionError:
            print("API connection failed.", file=sys.stderr, flush=True)
            save_status("手机请求失败：电脑连接 API 失败。")
            self.send_json(502, {"error": "电脑无法连接语音或回答服务，请检查电脑网络。"})
        except Exception as error:
            print(f"API error: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            save_status(f"手机请求失败：{type(error).__name__}（HTTP {getattr(error, 'status_code', '未知')}）。")
            self.send_json(502, {"error": f"API 处理失败（{type(error).__name__}）。请查看电脑窗口的具体错误。"})


def show_phone_qr(url: str, open_window: bool = True) -> Path:
    LOCAL.mkdir(exist_ok=True)
    image_path = LOCAL / f"phone-qr-{TOKEN[:8]}.png"
    qrcode.make(url, box_size=12, border=4).save(image_path)
    if open_window and hasattr(os, "startfile"):
        try:
            os.startfile(str(image_path))
        except OSError as error:
            print(f"无法自动打开二维码图片：{error}", file=sys.stderr, flush=True)
    return image_path


def print_tunnel_url(process: subprocess.Popen[str]) -> None:
    try:
        for line in process.stdout or []:
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if match:
                url = f"{match.group(0)}/?token={TOKEN}"
                print("\n" + "=" * 60, flush=True)
                print(f"手机 Safari 地址：{url}", flush=True)
                image_path = show_phone_qr(url)
                print(f"二维码已打开：{image_path}", flush=True)
                print("用 iPhone 相机扫码，在 Safari 打开后点开始收音。", flush=True)
                print("=" * 60 + "\n", flush=True)
                save_status("手机二维码已生成；等待手机访问和收音。")
            elif "ERR" in line or "error" in line.lower():
                print(f"cloudflared: {line.strip()}", file=sys.stderr, flush=True)
        if process.poll() is not None:
            save_status(f"手机隧道程序已退出（代码 {process.returncode}）；请重新启动。")
    except Exception as error:
        print(f"读取隧道地址失败：{error}", file=sys.stderr, flush=True)
        save_status(f"隧道地址读取失败：{type(error).__name__}")


def check_answer_api() -> None:
    if "groq.com" not in str(getattr(CLIENT, "base_url", "")):
        return
    print("正在检查 Groq 韩语文字回答接口，请稍候…", flush=True)
    try:
        data = call_llm_json(
            "请按规定的 JSON 结构回答，valid 和 question 为布尔值，另外三个字段为字符串。"
            "这是接口连通性检查，请给出极短的翻译和一句韩语回答。",
            "이 연구의 핵심 질문은 무엇입니까?",
        )
    except AuthenticationError:
        raise SystemExit("Groq 密钥能读取模型列表，但回答接口拒绝了密钥（401）。") from None
    except RateLimitError:
        raise SystemExit("Groq 回答接口达到额度或速率限制（429）。请查看 Groq Console 的 Limits。") from None
    except OpenAIError as error:
        status = getattr(error, "status_code", "未知")
        raise SystemExit(f"Groq 回答接口测试失败（HTTP {status}，{type(error).__name__}）。") from None
    except Exception as error:
        raise SystemExit(f"Groq 回答接口测试失败（{type(error).__name__}）。") from None
    if (
        not isinstance(data, dict) or set(data) != set(SCHEMA["required"])
        or type(data["valid"]) is not bool or type(data["question"]) is not bool
        or any(type(data[key]) is not str for key in ("question_zh", "answer_ko", "answer_zh"))
    ):
        raise SystemExit("Groq 回答接口可调用，但返回结构不完整。请检查回答模型设置。")
    print("Groq 韩语文字回答接口检查通过。", flush=True)
    save_status("Groq 密钥和韩语文字回答接口通过；正在建立手机入口。")


def main() -> None:
    save_status("启动中：正在检查密钥。")
    try:
        validate_api_key()
        check_answer_api()
    except SystemExit as error:
        save_status(f"启动失败：{error}")
        raise
    addresses = lan_ips()
    tunnel_mode = os.getenv("DEFENSE_TUNNEL") == "1"
    bind_ip = "127.0.0.1" if tunnel_mode or not addresses else addresses[0]
    ca_path, cert_path, key_path = ensure_certificates(sorted({bind_ip, "127.0.0.1"}))
    setup = None
    if not tunnel_mode and bind_ip != "127.0.0.1":
        setup = ThreadingHTTPServer((bind_ip, SETUP_PORT), Handler)
        setup.setup_only = True
        Thread(target=setup.serve_forever, daemon=True).start()
    try:
        server = ThreadingHTTPServer((bind_ip, PORT), Handler)
    except OSError as error:
        if getattr(error, "winerror", None) == 10048 or getattr(error, "errno", None) in (48, 98, 10048):
            message = f"电脑端口 {PORT} 已被占用。请关闭旧的“韩语答辩助手”窗口，再双击桌面快捷方式。"
            save_status(f"启动失败：{message}")
            raise SystemExit(message) from None
        raise
    server.setup_only = False
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    tunnel = None
    if tunnel_mode:
        cloudflared = os.getenv("DEFENSE_CLOUDFLARED_PATH", "cloudflared")
        command = [cloudflared, "tunnel", "--url", f"https://127.0.0.1:{PORT}", "--no-tls-verify"]
        allowed_mail = os.getenv("DEFENSE_ALLOWED_MAIL", "").strip()
        if allowed_mail:
            command.extend(["--allowed-mail", allowed_mail])
        tunnel = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        Thread(target=print_tunnel_url, args=(tunnel,), daemon=True).start()
        print("\n正在建立手机 HTTPS 临时入口；保持此窗口开着。", flush=True)
    elif bind_ip != "127.0.0.1":
        print("\n手机与电脑连接同一局域网，保持电脑和此窗口开着。")
        print(f"先在 iPhone Safari 安装并完全信任本地证书：http://{bind_ip}:{SETUP_PORT}/ca.cer")
        print(f"然后打开：https://{bind_ip}:{PORT}/?token={TOKEN}")
    else:
        print("\n没有检测到可供手机直连的局域网地址。可安装 cloudflared 后运行 start.ps1 -Tunnel。")
        print(f"仅供电脑本机测试：https://127.0.0.1:{PORT}/?token={TOKEN}")
    print(f"\n转写模型：{TRANSCRIBE_MODEL}；回答模型：{ANSWER_MODEL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if setup:
            setup.shutdown()
        if tunnel:
            tunnel.terminate()


if __name__ == "__main__":
    main()
