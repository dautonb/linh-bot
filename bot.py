# -*- coding: utf-8 -*-
"""
bot.py - Linh Voice Bot chạy trên GitHub Actions (Long Polling).

Bản MYTHOMAX + VOICE: MythoMax 13B (gryphe/mythomax-l2-13b, qua OpenRouter) viết câu trả lời ->
HeyGen Voice Turbo (qua OpenRouter, giọng meredith) đọc thành giọng nói
-> đổi PCM sang ogg/opus bằng ffmpeg -> gửi vào Telegram bằng sendVoice.
TTS hoặc gửi voice lỗi thì tự gửi chữ, bot không bao giờ im lặng.

Khác bản chạy trên laptop:
- Mọi khóa bí mật và nội dung nhân vật đọc từ GitHub Secrets (repo public không lộ gì).
- Bộ nhớ (lịch sử chat + mốc thời gian) lưu vào một repo RIÊNG TƯ khác qua GitHub API,
  vì máy chạy Actions bị xóa sạch sau mỗi lượt.
- Mỗi lượt chạy tối đa RUN_SECONDS (mặc định 5h40m) rồi tự thoát gọn gàng;
  workflow sẽ tự kích hoạt lượt kế tiếp để bot gần như chạy liên tục.
- Log công khai => tuyệt đối không ghi nội dung tin nhắn ra log.
"""

import base64
import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests


# ============================================================
# CẤU HÌNH - ĐỌC TỪ BIẾN MÔI TRƯỜNG (GitHub Secrets)
# ============================================================
def env(name, default=""):
    """Đọc biến môi trường; rỗng hoặc chưa đặt thì dùng giá trị mặc định."""
    return (os.environ.get(name) or default).strip()


TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
CHAT_ID = env("CHAT_ID")
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")  # Một khóa OpenRouter dùng cho cả MythoMax (chat) lẫn HeyGen (giọng nói)
STATE_REPO = env("STATE_REPO")                  # Dạng "ten-github/linh-bot-data" (repo RIÊNG TƯ)
STATE_REPO_TOKEN = env("STATE_REPO_TOKEN")      # Token quyền Contents: Read and write cho repo đó

RUN_SECONDS = int(env("RUN_SECONDS", "20400"))  # Thời gian chạy mỗi lượt (5h40m < giới hạn 6h)
TZ_OFFSET_HOURS = float(env("TZ_OFFSET_HOURS", "7"))  # Múi giờ để tính giờ yên lặng (VN = +7)

# --- Chat: MythoMax 13B (Gryphe) qua OpenRouter ---
# Model nhỏ 13B dựa trên Llama 2 (ra đời 07/2023), chuyên nhập vai. Trả phí theo token (rất rẻ), KHÔNG có bản free.
LLM_MODEL = env("LLM_MODEL", "gryphe/mythomax-l2-13b")
LLM_MAX_TOKENS = int(env("LLM_MAX_TOKENS", "500"))          # Tin nhắn voice ngắn nên 500 là dư
LLM_TEMPERATURE = float(env("LLM_TEMPERATURE", "0.85"))     # Model nhỏ để 1.0 dễ lan man; lần thử lại tự hạ thêm
# Ngữ cảnh của model chỉ 4K-8K token và tiếng Việt tốn token (ước ~2 ký tự/token) nên giới hạn TỔNG số ký tự gửi đi
# (system prompt + lịch sử). Persona dài thì tăng số này; gặp lỗi 400 vượt ngữ cảnh thì giảm xuống.
LLM_CONTEXT_CHARS = int(env("LLM_CONTEXT_CHARS", "6000"))
HISTORY_MIN_CHARS = 1500                                    # Dù persona dài đến đâu vẫn chừa chỗ cho ~3 tin gần nhất
# Model kiểu này hay tự viết tiếp lượt của bạn ("Anh: ...") hoặc rò rỉ mẫu prompt Llama 2 -> cắt ngay tại đó.
# Ngăn cách bằng "|", \n = xuống dòng, tối đa 4 mục (một số nhà cung cấp từ chối nhiều hơn). "-" = tắt.
LLM_STOP = env("LLM_STOP", "</s>|[INST]|\\nUser:|\\nAnh:")
# "deny" = chỉ dùng nhà cung cấp KHÔNG lưu dữ liệu của bạn (khuyên dùng cho chat riêng tư).
# Nếu log báo 404 kiểu "không có nhà cung cấp khớp chính sách dữ liệu" thì đặt "allow". "-" = không gửi tùy chọn này.
LLM_DATA_COLLECTION = env("LLM_DATA_COLLECTION", "deny").lower()

# --- Giọng nói (TTS) ---
# Bản ":free" sẽ bị OpenRouter gỡ ngày 30/11/2026 -> sau đó đổi TTS_MODEL thành "heygen/heygen-voice-1-turbo" (trả phí).
TTS_MODEL = env("TTS_MODEL", "heygen/heygen-voice-1-turbo:free")
# 10 giọng có sẵn: nadine, tabitha, bram, gareth, noelle, meredith, sabrina, lucian, weston, vincent
TTS_VOICE = env("TTS_VOICE", "meredith")
SEND_TEXT_ALSO = env("SEND_TEXT_ALSO", "0") == "1"   # "1" = gửi thêm bản chữ ngay sau voice
OPENROUTER_TITLE = env("OPENROUTER_TITLE", "Linh Voice Bot")   # Tên hiển thị trên openrouter.ai (tùy chọn)
OPENROUTER_REFERER = env("OPENROUTER_REFERER", "")             # URL site cho bảng xếp hạng (tùy chọn, có thể để trống)

