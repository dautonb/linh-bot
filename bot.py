# -*- coding: utf-8 -*-
"""
bot.py - Linh Voice Bot chạy trên GitHub Actions (Long Polling).

Bản VOICE (Dots): Dots3-Note Preview (qua OpenRouter) viết câu trả lời -> HeyGen Voice Turbo (cũng qua OpenRouter)
đọc thành giọng nói -> đổi PCM sang ogg/opus bằng ffmpeg -> gửi vào Telegram bằng sendVoice.
Cả LLM lẫn giọng nói dùng chung MỘT khóa OPENROUTER_API_KEY.
LLM lỗi thì tự chuyển sang model free dự phòng khác trên OpenRouter; TTS hoặc gửi voice lỗi thì tự gửi chữ,
bot không bao giờ im lặng.

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
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")  # Một khóa OpenRouter dùng cho cả LLM (Dots3-Note) lẫn TTS (HeyGen)
STATE_REPO = env("STATE_REPO")                  # Dạng "ten-github/linh-bot-data" (repo RIÊNG TƯ)
STATE_REPO_TOKEN = env("STATE_REPO_TOKEN")      # Token quyền Contents: Read and write cho repo đó

RUN_SECONDS = int(env("RUN_SECONDS", "20400"))  # Thời gian chạy mỗi lượt (5h40m < giới hạn 6h)
TZ_OFFSET_HOURS = float(env("TZ_OFFSET_HOURS", "7"))  # Múi giờ để tính giờ yên lặng (VN = +7)

# --- LLM (qua OpenRouter) ---
# Model chính + danh sách model free dự phòng (ngăn cách bằng dấu phẩy, thử lần lượt từ trái sang phải).
# Model free hay bị gỡ/đổi mà không báo trước -> nếu một cái bị 404 thì sửa biến LLM_FALLBACK_MODELS trong bot.yml,
# danh sách hiện có xem ở https://openrouter.ai/collections/free-models . Đặt "-" để tắt hẳn dự phòng.
# "openrouter/free" là bộ định tuyến tự chọn ngẫu nhiên một model free còn sống -> để cuối làm phao cứu sinh.
DEFAULT_LLM_MODEL = "dots-studio/dots-3-note-preview:free"
DEFAULT_LLM_FALLBACKS = (
    "thinkingmachines/inkling:free,"
    "thinkingmachines/inkling-small:free,"
    "nvidia/nemotron-3-ultra-550b-a55b:free,"
    "openrouter/free"
)
LLM_MODEL = env("LLM_MODEL", DEFAULT_LLM_MODEL)
_fallbacks = env("LLM_FALLBACK_MODELS", DEFAULT_LLM_FALLBACKS)
LLM_FALLBACK_MODELS = [] if _fallbacks == "-" else [m.strip() for m in _fallbacks.split(",") if m.strip()]
LLM_CHAIN = list(dict.fromkeys([LLM_MODEL] + LLM_FALLBACK_MODELS))   # Thứ tự thử, bỏ trùng
LLM_REASONING = env("LLM_REASONING", "1") == "1"     # "1" = bật chế độ suy luận (reasoning) như mẫu OpenRouter; "0" = tắt cho nhanh
LLM_MAX_TOKENS = int(env("LLM_MAX_TOKENS", "3000"))  # Token suy luận cũng tính vào đây nên để rộng hơn chat thường
LLM_TIMEOUT = int(env("LLM_TIMEOUT", "60"))          # Giây chờ phản hồi tối đa cho MỖI model
LLM_TOTAL_BUDGET = int(env("LLM_TOTAL_BUDGET", "150"))   # Tổng giây tối đa cho cả chuỗi model trước khi bỏ cuộc lượt này

# --- Giọng nói (TTS) ---
# Bản ":free" sẽ bị OpenRouter gỡ ngày 30/11/2026 -> sau đó đổi TTS_MODEL thành "heygen/heygen-voice-1-turbo" (trả phí).
TTS_MODEL = env("TTS_MODEL", "heygen/heygen-voice-1-turbo:free")
# 10 giọng có sẵn: nadine, tabitha, bram, gareth, noelle, meredith, sabrina, lucian, weston, vincent
TTS_VOICE = env("TTS_VOICE", "sabrina")
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
DEFAULT_PROACTIVE_INSTRUCTION = (
    "[Tình huống: đã khoảng {hours} giờ anh không nhắn tin cho em. "
    "Hãy nhắn trước một tin ngắn, ngọt ngào, nói em nhớ anh.]"
)
_voice_suffix = env("VOICE_PROMPT_SUFFIX", DEFAULT_VOICE_SUFFIX)
SYSTEM_PROMPT = env("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
if _voice_suffix and _voice_suffix != "-":
    SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + _voice_suffix
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
MAX_STORED_MESSAGES = 60     # Chỉ cần lưu hơn 14 tin một chút -> mỗi lần lưu rất nhẹ
POLL_TIMEOUT = 20            # Long Polling: Telegram giữ kết nối tối đa 20s nếu chưa có tin mới
RETRY_DELAY = 5              # Giây chờ khi lỗi mạng / lỗi API trước khi thử lại
MAX_RETRY_PER_UPDATE = 3     # Một tin nhắn lỗi tối đa 3 lần thì bỏ qua

# --- Tính năng Linh chủ động nhắn trước khi chủ nhân im lặng ---
PROACTIVE_ENABLED = True
PROACTIVE_MIN_IDLE = 2 * 3600
PROACTIVE_MAX_IDLE = 3 * 3600
MAX_UNANSWERED_PROACTIVE = 2     # Nhắn trước tối đa 2 tin liên tiếp nếu chưa được trả lời
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
# LLM: Dots3-Note Preview qua OpenRouter (lỗi thì tự chuyển sang model free dự phòng)
# ============================================================
# Các mẫu nhận diện câu trả lời "vô nghĩa" do model sinh lỗi
CJK_PATTERN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")  # chữ Trung/Nhật/Hàn
REPEAT_CHAR_PATTERN = re.compile(r"(.)\1{14,}")   # 1 ký tự lặp liên tiếp từ 15 lần
LEAK_MARKERS = ("[Nhắc Linh", "[Tình huống", "[BỐI CẢNH", "[Lưu ý giọng nói")   # model lỡ đọc lại chỉ dẫn nội bộ
VI_CHARS = set("àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ")
THINK_PATTERN = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)   # <think>...</think>
THINK_CLOSE_PATTERN = re.compile(r"</think(?:ing)?>", re.I)
THINK_OPEN_PATTERN = re.compile(r"<think(?:ing)?>.*$", re.S | re.I)
# Gợi ý dễ hiểu cho các mã lỗi hay gặp khi gọi LLM (hiện trong log Actions để bạn biết sửa gì)
LLM_HINTS = {
    400: "yêu cầu bị từ chối - model không nhận một tham số nào đó",
    401: "sai hoặc thiếu OPENROUTER_API_KEY",
    402: "số dư credit OpenRouter đang âm (model :free cũng báo 402 khi âm) -> nạp thêm credit",
    404: "model đã bị gỡ hoặc không có endpoint - nếu thông báo nhắc 'data policy' thì bật free endpoints "
         "ở openrouter.ai/settings/privacy",
    429: "chạm giới hạn của model :free (20 lượt/phút; 50 lượt/ngày, hoặc 1000 nếu từng nạp >= 10 USD credit; "
         "tính chung cho cả tài khoản, gồm LLM lẫn giọng nói)",
}
LLM_DOWN_NOTICE = "Em đang hơi mệt một chút, lát nữa anh nhắn lại giúp em nhé 💋"


class LLMError(Exception):
    """
    Một model lỗi. Mặc định -> đổi sang model dự phòng kế tiếp.
    fatal=True  : lỗi cấp tài khoản (sai key, âm credit, hết lượt free trong ngày) -> đổi model cũng vô ích.
    block       : số giây tạm ngưng gọi LLM hoàn toàn (khi hết lượt free trong ngày).
    cooldown    : số giây không thử lại riêng model này (vd model đã bị gỡ, đang bị giới hạn tốc độ).
    """

    def __init__(self, message, fatal=False, block=0, cooldown=0):
        super().__init__(message)
        self.fatal = fatal
        self.block = block
        self.cooldown = cooldown


class LLMUnavailable(Exception):
    """Không thể gọi LLM nếu không có can thiệp của bạn (hết lượt free/ngày, sai key, âm credit)."""


llm_block = {"until": 0.0}   # Hết lượt free trong ngày -> tạm ngưng gọi API tới mốc này
model_cooldown = {}          # model -> thời điểm được thử lại (tránh mỗi tin đều phí lượt vào model đang hỏng)


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


def strip_think(text):
    """Model suy luận đôi khi để cả phần suy nghĩ trong content (<think>...</think>) -> bỏ đi, chỉ giữ lời nói."""
    text = THINK_PATTERN.sub("", text)
    text = THINK_CLOSE_PATTERN.split(text)[-1]    # còn thẻ đóng lẻ -> lấy phần đứng sau nó
    text = THINK_OPEN_PATTERN.sub("", text)       # thẻ mở bị cắt dở -> bỏ phần đuôi
    return text.strip()


def openrouter_headers():
    """Header dùng chung cho cả LLM và TTS (cùng một khóa OPENROUTER_API_KEY)."""
    headers = {
        "Authorization": "Bearer {}".format(OPENROUTER_API_KEY),
        "Content-Type": "application/json",
        "X-OpenRouter-Title": OPENROUTER_TITLE,
    }
    if OPENROUTER_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_REFERER
    return headers


def error_reason(resp):
    """Lấy thông báo lỗi từ phản hồi HTTP của OpenRouter (thông tin hệ thống, không chứa nội dung chat)."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:150]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or "")[:150]
        return str(error or data.get("message") or "")[:150]
    return ""