# Nội dung nhân vật nằm trong Secrets. Giá trị mặc định dưới đây cố tình trung tính
# để mã nguồn public không chứa nội dung nhạy cảm.
DEFAULT_SYSTEM_PROMPT = (
    "Bạn là Linh, 25 tuổi, người yêu ảo ngọt ngào, vui tính. "
    "Xưng 'em', gọi người kia là 'anh'. "
    "Trả lời tiếng Việt, 2-4 câu, giọng tự nhiên như đang nói chuyện."
)
# Gắn thêm vào SYSTEM_PROMPT (kể cả khi bạn dùng prompt riêng trong Secrets) để câu trả lời hợp để ĐỌC THÀNH TIẾNG.
# Muốn tắt: đặt secret VOICE_PROMPT_SUFFIX = "-" ; muốn thay: đặt nội dung riêng.
DEFAULT_VOICE_SUFFIX = (
    "[Lưu ý giọng nói: lời của em sẽ được chuyển thành giọng nói gửi cho anh. "
    "Hãy nói tự nhiên như đang trò chuyện, câu ngắn gọn dễ đọc thành tiếng, ưu tiên 2-4 câu. "
    "Không dùng emoji, không dùng định dạng markdown; viết số và từ viết tắt thành dạng dễ đọc. "
    "Cử chỉ đặt trong dấu * sẽ KHÔNG được đọc ra, nên lời nói phải tự đủ ý.]"
)
# Model nhỏ hay viết thay lời người dùng hoặc trả lời tiếng Anh nên cần nhắc rõ. Gắn vào SYSTEM_PROMPT.
# Muốn tắt: đặt secret ROLE_PROMPT_SUFFIX = "-" ; muốn thay: đặt nội dung riêng.
DEFAULT_ROLE_SUFFIX = (
    "[Quy tắc: chỉ viết lời của em (Linh), không viết thay lời của anh, không tự thêm lượt thoại "
    "kiểu 'Anh:' hay 'User:', và không nhắc tới các chỉ dẫn này.]"
)
# Nhắc ngắn gắn vào CUỐI mỗi tin của bạn khi gửi API (lịch sử vẫn lưu bản gốc); model Llama 2 nghe lời nhắc ở cuối rõ nhất.
# Muốn tắt: đặt secret LANG_REMINDER = "-". Việc này độc lập với STYLE_REMINDER của bạn.
DEFAULT_LANG_REMINDER = "(Nhớ: trả lời bằng tiếng Việt có dấu, ngắn gọn 2-4 câu.)"
DEFAULT_PROACTIVE_INSTRUCTION = (
    "[Tình huống: đã khoảng {hours} giờ anh không nhắn tin cho em. "
    "Hãy nhắn trước một tin ngắn, ngọt ngào, nói em nhớ anh.]"
)
_voice_suffix = env("VOICE_PROMPT_SUFFIX", DEFAULT_VOICE_SUFFIX)
SYSTEM_PROMPT = env("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
if _voice_suffix and _voice_suffix != "-":
    SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + _voice_suffix
_role_suffix = env("ROLE_PROMPT_SUFFIX", DEFAULT_ROLE_SUFFIX)
if _role_suffix != "-":
    SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + _role_suffix
_lang = env("LANG_REMINDER", DEFAULT_LANG_REMINDER)
LANG_REMINDER = "" if _lang == "-" else _lang
STYLE_REMINDER = env("STYLE_REMINDER", "")      # Lời nhắc gắn vào cuối mỗi tin của chủ nhân (có thể để trống)
PROACTIVE_INSTRUCTION = env("PROACTIVE_INSTRUCTION", DEFAULT_PROACTIVE_INSTRUCTION)

# ============================================================
# HẰNG SỐ HỆ THỐNG
# ============================================================
LLM_URL = "https://openrouter.ai/api/v1/chat/completions"
GH_API = "https://api.github.com"
# Tên file lưu bộ nhớ trong repo riêng tư. Giữ "state.json" = dùng tiếp bộ nhớ của bot cũ;
# đặt tên khác (vd state_voice.json) nếu muốn bản voice có bộ nhớ riêng.
STATE_FILE = env("STATE_FILE", "state.json")

TTS_URL = "https://openrouter.ai/api/v1/audio/speech"
TTS_MAX_CHARS = 4800         # HeyGen Voice cho tối đa 5.000 ký tự mỗi lần gọi
TTS_ATTEMPTS = 2             # Số lần thử gọi TTS trước khi chuyển sang gửi chữ
TTS_RETRY_DELAY = 3          # Giây chờ giữa 2 lần thử
# HeyGen Voice CHỈ hỗ trợ response_format "pcm": 16-bit little-endian, mono, theo trang model là 44,1 kHz.
# Nếu nghe giọng nhanh/chậm/méo thì đổi biến TTS_PCM_RATE (thử 24000, 22050...). Header Content-Type có "rate=" thì ưu tiên theo header.
PCM_SAMPLE_RATE = int(env("TTS_PCM_RATE", "44100"))

MEMORY_WINDOW = 14           # Số tin gần nhất gửi kèm làm ngữ cảnh mỗi lần gọi API
MAX_STORED_MESSAGES = 6000     # Chỉ cần lưu hơn 14 tin một chút -> mỗi lần lưu rất nhẹ
POLL_TIMEOUT = 20            # Long Polling: Telegram giữ kết nối tối đa 20s nếu chưa có tin mới
RETRY_DELAY = 5              # Giây chờ khi lỗi mạng / lỗi API trước khi thử lại
MAX_RETRY_PER_UPDATE = 3     # Một tin nhắn lỗi tối đa 3 lần thì bỏ qua

# --- Tính năng Linh chủ động nhắn trước khi chủ nhân im lặng ---
PROACTIVE_ENABLED = True
PROACTIVE_MIN_IDLE = 1 * 3600
PROACTIVE_MAX_IDLE = 1 * 3600
MAX_UNANSWERED_PROACTIVE = 2000     # Nhắn trước tối đa 2 tin liên tiếp nếu chưa được trả lời
QUIET_START_HOUR = 0             # Không nhắn trước từ 0h...
QUIET_END_HOUR = 7               # ...đến 7h sáng (theo TZ_OFFSET_HOURS)
PROACTIVE_RETRY_DELAY = 60


# ============================================================
# LOGGING (in ra console của Actions; log của repo public ai cũng xem được)
# ============================================================
class RedactFormatter(logging.Formatter):
    """Che token/API key trong log (kể cả traceback) phòng khi lộ qua thông báo lỗi."""

    def format(self, record):
        text = super().format(record)
        for secret in (TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, STATE_REPO_TOKEN):
            if secret:
                text = text.replace(secret, "***")
        return text


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


session = requests.Session()  # Dùng lại kết nối -> nhanh và nhẹ


# ============================================================
# BỘ NHỚ: lưu trong repo riêng tư qua GitHub Contents API
# ============================================================
# store = toàn bộ trạng thái cần giữ giữa các lượt chạy
store = {
    "history": [],       # Danh sách {"role": "user"/"assistant", "content": "..."}
    "last_user": 0.0,    # Lần cuối chủ nhân nhắn
    "last_event": 0.0,   # Lần cuối có tương tác (chủ nhân nhắn HOẶC Linh nhắn trước)
    "unanswered": 0,     # Số tin nhắn trước liên tiếp chưa được trả lời
}
remote = {"sha": None, "dirty": False}  # sha của file trên repo; dirty = có thay đổi chưa lưu được


def gh_headers():
    return {
        "Authorization": "Bearer {}".format(STATE_REPO_TOKEN),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def state_url():
    return "{}/repos/{}/contents/{}".format(GH_API, STATE_REPO, STATE_FILE)


def pull_state():
    """Tải state.json từ repo riêng tư. Trả về dict, hoặc None nếu file chưa tồn tại."""
    resp = session.get(state_url(), headers=gh_headers(), timeout=(5, 20))
    if resp.status_code == 404:
        remote["sha"] = None
        return None
    resp.raise_for_status()
    info = resp.json()
    remote["sha"] = info["sha"]
    return json.loads(base64.b64decode(info["content"]).decode("utf-8"))


def refresh_sha():
    """Lấy lại sha mới nhất của file (dùng khi PUT bị báo lệch phiên bản)."""
    resp = session.get(state_url(), headers=gh_headers(), timeout=(5, 20))
    remote["sha"] = resp.json().get("sha") if resp.status_code == 200 else None


def push_state():
    """
    Ghi store lên repo riêng tư (mỗi lần = 1 commit nhỏ). Trả về True nếu thành công.
    Lỗi chỉ ghi log và đánh dấu dirty để vòng lặp tự thử lưu lại, không làm sập bot.
    """
    text = json.dumps(store, ensure_ascii=False, indent=1)
    body = {
        "message": "state",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    for attempt in range(2):
        if remote["sha"]:
            body["sha"] = remote["sha"]
        try:
            resp = session.put(state_url(), headers=gh_headers(), json=body, timeout=(5, 30))
            if resp.status_code in (409, 422) and attempt == 0:
                refresh_sha()   # sha lệch -> lấy sha mới rồi thử lại 1 lần
                continue
            resp.raise_for_status()
            remote["sha"] = resp.json()["content"]["sha"]
            remote["dirty"] = False
            return True
        except Exception as e:
            logging.error("Không lưu được bộ nhớ lên repo: %s", e)
            break
    remote["dirty"] = True
    return False


def load_store():
    """Nạp bộ nhớ từ repo riêng tư vào store (chưa có file thì giữ giá trị mặc định)."""
    data = pull_state()
    if not isinstance(data, dict):
        return
    history = data.get("history", [])
    if isinstance(history, list):
        store["history"] = [
            m for m in history
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
        ]
    for key, cast in (("last_user", float), ("last_event", float), ("unanswered", int)):
        try:
            store[key] = cast(data.get(key, store[key]))
        except (TypeError, ValueError):
            pass


def load_history():
    return list(store["history"])


def save_history(history):
    """Cập nhật lịch sử trong store rồi đẩy lên repo riêng tư."""
    store["history"] = history[-MAX_STORED_MESSAGES:]
    push_state()


def startup_sync():
    """Kiểm tra repo dữ liệu + tải bộ nhớ. Lỗi mạng thử tối đa 3 lần; sai cấu hình thì dừng ngay."""
    repo_url = "{}/repos/{}".format(GH_API, STATE_REPO)
    for attempt in range(3):
        try:
            resp = session.get(repo_url, headers=gh_headers(), timeout=(5, 20))
            if resp.status_code in (401, 403, 404):
                logging.error(
                    "Không truy cập được repo dữ liệu (HTTP %s). Kiểm tra secret STATE_REPO và STATE_REPO_TOKEN.",
                    resp.status_code,
                )
                return False
            resp.raise_for_status()
            load_store()
            return True
        except Exception as e:
            logging.error("Lỗi khi tải bộ nhớ (lần %s): %s", attempt + 1, e)
            time.sleep(RETRY_DELAY)
    return False


# ============================================================
# TELEGRAM API
# ============================================================
def tg_call(method, payload=None, timeout=10):
    """Gọi một phương thức Telegram Bot API, ném lỗi nếu HTTP lỗi hoặc ok=false."""
    url = "https://api.telegram.org/bot{}/{}".format(TELEGRAM_BOT_TOKEN, method)
    resp = session.post(url, json=payload or {}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram trả về lỗi: {}".format(data))
    return data["result"]


def get_updates(offset):
    """Long Polling: Telegram giữ kết nối tới 20s, có tin mới là trả về ngay lập tức."""
    payload = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset  # Xác nhận đã nhận các update trước đó
    return tg_call("getUpdates", payload, timeout=(5, POLL_TIMEOUT + 10))


def send_message(chat_id, text):
    # Không dùng parse_mode để dấu * trong cử chỉ không làm lỗi định dạng
    tg_call("sendMessage", {"chat_id": chat_id, "text": text[:4000]})


def send_voice(chat_id, audio, kind):
    """
    Gửi tin nhắn thoại (hiện dạng bong bóng voice có sóng âm). sendVoice nhận ogg/opus (đường chính của bot) hoặc mp3.
    File được upload dạng multipart nên không dùng tg_call (chỉ gửi JSON).
    """
    mime, ext = ("audio/mpeg", "mp3") if kind == "mp3" else ("audio/ogg", "ogg")
    url = "https://api.telegram.org/bot{}/sendVoice".format(TELEGRAM_BOT_TOKEN)
    resp = session.post(
        url,
        data={"chat_id": chat_id},
        files={"voice": ("linh.{}".format(ext), audio, mime)},
        timeout=(5, 60),
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram từ chối sendVoice: {}".format(data))


def send_action(chat_id, action):
    """Hiện trạng thái 'đang nhập...' / 'đang ghi âm...'. Lỗi ở đây không quan trọng nên bỏ qua."""
    try:
        tg_call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=5)
    except Exception:
        pass


def send_typing(chat_id):
    send_action(chat_id, "typing")


# ============================================================
# LLM: MYTHOMAX 13B QUA OPENROUTER
# ============================================================
# Các mẫu nhận diện câu trả lời "vô nghĩa" do model sinh lỗi
CJK_PATTERN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")  # chữ Trung/Nhật/Hàn
REPEAT_CHAR_PATTERN = re.compile(r"(.)\1{14,}")   # 1 ký tự lặp liên tiếp từ 15 lần
# Model lỡ đọc lại chỉ dẫn nội bộ, hoặc rò rỉ mẫu prompt Llama 2 / Alpaca
LEAK_MARKERS = ("[Nhắc Linh", "[Tình huống", "[BỐI CẢNH", "[Lưu ý giọng nói", "[Quy tắc",
                "[INST]", "[/INST]", "<<SYS>>", "</s>", "### Instruction", "### Response")
SPEAKER_LABEL_PATTERN = re.compile(r"^\s*(?:Linh|Assistant)\s*:\s*", re.IGNORECASE)   # Model hay mở đầu bằng "Linh: ..."
VI_CHARS = set("àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ")


def find_problem(text):
    """Kiểm tra nhanh một câu trả lời có bị lỗi/vô nghĩa không. Trả về lý do, hoặc None nếu ổn."""
    if len(text) < 15:
        return "quá ngắn/rỗng"
    if CJK_PATTERN.search(text):
        return "lẫn chữ Trung/Nhật/Hàn"
    if any(marker in text for marker in LEAK_MARKERS):
        return "lộ chỉ dẫn nội bộ"
    if REPEAT_CHAR_PATTERN.search(text):
        return "lặp ký tự"
    if len(text) >= 60 and not any(ch in VI_CHARS for ch in text.lower()):
        return "không phải tiếng Việt"

    words = text.lower().split()
    if len(words) >= 30:
        if len(set(words)) / len(words) < 0.35:
            return "lặp từ"
        # Cùng một cụm 4 từ xuất hiện từ 4 lần trở lên = model đang kẹt vòng lặp
        grams = Counter(zip(words, words[1:], words[2:], words[3:]))
        if grams.most_common(1)[0][1] >= 4:
            return "lặp cụm từ"
    return None


def trim_incomplete(text):
    """Nếu câu trả lời bị cắt vì hết token, bỏ phần câu dở dang ở cuối để không đứt giữa chừng."""
    cut = max(text.rfind(ch) for ch in ".!?…")
    if cut > len(text) * 0.4:
        text = text[:cut + 1]
    # Dấu * lẻ nghĩa là cử chỉ bị bỏ dở -> cắt bỏ phần cử chỉ đó
    if text.count("*") % 2 == 1:
        text = text[:text.rfind("*")].rstrip()
    return text


LLM_HINTS = {
    400: "yêu cầu bị từ chối - có thể vượt độ dài ngữ cảnh của model, hãy giảm LLM_CONTEXT_CHARS",
    401: "sai hoặc thiếu OPENROUTER_API_KEY",
    402: "OpenRouter hết credit - MythoMax là model trả phí, hãy nạp credit ở openrouter.ai/credits",
    404: "không có nhà cung cấp nào khớp LLM_DATA_COLLECTION=deny (đặt 'allow' nếu chấp nhận), hoặc sai tên LLM_MODEL",
    429: "chạm giới hạn tốc độ, chờ một lúc rồi thử lại",
}


def openrouter_headers():
    """Header chung cho mọi lời gọi OpenRouter (chat lẫn giọng nói)."""
    headers = {
        "Authorization": "Bearer {}".format(OPENROUTER_API_KEY),
        "Content-Type": "application/json",
        "X-OpenRouter-Title": OPENROUTER_TITLE,
    }
    if OPENROUTER_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_REFERER
    return headers


def api_error_reason(resp):
    """Lấy thông báo lỗi từ phản hồi lỗi của OpenRouter (dạng {"error": {"message": ...}})."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            error = data.get("error")
            return (error.get("message") if isinstance(error, dict) else error) or data.get("message") or ""
    except ValueError:
        pass
    return ""


def check_llm_response(resp):
    """Nếu OpenRouter trả lỗi HTTP thì ghi rõ lý do + gợi ý cách sửa (không ghi nội dung chat) rồi ném lỗi."""
    if resp.status_code < 400:
        return
    logging.error(
        "OpenRouter LLM HTTP %s: %s%s",
        resp.status_code,
        str(api_error_reason(resp) or resp.text)[:200],
        " -> " + LLM_HINTS[resp.status_code] if resp.status_code in LLM_HINTS else "",
    )
    resp.raise_for_status()


def message_text(message):
    """Lấy phần chữ của câu trả lời (content có thể là chuỗi hoặc danh sách các phần)."""
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def parse_stop(value):
    """Đổi chuỗi cấu hình LLM_STOP thành danh sách stop sequence (\\n = xuống dòng, "-" hoặc rỗng = không dùng)."""
    if not value or value == "-":
        return []
    return [part.replace("\\n", "\n") for part in value.split("|") if part][:4]


def call_llm(messages):
    """
    Gửi messages tới MythoMax qua OpenRouter. Câu trả lời lỗi/vô nghĩa (lẫn tiếng Anh, rò rỉ prompt, lặp từ...)
    bị loại và thử lại 1 lần với nhiệt độ thấp hơn. Lỗi HTTP/timeout ném exception cho vòng lặp chính xử lý.
    """
    last_problem = "không rõ"
    for attempt in range(2):
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": LLM_TEMPERATURE if attempt == 0 else max(0.3, LLM_TEMPERATURE - 0.25),
            "max_tokens": LLM_MAX_TOKENS,
            "stream": False,
        }
        stop = parse_stop(LLM_STOP)
        if stop:
            payload["stop"] = stop
        if LLM_DATA_COLLECTION in ("allow", "deny"):
            payload["provider"] = {"data_collection": LLM_DATA_COLLECTION}

        resp = session.post(LLM_URL, headers=openrouter_headers(), json=payload, timeout=(5, 90))
        check_llm_response(resp)
        data = resp.json()

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            # OpenRouter đôi khi trả HTTP 200 nhưng nội dung là lỗi của nhà cung cấp
            error = data.get("error") if isinstance(data, dict) else None
            logging.error("OpenRouter LLM không trả về câu trả lời: %s",
                          str((error.get("message") if isinstance(error, dict) else error) or "không rõ")[:200])
            raise ValueError("LLM không trả về câu trả lời")

        choice = choices[0]
        text = SPEAKER_LABEL_PATTERN.sub("", message_text(choice.get("message") or {}).strip(), count=1)
        # Bị cắt vì hết token -> bỏ câu dở dang ở cuối
        if choice.get("finish_reason") == "length":
            text = trim_incomplete(text)

        problem = find_problem(text)
        if problem is None:
            return text

        # Log công khai: chỉ ghi lý do và độ dài, KHÔNG ghi nội dung
        last_problem = problem
        logging.warning("Loại câu trả lời lỗi (%s), lần %s, dài %s ký tự", problem, attempt + 1, len(text))

    # Lỗi cả 2 lần -> ném exception: KHÔNG gửi, KHÔNG lưu vào bộ nhớ, vòng lặp chính sẽ thử lại
    raise ValueError("LLM trả lời lỗi 2 lần liền: {}".format(last_problem))


def build_context(history, extra_chars=0):
    """
    Ghép system prompt + các tin gần nhất làm ngữ cảnh: tối đa MEMORY_WINDOW tin VÀ tổng ký tự không vượt
    LLM_CONTEXT_CHARS (model chỉ có ngữ cảnh ngắn). extra_chars = độ dài tin mới sắp thêm vào.
    """
    budget = max(LLM_CONTEXT_CHARS - len(SYSTEM_PROMPT) - extra_chars, HISTORY_MIN_CHARS)
    kept, used = [], 0
    for message in reversed(history[-MEMORY_WINDOW:]):
        used += len(message["content"])
        # Luôn giữ lượt trao đổi gần nhất (2 tin) dù dài, nếu không Linh sẽ mất sạch trí nhớ ngay lúc đang trò chuyện
        if used > budget and len(kept) >= 2:
            break
        kept.append(message)
    kept.reverse()
    # Bỏ các tin assistant nằm đầu cửa sổ để ngữ cảnh luôn bắt đầu bằng lượt của user
    while kept and kept[0]["role"] != "user":
        kept = kept[1:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(kept)
    return messages


def with_reminder(text):
    """Gắn lời nhắc (STYLE_REMINDER của bạn + nhắc tiếng Việt) vào cuối nội dung gửi API; lịch sử vẫn lưu bản gốc."""
    extras = [reminder for reminder in (STYLE_REMINDER, LANG_REMINDER) if reminder]
    return (text + "\n\n" + "\n".join(extras)) if extras else text


def ask_llm(user_text, history):
    """Trả lời tin nhắn mới của chủ nhân."""
    new_text = with_reminder(user_text)
    messages = build_context(history, len(new_text))
    messages.append({"role": "user", "content": new_text})
    return call_llm(messages)


def generate_proactive(history, idle_hours):
    """Tạo tin nhắn Linh tự gửi trước, dựa trên bối cảnh trò chuyện gần đây."""
    instruction = with_reminder(PROACTIVE_INSTRUCTION.replace("{hours}", str(idle_hours)))
    messages = build_context(history, len(instruction))
    messages.append({"role": "user", "content": instruction})
    return call_llm(messages)


# ============================================================
# GIỌNG NÓI: HeyGen Voice Turbo qua OpenRouter -> Telegram sendVoice
# ============================================================
GESTURE_PATTERN = re.compile(r"\*[^*]*\*")   # *cử chỉ* - không đọc thành tiếng
EMOJI_PATTERN = re.compile(
    "[\U0001F000-\U0001FAFF\u2190-\u21FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d\u20e3]+"
)
# Gợi ý dễ hiểu cho các mã lỗi thường gặp (hiện trong log Actions để bạn biết sửa gì)
TTS_HINTS = {
    400: "tham số bị từ chối - xem thông báo ngay trước (vd voice không có trong 10 giọng, hoặc response_format khác pcm)",
    401: "sai hoặc thiếu OPENROUTER_API_KEY",
    402: "OpenRouter hết credit (model trả phí cần nạp tiền)",
    404: "model/voice không tồn tại - nếu đã qua 30/11/2026 thì bản :free đã bị gỡ, hãy đổi TTS_MODEL",
    429: "chạm giới hạn tốc độ của OpenRouter (bản free dễ bị)",
}


def clean_for_speech(text):
    """
    Làm sạch chữ trước khi đọc: bỏ *cử chỉ*, emoji, ký hiệu markdown. Trả về "" nếu không còn gì để đọc.
    Lịch sử trò chuyện vẫn lưu bản gốc (còn cử chỉ) nên văn phong của Linh không bị mất.
    """
    text = unicodedata.normalize("NFC", text)   # Gộp dấu tiếng Việt về dạng chuẩn
    text = GESTURE_PATTERN.sub(" ", text)
    text = text.replace("*", " ")                # Dấu * lẻ còn sót
    text = EMOJI_PATTERN.sub(" ", text)
    text = re.sub(r"[_`#>~]+", " ", text)        # Ký hiệu markdown và dấu ~ (vd "anh yêu~")
    text = re.sub(r"\s+", " ", text).strip()
    if not re.search(r"\w", text):
        return ""
    return text[:TTS_MAX_CHARS]


def detect_audio(data):
    """
    Phòng khi sau này API trả sẵn mp3/ogg: nhận ra qua dấu hiệu đầu file ('ID3' hoặc 'OggS').
    Ngoài ra coi là PCM thô (đường chính hiện nay của HeyGen).
    """
    if data[:3] == b"ID3":
        return "mp3"
    if data[:4] == b"OggS":
        return "ogg"
    return None


def pcm_to_ogg(pcm, rate):
    """
    Đổi PCM thô (16-bit little-endian, mono) sang ogg/opus bằng ffmpeg - định dạng Telegram phát được
    dưới dạng tin nhắn thoại. ffmpeg là BẮT BUỘC (workflow đã có bước cài).
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("máy không có ffmpeg để đổi PCM sang ogg/opus")
    if len(pcm) % 2:
        pcm = pcm[:-1]   # PCM 16-bit phải chẵn byte
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1",
         "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
         "-ar", "48000", "-f", "ogg", "pipe:1"],
        input=pcm, capture_output=True, timeout=60,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg lỗi: {}".format(proc.stderr.decode("utf-8", "ignore")[:150]))
    return proc.stdout


def check_tts_response(resp):
    """Nếu OpenRouter trả lỗi HTTP thì ghi rõ lý do + gợi ý cách sửa (không ghi nội dung chat) rồi ném lỗi."""
    if resp.status_code < 400:
        return
    reason = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            error = data.get("error")
            reason = (error.get("message") if isinstance(error, dict) else error) or data.get("message") or ""
    except ValueError:
        pass
    logging.error(
        "OpenRouter TTS HTTP %s: %s%s",
        resp.status_code,
        str(reason)[:150],
        " -> " + TTS_HINTS[resp.status_code] if resp.status_code in TTS_HINTS else "",
    )
    resp.raise_for_status()


def synthesize(text):
    """Gọi HeyGen Voice Turbo qua OpenRouter (PCM) rồi đổi sang ogg/opus. Trả về (bytes âm thanh, 'ogg')."""
    headers = openrouter_headers()

    payload = {
        "model": TTS_MODEL,
        "input": text,
        "voice": TTS_VOICE,
        # HeyGen Voice chỉ hỗ trợ "pcm" (gửi "mp3" sẽ bị lỗi 400: only supports response_format "pcm")
        "response_format": "pcm",
    }
    resp = session.post(TTS_URL, headers=headers, json=payload, timeout=(5, 60))
    check_tts_response(resp)

    if "json" in resp.headers.get("Content-Type", "").lower():
        raise ValueError("OpenRouter trả JSON thay vì âm thanh")
    audio = resp.content
    if len(audio) < 1000:
        raise ValueError("Âm thanh rỗng hoặc quá ngắn ({} byte)".format(len(audio)))

    kind = detect_audio(audio)
    if kind:                      # Đã là mp3/ogg sẵn -> gửi thẳng
        return audio, kind

    rate = PCM_SAMPLE_RATE
    match = re.search(r"rate=(\d{4,6})", resp.headers.get("Content-Type", ""))
    if match:                     # Header nói rõ tần số mẫu thì tin header
        rate = int(match.group(1))
    return pcm_to_ogg(audio, rate), "ogg"


def text_to_voice(text):
    """synthesize() có thử lại. Hết lượt thử vẫn lỗi thì ném lỗi cuối cùng."""
    last_error = None
    for attempt in range(TTS_ATTEMPTS):
        try:
            return synthesize(text)
        except Exception as e:
            last_error = e
            logging.warning("TTS lỗi lần %s/%s: %s", attempt + 1, TTS_ATTEMPTS, e)
            if attempt + 1 < TTS_ATTEMPTS:
                time.sleep(TTS_RETRY_DELAY)
    raise last_error


def deliver(chat_id, text):
    """
    Gửi câu trả lời của Linh dưới dạng VOICE. Bất cứ bước nào của voice (TTS, tải lên Telegram) lỗi
    thì tự gửi bản chữ để bot không bao giờ im lặng. Chỉ khi cả bản chữ cũng lỗi mới ném exception
    (để vòng lặp chính thử lại tin này).
    """
    spoken = clean_for_speech(text)
    if spoken:
        try:
            send_action(chat_id, "record_voice")
            audio, kind = text_to_voice(spoken)
            send_voice(chat_id, audio, kind)
        except Exception as e:
            logging.error("Không gửi được voice (%s) -> gửi bản chữ thay thế", e)
        else:
            if SEND_TEXT_ALSO:
                try:
                    send_message(chat_id, text)
                except Exception as e:
                    # Voice đã tới nơi; ném lỗi ở đây sẽ làm gửi lại voice lần nữa nên chỉ ghi log
                    logging.error("Không gửi được bản chữ kèm theo: %s", e)
            return
    send_message(chat_id, text)


# ============================================================
# NHẮN TRƯỚC CHỦ ĐỘNG (khi chủ nhân im lặng quá lâu)
# ============================================================
timer = {"next_at": 0.0}   # Thời điểm sớm nhất Linh được phép nhắn trước


def schedule_next(base):
    """Hẹn giờ nhắn trước kế tiếp: ngẫu nhiên 2-3 giờ tính từ mốc base."""
    timer["next_at"] = base + random.uniform(PROACTIVE_MIN_IDLE, PROACTIVE_MAX_IDLE)


def mark_user_activity():
    """Chủ nhân vừa nhắn: đặt lại đồng hồ im lặng và bộ đếm tin chưa được trả lời."""
    now = time.time()
    store["last_user"] = now
    store["last_event"] = now
    store["unanswered"] = 0
    schedule_next(now)


def local_hour():
    """Giờ hiện tại (0-23) theo múi giờ của bạn, vì máy chạy Actions dùng giờ UTC."""
    return datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS))).hour


def in_quiet_hours(hour):
    """True nếu giờ nằm trong khung yên lặng (hỗ trợ khung qua nửa đêm, vd 23 -> 7)."""
    if QUIET_START_HOUR == QUIET_END_HOUR:
        return False
    if QUIET_START_HOUR < QUIET_END_HOUR:
        return QUIET_START_HOUR <= hour < QUIET_END_HOUR
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def maybe_send_proactive():
    """Gọi sau mỗi lần quét tin. Đến giờ thì Linh tự nhắn trước cho chủ nhân."""
    if not PROACTIVE_ENABLED:
        return

    now = time.time()
    if now < timer["next_at"]:
        return  # Chưa đến giờ

    if in_quiet_hours(local_hour()):
        return  # Không làm phiền ban đêm; hết giờ yên lặng sẽ tự gửi

    # Đã nhắn trước nhiều lần mà chủ nhân chưa trả lời -> dừng hẳn, tránh spam
    # (bộ đếm được lưu giữa các lượt chạy nên khởi động lại không làm nó về 0)
    if store["unanswered"] >= MAX_UNANSWERED_PROACTIVE:
        return

    idle_hours = max(1, round((now - (store["last_user"] or store["last_event"] or now)) / 3600))
    try:
        history = load_history()
        text = generate_proactive(history, idle_hours)
        deliver(CHAT_ID, text)

        # Chỉ lưu tin của Linh (không lưu chỉ dẫn nội bộ)
        history.append({"role": "assistant", "content": text})
        store["unanswered"] += 1
        store["last_event"] = now
        schedule_next(now)
        save_history(history)
        logging.info("Đã nhắn trước (chưa trả lời: %s)", store["unanswered"])
    except Exception as e:
        # Lỗi mạng / API: thử lại sau 1 phút, không làm sập vòng lặp
        logging.error("Lỗi khi nhắn trước: %s", e)
        timer["next_at"] = time.time() + PROACTIVE_RETRY_DELAY