def llm_error_from_http(status, reason):
    """Phân loại lỗi HTTP: đổi model dự phòng được, hay là lỗi cấp tài khoản?"""
    low = reason.lower()
    if status in (401, 402):
        return LLMError("HTTP {}".format(status), fatal=True)
    if status == 429 and ("per-day" in low or "per day" in low or "daily" in low):
        # Hạn mức free tính CHUNG cả tài khoản, đổi sang model :free khác cũng vẫn hết -> ngưng 1 giờ rồi thử lại
        return LLMError("HTTP 429 (hết lượt free trong ngày)", fatal=True, block=3600)
    if status == 429:
        return LLMError("HTTP 429", cooldown=120)
    if status == 404:
        return LLMError("HTTP 404", cooldown=3600)      # model bị gỡ -> đừng thử lại liên tục
    if status in (400, 403):
        return LLMError("HTTP {}".format(status))        # có thể chỉ do một tin cụ thể -> không phạt model
    return LLMError("HTTP {}".format(status), cooldown=60)   # 5xx, quá tải...


def request_llm(model, messages, temperature, read_timeout):
    """Gọi MỘT model qua OpenRouter. Trả về câu trả lời đã kiểm tra, hoặc ném LLMError."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": LLM_MAX_TOKENS,   # token suy luận cũng tính vào đây -> để rộng hơn chat thường
        "stream": False,
    }
    if LLM_REASONING:
        # enabled: bật chế độ suy luận. exclude: vẫn suy luận nhưng KHÔNG trả phần suy nghĩ về
        # (bot chỉ cần lời nói, lịch sử lưu dạng chữ thường nên không cần giữ reasoning_details giữa các lượt)
        payload["reasoning"] = {"enabled": True, "exclude": True}

    try:
        resp = session.post(LLM_URL, headers=openrouter_headers(), json=payload, timeout=(5, read_timeout))
    except requests.RequestException as e:
        raise LLMError("lỗi mạng/timeout ({})".format(type(e).__name__), cooldown=60)

    if resp.status_code >= 400:
        reason = error_reason(resp)
        logging.error(
            "OpenRouter LLM HTTP %s (%s): %s%s",
            resp.status_code, model, reason,
            " -> " + LLM_HINTS[resp.status_code] if resp.status_code in LLM_HINTS else "",
        )
        raise llm_error_from_http(resp.status_code, reason)

    try:
        data = resp.json()
    except ValueError:
        raise LLMError("phản hồi không phải JSON", cooldown=60)

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        # OpenRouter đôi khi trả HTTP 200 nhưng nội dung là {"error": {...}} khi nhà cung cấp phía sau lỗi
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            code = error.get("code")
            reason = str(error.get("message") or "")[:150]
            logging.error("OpenRouter LLM lỗi trong thân phản hồi (%s, mã %s): %s", model, code, reason)
            if isinstance(code, int) and code >= 400:
                raise llm_error_from_http(code, reason)
        raise LLMError("phản hồi không có câu trả lời", cooldown=60)

    choice = choices[0]
    content = (choice.get("message") or {}).get("content")
    content = strip_think(content if isinstance(content, str) else "")

    # Bị cắt vì hết token -> bỏ câu dở dang ở cuối
    if choice.get("finish_reason") == "length":
        content = trim_incomplete(content)

    problem = find_problem(content)
    if problem:
        # Log công khai: chỉ ghi lý do và độ dài, KHÔNG ghi nội dung
        logging.warning("Loại câu trả lời lỗi của %s (%s), dài %s ký tự", model, problem, len(content))
        raise LLMError("trả lời lỗi: {}".format(problem))
    return content


def call_llm(messages):
    """
    Thử lần lượt Dots3-Note rồi tới các model free dự phòng cho đến khi có câu trả lời tốt.
    - Model lỗi (HTTP lỗi, timeout, trả lời vô nghĩa/rỗng) -> chuyển sang model kế tiếp.
    - Lỗi cấp tài khoản (sai key, âm credit, hết lượt free/ngày) -> ném LLMUnavailable, không đổi model nữa.
    - Tất cả model đều lỗi -> ném ValueError: KHÔNG gửi, KHÔNG lưu bộ nhớ, vòng lặp chính sẽ thử lại tin này.
    """
    started = time.time()
    if started < llm_block["until"]:
        raise LLMUnavailable("đang tạm ngưng gọi LLM vì hết lượt free trong ngày")

    # Bỏ qua model đang "nghỉ" (vừa bị gỡ/giới hạn tốc độ); nếu tất cả đều đang nghỉ thì thử lại hết
    ready = [m for m in LLM_CHAIN if model_cooldown.get(m, 0) <= started]
    candidates = ready or list(LLM_CHAIN)

    last_error = "không rõ"
    for index, model in enumerate(candidates):
        remaining = LLM_TOTAL_BUDGET - (time.time() - started)
        if index > 0 and remaining <= 10:
            logging.warning("Hết ngân sách thời gian gọi LLM (%ss), dừng chuyển model", LLM_TOTAL_BUDGET)
            break
        read_timeout = max(10, min(LLM_TIMEOUT, remaining))
        # Model đầu nhiệt độ cao cho tự nhiên; model dự phòng hạ xuống cho ổn định
        temperature = 1.0 if index == 0 else 0.7
        try:
            text = request_llm(model, messages, temperature, read_timeout)
        except LLMError as e:
            last_error = "{} ({})".format(e, model)
            if e.fatal:
                if e.block:
                    llm_block["until"] = time.time() + e.block
                raise LLMUnavailable(last_error)
            if e.cooldown:
                model_cooldown[model] = time.time() + e.cooldown
            logging.warning("Model %s lỗi: %s -> thử model kế tiếp", model, e)
            continue

        if model != LLM_CHAIN[0]:
            logging.info("Đã trả lời bằng model dự phòng: %s", model)
        return text

    raise ValueError("Tất cả model LLM đều lỗi, lỗi cuối: {}".format(last_error))


def build_context(history):
    """Ghép system prompt + tối đa 14 tin gần nhất làm ngữ cảnh."""
    window = history[-MEMORY_WINDOW:]
    # Bỏ các tin assistant nằm đầu cửa sổ để ngữ cảnh luôn bắt đầu bằng lượt của user
    while window and window[0]["role"] != "user":
        window = window[1:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(window)
    return messages


def with_reminder(text):
    """Gắn lời nhắc phong cách (nếu có) vào cuối nội dung gửi API; lịch sử vẫn lưu bản gốc."""
    return (text + "\n\n" + STYLE_REMINDER) if STYLE_REMINDER else text


def ask_llm(user_text, history):
    """Trả lời tin nhắn mới của chủ nhân."""
    messages = build_context(history)
    messages.append({"role": "user", "content": with_reminder(user_text)})
    return call_llm(messages)


def generate_proactive(history, idle_hours):
    """Tạo tin nhắn Linh tự gửi trước, dựa trên bối cảnh trò chuyện gần đây."""
    messages = build_context(history)
    instruction = PROACTIVE_INSTRUCTION.replace("{hours}", str(idle_hours))
    messages.append({"role": "user", "content": with_reminder(instruction)})
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
    429: "chạm giới hạn của model :free (20 lượt/phút; 50 lượt/ngày, hoặc 1000 nếu từng nạp >= 10 USD credit; "
         "tính chung cả LLM lẫn giọng nói, mỗi câu trả lời tốn 2 lượt)",
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
    payload = {
        "model": TTS_MODEL,
        "input": text,
        "voice": TTS_VOICE,
        # HeyGen Voice chỉ hỗ trợ "pcm" (gửi "mp3" sẽ bị lỗi 400: only supports response_format "pcm")
        "response_format": "pcm",
    }
    resp = session.post(TTS_URL, headers=openrouter_headers(), json=payload, timeout=(5, 60))
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
    except LLMUnavailable as e:
        # Hết lượt free/ngày, sai key, âm credit: thử lại ngay cũng vô ích -> hẹn lại sau 1 giờ
        logging.error("Chưa nhắn trước được, LLM không dùng được: %s", e)
        timer["next_at"] = time.time() + 3600
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
    try:
        reply = ask_llm(text, history)             # Gọi AI (lỗi tạm thời sẽ ném exception ra ngoài để thử lại)
    except LLMUnavailable as e:
        # Lỗi cấp tài khoản (hết lượt free/ngày, sai key, âm credit): thử lại 3 lần cũng chỉ lặp lại lỗi,
        # nên báo nhẹ một câu rồi bỏ qua tin này
        logging.error("LLM không dùng được: %s", e)
        send_message(chat_id, LLM_DOWN_NOTICE)
        return True
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
    logging.info("LLM: %s | reasoning: %s", " -> ".join(LLM_CHAIN), "bật" if LLM_REASONING else "tắt")
    logging.info("TTS: %s, giọng '%s'", TTS_MODEL, TTS_VOICE)

    if not startup_sync():
        return 1

    # getUpdates không dùng chung được với Webhook, nên xóa webhook cũ (nếu có)
    for attempt in range(5):
        try:
            tg_call("deleteWebhook", {"drop_pending_updates": False})
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