# ============================================================
# XỬ LÝ TỪNG TIN NHẮN
# ============================================================
def handle_update(update):
    """
    Trả về True  nếu đã xử lý xong (hoặc bỏ qua hợp lệ) -> được phép sang tin kế tiếp.
    Trả về False nếu gặp lỗi tạm thời -> vòng lặp chính sẽ đợi 5s rồi thử lại tin này.
    """
    message = update.get("message")
    if not message:
        return True

    chat_id = message.get("chat", {}).get("id")

    # BẢO MẬT: người lạ nhắn vào bot sẽ bị im lặng bỏ qua (không ghi định danh vào log công khai)
    if str(chat_id) != str(CHAT_ID):
        logging.warning("Bỏ qua tin nhắn từ người lạ.")
        return True

    # Chủ nhân vừa nhắn -> đặt lại đồng hồ "im lặng" của tính năng nhắn trước
    mark_user_activity()

    text = message.get("text")
    if not text:
        send_message(chat_id, "Em chỉ đọc được tin nhắn chữ thôi anh ơi 😘")
        return True

    command = text.strip().lower()

    # Lệnh /start: chào mở đầu
    if command.startswith("/start"):
        deliver(chat_id, "Anh yêu~ em chờ anh nãy giờ đó 😘 *nhào vào lòng anh* Hôm nay của anh thế nào?")
        push_state()
        return True

    # Lệnh /reset: xóa toàn bộ bộ nhớ trò chuyện
    if command.startswith("/reset"):
        save_history([])
        send_message(chat_id, "Em quên hết rồi nè, mình bắt đầu lại từ đầu nhé anh 💕")
        return True

    send_typing(chat_id)

    history = load_history()                       # Đọc bộ nhớ
    reply = ask_llm(text, history)                 # Gọi AI (lỗi sẽ ném exception ra ngoài)
    deliver(chat_id, reply)                        # Đọc thành giọng nói rồi gửi (lỗi thì gửi chữ)

    # Chỉ lưu vào bộ nhớ SAU KHI đã gửi thành công -> không bao giờ lưu nửa vời
    # (lưu bản chữ gốc, còn nguyên cử chỉ, chứ không phải bản đã lọc để đọc)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    save_history(history)
    return True


def notify_failure():
    """Báo nhẹ cho chủ nhân khi một tin nhắn lỗi liên tục và bị bỏ qua."""
    try:
        send_message(CHAT_ID, "Em bị chập chờn sóng một chút, anh nhắn lại giúp em nhé 💋")
    except Exception as e:
        logging.error("Không gửi được thông báo lỗi: %s", e)


# ============================================================
# VÒNG LẶP CHÍNH
# ============================================================
def shutdown(offset):
    """Dọn dẹp trước khi thoát: xác nhận tin đã xử lý với Telegram và lưu nốt bộ nhớ."""
    # Gọi getUpdates với offset để Telegram không gửi lại tin cuối cho lượt chạy kế tiếp
    if offset is not None:
        try:
            tg_call("getUpdates", {"offset": offset, "timeout": 0, "limit": 1, "allowed_updates": ["message"]})
        except Exception as e:
            logging.error("Không xác nhận được offset khi thoát: %s", e)
    if remote["dirty"]:
        push_state()


def main():
    missing = [
        name for name, value in (
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("CHAT_ID", CHAT_ID),
            ("OPENROUTER_API_KEY", OPENROUTER_API_KEY),
            ("STATE_REPO", STATE_REPO),
            ("STATE_REPO_TOKEN", STATE_REPO_TOKEN),
        ) if not value
    ]
    if missing:
        logging.error("Thiếu secret: %s", ", ".join(missing))
        return 1

    deadline = time.time() + RUN_SECONDS
    logging.info("=== Bot khởi động (chạy tối đa %s giây) ===", RUN_SECONDS)
    logging.info("LLM: %s (data_collection=%s) | TTS: %s, giọng '%s'",
                 LLM_MODEL, LLM_DATA_COLLECTION, TTS_MODEL, TTS_VOICE)

    if not startup_sync():
        return 1

    # getUpdates không dùng chung được với Webhook, nên xóa webhook cũ (nếu có)
    # Lần kết nối đầu tiên từ máy chủ GitHub tới Telegram đôi khi chậm -> timeout rộng hơn và thử nhiều lần hơn
    for attempt in range(8):
        try:
            tg_call("deleteWebhook", {"drop_pending_updates": False}, timeout=(5, 30))
            break
        except Exception as e:
            logging.error("Chưa xóa được webhook (lần %s): %s", attempt + 1, e)
            time.sleep(RETRY_DELAY)
    else:
        return 1

    # Tiếp tục đồng hồ nhắn trước từ mốc tương tác cuối (đã lưu từ lượt chạy trước)
    schedule_next(store["last_event"] or time.time())

    offset = None       # update_id kế tiếp cần lấy
    fail_count = 0      # Số lần liên tiếp tin hiện tại bị lỗi

    try:
        while time.time() < deadline:
            try:
                updates = get_updates(offset)  # Treo tối đa 20s, có tin là trả về ngay

                for update in updates:
                    try:
                        ok = handle_update(update)
                    except Exception as e:
                        logging.error("Lỗi khi xử lý tin nhắn: %s", e)
                        ok = False

                    if ok:
                        offset = update["update_id"] + 1   # Xác nhận đã xử lý xong tin này
                        fail_count = 0
                        continue

                    # Tin bị lỗi: giữ nguyên offset để lần quét sau lấy lại đúng tin này
                    fail_count += 1
                    if fail_count >= MAX_RETRY_PER_UPDATE:
                        offset = update["update_id"] + 1   # Quá số lần thử -> bỏ qua tin này
                        fail_count = 0
                        logging.error("Bỏ qua một tin nhắn sau %s lần lỗi", MAX_RETRY_PER_UPDATE)
                        notify_failure()
                    else:
                        time.sleep(RETRY_DELAY)            # Đợi 5 giây rồi thử lại
                    break

                # Sau mỗi lần quét: kiểm tra đã đến lúc Linh nhắn trước chưa
                maybe_send_proactive()

                # Nếu lần lưu bộ nhớ trước bị lỗi mạng thì thử lưu lại
                if remote["dirty"]:
                    push_state()

            except Exception as e:
                # Mất mạng, Telegram timeout, lỗi bất ngờ... -> đợi 5s rồi quét tiếp, không sập
                logging.error("Lỗi vòng lặp (%s), thử lại sau %ss", e, RETRY_DELAY)
                time.sleep(RETRY_DELAY)
    finally:
        shutdown(offset)

    logging.info("=== Hết thời gian lượt chạy, thoát để workflow nối lượt kế tiếp ===")
    return 0


def on_sigterm(signum, frame):
    """Actions gửi SIGTERM khi hủy job -> đổi thành KeyboardInterrupt để thoát gọn và lưu bộ nhớ."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    setup_logging()
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        code = main()
    except KeyboardInterrupt:
        logging.info("Bot dừng theo yêu cầu.")
        code = 0
    sys.exit(code)
