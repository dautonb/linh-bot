# -*- coding: utf-8 -*-
"""
bot.py - Linh Voice Bot (bản ERNIE + HeyGen) chạy trên GitHub Actions (Long Polling).

Bản VOICE: Baidu ERNIE-5.5-Flash-Preview viết câu trả lời -> HeyGen Voice Turbo đọc thành giọng nói (PCM)
-> đổi sang ogg/opus bằng ffmpeg -> gửi vào Telegram bằng sendVoice.
CẢ HAI model đều gọi qua OpenRouter nên chỉ cần MỘT khóa OPENROUTER_API_KEY.
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
import threading
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
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")  # MỘT khóa OpenRouter dùng cho cả chat (ERNIE) lẫn giọng nói (HeyGen)
STATE_REPO = env("STATE_REPO")                  # Dạng "ten-github/linh-bot-data" (repo RIÊNG TƯ)
STATE_REPO_TOKEN = env("STATE_REPO_TOKEN")      # Token quyền Contents: Read and write cho repo đó

RUN_SECONDS = int(env("RUN_SECONDS", "20400"))  # Thời gian chạy mỗi lượt (5h40m < giới hạn 6h)
TZ_OFFSET_HOURS = float(env("TZ_OFFSET_HOURS", "7"))  # Múi giờ để tính giờ yên lặng (VN = +7)

# --- Chat (LLM) ---
LLM_MODEL = env("LLM_MODEL", "baidu/ernie-5.5-flash-preview:free")
# "1" = gửi reasoning {"enabled": true} như mẫu của OpenRouter. Trang model KHÔNG nói ERNIE-5.5-Flash-Preview có reasoning hay không:
# nếu không hỗ trợ thì thường bị bỏ qua, còn nếu bị từ chối (400) bot tự bỏ tham số và nhớ cho các lượt sau.
# "0" = gửi effort "none" (tắt suy nghĩ); model không cho tắt thì cũng tự bỏ tham số như trên.
LLM_REASONING = env("LLM_REASONING", "1") == "1"
# "1" = lưu reasoning_details của vài câu trả lời gần nhất và gửi lại đúng nguyên bản ở lượt sau (như mẫu của OpenRouter).
KEEP_REASONING = env("KEEP_REASONING", "1") == "1"
KEEP_REASONING_TURNS = 2     # Chỉ giữ reasoning_details của 2 câu trả lời gần nhất -> file bộ nhớ không phình to
# Token suy nghĩ TÍNH CHUNG vào max_tokens. Đặt thấp quá thì model dùng hết token để nghĩ và không còn gì để trả lời.
LLM_MAX_TOKENS = int(env("LLM_MAX_TOKENS", "4096"))
LLM_READ_TIMEOUT = int(env("LLM_READ_TIMEOUT", "120"))   # Giây chờ phản hồi (ERNIE Flash nhanh, nhưng nếu có suy nghĩ thì để dư)
# Model dự phòng (cách nhau bằng dấu phẩy, thử theo thứ tự). Khi model chính lỗi, kể cả bị 429, OpenRouter tự chuyển sang
# model kế tiếp. Model không có đuôi ":free" sẽ bị tính tiền (cần credit) theo model thực sự được dùng. Để trống = không dự phòng.
# ERNIE-5.5-Flash-Preview chỉ có bản :free (không có bản trả phí cùng tên) nên bản workflow đặt sẵn là KHÔNG dự phòng.
# Muốn có dự phòng miễn phí, vd: "inclusionai/ling-3.0-flash:free".
LLM_FALLBACK_MODELS = [m.strip() for m in env("LLM_FALLBACK_MODELS").split(",") if m.strip()]

# --- Giọng nói (TTS) ---
# Theo ghi chú ở bản cũ, bản ":free" của HeyGen bị OpenRouter gỡ ngày 30/11/2026 -> sau đó đổi TTS_MODEL thành
# "heygen/heygen-voice-1-turbo" (trả phí, khoảng $4 cho mỗi triệu ký tự). Vẫn dùng được Fish: "fish-audio/s2.1-pro-free:free".
TTS_MODEL = env("TTS_MODEL", "heygen/heygen-voice-1-turbo:free")
_is_heygen = TTS_MODEL.startswith("heygen/")
# Định dạng âm thanh yêu cầu. HeyGen: "pcm" (thô 16-bit, mono, 44,1 kHz; bắt buộc có ffmpeg để đổi sang ogg/opus).
# Fish Audio: "mp3". Endpoint mặc định trả "pcm" nên bot luôn gửi định dạng rõ ràng.
TTS_FORMAT = env("TTS_FORMAT", "pcm" if _is_heygen else "mp3")
# Tần số mẫu của PCM (trang HeyGen ghi 44,1 kHz). Nghe giọng nhanh/chậm/méo thì thử 24000 hoặc 22050.
# Nếu header Content-Type có "rate=" thì ưu tiên theo header.
TTS_PCM_RATE = int(env("TTS_PCM_RATE", "44100"))
# HeyGen có 10 giọng: nadine, tabitha, bram, gareth, noelle, meredith, sabrina, lucian, weston, vincent.
# Fish Audio dùng ID giọng (dãy hex 32 ký tự) hoặc để trống = giọng mặc định. Giọng PHẢI khớp với model.
TTS_VOICE = env("TTS_VOICE", "sabrina" if _is_heygen else "")
# Chỉ dẫn cách đọc (giọng điệu, nhịp, cảm xúc). Fish Audio có hỗ trợ; HeyGen không có tham số này. Để trống = không gửi.
TTS_INSTRUCTIONS = env("TTS_INSTRUCTIONS", "")
TTS_MAX_CHARS = int(env("TTS_MAX_CHARS", "2000"))   # HeyGen cho tối đa 5.000 ký tự/lần; tin của Linh ngắn nên đặt 2.000 là dư
SEND_TEXT_ALSO = env("SEND_TEXT_ALSO", "0") == "1"   # "1" = gửi thêm bản chữ ngay sau voice
OPENROUTER_TITLE = env("OPENROUTER_TITLE", "Linh Voice Bot (ERNIE)")   # Tên hiển thị trên openrouter.ai (tùy chọn)
OPENROUTER_REFERER = env("OPENROUTER_REFERER", "")             # URL site cho bảng xếp hạng (tùy chọn, có thể để trống)
# "deny" = chỉ dùng nhà cung cấp cam kết không thu thập dữ liệu. Mặc định để trống (= allow) vì nhiều endpoint miễn phí
# không có tùy chọn này, đặt "deny" có thể khiến model free báo 404.
OPENROUTER_DATA_COLLECTION = env("OPENROUTER_DATA_COLLECTION", "")

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
    "Chỉ viết bằng tiếng Việt, không chèn chữ Hán hay chữ Trung/Nhật/Hàn. "
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
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
TTS_URL = "https://openrouter.ai/api/v1/audio/speech"
GH_API = "https://api.github.com"
# Tên file lưu bộ nhớ trong repo riêng tư. Mặc định "state_ernie.json" để bot này có bộ nhớ RIÊNG, không đụng vào
# state.json của bot khác nếu hai bot cùng trỏ vào một repo dữ liệu. Muốn dùng tiếp bộ nhớ của bot cũ thì đặt "state.json".
STATE_FILE = env("STATE_FILE", "state_ernie.json")

TTS_ATTEMPTS = 2             # Số lần thử gọi TTS trước khi chuyển sang gửi chữ
TTS_RETRY_DELAY = 3          # Giây chờ giữa 2 lần thử

MEMORY_WINDOW = 15           # Số tin gần nhất gửi kèm làm ngữ cảnh mỗi lần gọi API
MAX_STORED_MESSAGES = 600000     # Chỉ cần lưu hơn 14 tin một chút -> mỗi lần lưu rất nhẹ
POLL_TIMEOUT = 20            # Long Polling: Telegram giữ kết nối tối đa 20s nếu chưa có tin mới
RETRY_DELAY = 5              # Giây chờ khi lỗi mạng / lỗi API trước khi thử lại
RATE_LIMIT_DELAY = 25        # Giây chờ cơ sở khi bị báo 429 (lần thử thứ n sẽ chờ n lần mức này, tối đa 60s)
MAX_RETRY_PER_UPDATE = 3     # Một tin nhắn lỗi tối đa 3 lần thì bỏ qua
MAX_RETRY_BUSY = 6           # Riêng khi nhà cung cấp model quá tải (429 từ upstream) thì kiên nhẫn hơn: ~4 phút

# --- Tính năng Linh chủ động nhắn trước khi chủ nhân im lặng ---
PROACTIVE_ENABLED = True
PROACTIVE_MIN_IDLE = 1 * 300
PROACTIVE_MAX_IDLE = 1 * 600
MAX_UNANSWERED_PROACTIVE = 200000     # Nhắn trước tối đa 2 tin liên tiếp nếu chưa được trả lời
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
    "history": [],       # Danh sách {"role": "user"/"assistant", "content": "...", ["reasoning_details": [...]]}
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
    """Tải file bộ nhớ (STATE_FILE) từ repo riêng tư. Trả về dict, hoặc None nếu file chưa tồn tại."""
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
        clean = []
        for m in history:
            if not (isinstance(m, dict)
                    and m.get("role") in ("user", "assistant")
                    and isinstance(m.get("content"), str)):
                continue
            item = {"role": m["role"], "content": m["content"]}
            details = m.get("reasoning_details")
            if m["role"] == "assistant" and isinstance(details, list) and details:
                item["reasoning_details"] = details
            clean.append(item)
        store["history"] = clean
    for key, cast in (("last_user", float), ("last_event", float), ("unanswered", int)):
        try:
            store[key] = cast(data.get(key, store[key]))
        except (TypeError, ValueError):
            pass


def load_history():
    return list(store["history"])


def prune_reasoning(history):
    """Chỉ giữ reasoning_details ở KEEP_REASONING_TURNS câu trả lời gần nhất, các câu cũ hơn bỏ đi."""
    kept = 0
    for m in reversed(history):
        if m.get("role") == "assistant" and "reasoning_details" in m:
            kept += 1
            if not KEEP_REASONING or kept > KEEP_REASONING_TURNS:
                m.pop("reasoning_details", None)
    return history


def save_history(history):
    """Cập nhật lịch sử trong store rồi đẩy lên repo riêng tư."""
    store["history"] = prune_reasoning(history[-MAX_STORED_MESSAGES:])
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


class ChatAction:
    """
    Giữ trạng thái 'đang nhập...' / 'đang ghi âm...' suốt lúc chờ. Telegram chỉ hiển thị mỗi lần ~5 giây,
    mà model suy nghĩ có thể mất cả chục giây -> chạy một luồng nền gửi lại mỗi 4 giây để Linh không bị "đứng hình".
    Luồng nền dùng requests.post riêng (không dùng chung session) cho an toàn.
    """

    def __init__(self, chat_id, action="typing"):
        self.chat_id = chat_id
        self.action = action
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        url = "https://api.telegram.org/bot{}/sendChatAction".format(TELEGRAM_BOT_TOKEN)
        while not self._stop.is_set():
            try:
                requests.post(url, json={"chat_id": self.chat_id, "action": self.action}, timeout=5)
            except Exception:
                pass
            self._stop.wait(4)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._thread.join(timeout=2)
        return False


# ============================================================
# OPENROUTER (dùng chung cho chat và giọng nói)
# ============================================================
class RateLimitError(Exception):
    """
    OpenRouter báo 429. `wait` = số giây nên chờ trước khi thử lại.
    `upstream` = True nếu lỗi do NHÀ CUNG CẤP của model đang quá tải/giới hạn (không phải hạn mức tài khoản của bạn).
    """

    def __init__(self, wait=RATE_LIMIT_DELAY, upstream=False):
        super().__init__("OpenRouter báo 429 ({})".format("nhà cung cấp model quá tải" if upstream else "giới hạn tốc độ/hạn mức"))
        self.wait = wait
        self.upstream = upstream


# Gợi ý dễ hiểu cho các mã lỗi thường gặp (hiện trong log Actions để bạn biết sửa gì)
OR_HINTS = {
    400: "tham số bị từ chối - xem thông báo ngay trước",
    401: "sai hoặc thiếu OPENROUTER_API_KEY",
    402: "OpenRouter hết credit",
    404: "model không tồn tại/đã bị gỡ (kiểm tra tên model trên openrouter.ai, thử bỏ đuôi ':free'), "
         "hoặc không có endpoint nào khớp chính sách dữ liệu của tài khoản (Settings > Privacy)",
}
# 429 có thể đến từ HAI nơi nên hai gợi ý khác nhau
HINT_429_UPSTREAM = (
    "nhà cung cấp của model đang quá tải hoặc tự giới hạn (KHÔNG phải hạn mức tài khoản của bạn; model free thường chỉ có "
    "1 nhà cung cấp nên không có đường dự phòng). Bot tự chờ rồi thử lại; muốn né hẳn thì đặt LLM_FALLBACK_MODELS"
)
HINT_429_ACCOUNT = (
    "chạm hạn mức của OpenRouter cho model free: 20 yêu cầu/phút, và 50 yêu cầu/ngày nếu tài khoản chưa nạp đủ 10 credit "
    "(1000/ngày nếu đã nạp). Mỗi lượt trả lời tốn 2 yêu cầu (chat + giọng nói)"
)


def or_headers():
    headers = {
        "Authorization": "Bearer {}".format(OPENROUTER_API_KEY),
        "Content-Type": "application/json",
        "X-OpenRouter-Title": OPENROUTER_TITLE,
    }
    if OPENROUTER_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_REFERER
    return headers


def provider_prefs():
    """Tùy chọn chính sách dữ liệu gửi kèm mỗi yêu cầu (None nếu không đặt)."""
    if OPENROUTER_DATA_COLLECTION in ("allow", "deny"):
        return {"data_collection": OPENROUTER_DATA_COLLECTION}
    return None


def report_openrouter_error(what, status, error, fallback_text=""):
    """
    Ghi log một lỗi của OpenRouter (kèm gợi ý cách sửa) và trả về True nếu lỗi do NHÀ CUNG CẤP upstream gây ra.
    Khối `error.metadata` cho biết nhà cung cấp nào và mã lỗi gốc của họ. Với 429/5xx (lỗi dung lượng, không
    chứa nội dung chat) mới in thêm `raw`; lỗi 400 chỉ in thông điệp để chắc chắn không lộ nội dung vào log công khai.
    """
    reason, extra, upstream = str(fallback_text), "", False
    if isinstance(error, dict):
        reason = str(error.get("message") or fallback_text)
        upstream = "provider returned error" in reason.lower()
        meta = error.get("metadata")
        if isinstance(meta, dict):
            parts = []
            for key in ("provider_name", "provider_code"):
                if meta.get(key) not in (None, ""):
                    parts.append("{}={}".format(key, meta[key]))
            if status == 429 or status >= 500:
                if meta.get("raw") not in (None, ""):
                    parts.append("raw={}".format(str(meta["raw"])[:300]))
            extra = ", ".join(parts)
            upstream = upstream or bool(meta.get("provider_name"))
    elif error:
        reason = str(error)

    if status == 429:
        hint = HINT_429_UPSTREAM if upstream else HINT_429_ACCOUNT
    else:
        hint = OR_HINTS.get(status, "")
        if what == "TTS" and status == 404 and TTS_MODEL.endswith(":free"):
            hint += ". Nếu bản ':free' đã bị gỡ (ghi chú cũ nói là 30/11/2026) thì đổi TTS_MODEL thành bản không có ':free' (trả phí)"
    logging.error(
        "OpenRouter %s HTTP %s: %s%s%s",
        what,
        status,
        reason[:200],
        " [" + extra + "]" if extra else "",
        " -> " + hint if hint else "",
    )
    return upstream


def check_openrouter_response(resp, what):
    """
    Nếu OpenRouter trả lỗi HTTP thì ghi rõ lý do + gợi ý cách sửa (không ghi nội dung chat) rồi ném lỗi.
    Riêng 429 ném RateLimitError để phía trên biết đường chờ lâu hơn thay vì thử dồn dập.
    """
    if resp.status_code < 400:
        return
    error = None
    try:
        data = resp.json()
        if isinstance(data, dict):
            error = data.get("error") or data.get("message")
    except ValueError:
        pass
    upstream = report_openrouter_error(what, resp.status_code, error, resp.text[:200])
    if resp.status_code == 429:
        try:
            wait = min(60, max(5, int(resp.headers.get("Retry-After", RATE_LIMIT_DELAY))))
        except (TypeError, ValueError):
            wait = RATE_LIMIT_DELAY
        raise RateLimitError(wait, upstream)
    resp.raise_for_status()


# ============================================================
# CHAT: Baidu ERNIE-5.5-Flash-Preview qua OpenRouter
# ============================================================
# Các mẫu nhận diện câu trả lời "vô nghĩa" do model sinh lỗi
CJK_PATTERN = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")  # chữ Trung/Nhật/Hàn
REPEAT_CHAR_PATTERN = re.compile(r"(.)\1{14,}")   # 1 ký tự lặp liên tiếp từ 15 lần
LEAK_MARKERS = ("[Nhắc Linh", "[Tình huống", "[BỐI CẢNH", "[Lưu ý giọng nói")   # model lỡ đọc lại chỉ dẫn nội bộ
VI_CHARS = set("àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ")
THINK_PATTERN = re.compile(r"<think>.*?</think>", re.S | re.I)   # Phòng khi model để lộ phần suy nghĩ trong content


def strip_think(text):
    """Bỏ khối <think>...</think> (nếu có). Khối mở mà không đóng thì bỏ từ chỗ đó trở đi."""
    text = THINK_PATTERN.sub("", text)
    idx = text.lower().find("<think>")
    if idx != -1:
        text = text[:idx]
    return text.strip()


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


def post_chat(payload):
    """Gọi OpenRouter chat/completions. Ném lỗi nếu HTTP lỗi, hoặc HTTP 200 nhưng thân phản hồi là lỗi."""
    resp = session.post(CHAT_URL, headers=or_headers(), json=payload, timeout=(5, LLM_READ_TIMEOUT))
    check_openrouter_response(resp, "chat")
    data = resp.json()
    error = data.get("error")
    if isinstance(error, dict) and not data.get("choices"):
        code = error.get("code")
        status = code if isinstance(code, int) else 502
        upstream = report_openrouter_error("chat (lỗi trong thân phản hồi)", status, error)
        if status == 429:
            raise RateLimitError(RATE_LIMIT_DELAY, upstream)
        raise RuntimeError("OpenRouter chat lỗi (code {})".format(code))
    used = data.get("model")
    if LLM_FALLBACK_MODELS and used and used != LLM_MODEL:
        logging.info("Lượt này model chính lỗi, đã dùng model dự phòng: %s", used)   # Tên model không nhạy cảm
    return data


# Khả năng của model, học được trong lúc chạy. Có model (hoặc nhà cung cấp) không cho tắt suy nghĩ và trả 400 khi
# nhận effort "none"; biết rồi thì các lượt sau bỏ hẳn tham số reasoning thay vì tốn thêm một lượt gọi bị từ chối.
llm_caps = {"cannot_disable_reasoning": False, "reasoning_rejected": False}


def reasoning_param(on):
    """Tham số `reasoning` gửi lên. Bật: {"enabled": true} đúng như mẫu OpenRouter. Tắt: effort "none" (cách tài liệu ghi nhận).
    Trả về None (= không gửi tham số) nếu model đã được biết là từ chối tham số đó."""
    if on:
        return None if llm_caps["reasoning_rejected"] else {"enabled": True}
    if llm_caps["cannot_disable_reasoning"]:
        return None
    return {"effort": "none"}


def call_llm(messages):
    """
    Gửi messages tới model chat (ERNIE-5.5-Flash-Preview) qua OpenRouter.
    Trả về (câu trả lời, reasoning_details hoặc None).
    Câu trả lời lỗi/vô nghĩa bị loại và thử lại 1 lần với nhiệt độ thấp hơn. Lỗi HTTP/timeout sẽ ném
    exception cho vòng lặp chính xử lý.
    """
    last_problem = "không rõ"
    reasoning_on = LLM_REASONING
    for attempt in range(2):
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": 1.0 if attempt == 0 else 0.7,   # Lần thử lại hạ nhiệt độ cho ổn định
            "max_tokens": LLM_MAX_TOKENS,
            "stream": False,
        }
        reasoning = reasoning_param(reasoning_on)
        if reasoning is not None:
            payload["reasoning"] = reasoning
        if LLM_FALLBACK_MODELS:
            # Theo tài liệu OpenRouter: danh sách theo thứ tự ưu tiên, model đầu lỗi (kể cả 429) thì thử model kế tiếp
            del payload["model"]
            payload["models"] = [LLM_MODEL] + LLM_FALLBACK_MODELS
        prefs = provider_prefs()
        if prefs:
            payload["provider"] = prefs

        # Lỗi 400 có thể do các tham số tùy chọn bị model từ chối: reasoning_details cũ, effort "none", hoặc reasoning enabled.
        # Mỗi thứ chỉ được sửa một lần rồi gọi lại; lỗi 400 khác thì ném ra như bình thường.
        # Chỉ GHI NHỚ "model từ chối reasoning" khi gọi lại không có tham số đó mà thành công. Như vậy một lỗi 400 không
        # liên quan (vd yêu cầu hỏng) không làm bot tắt reasoning nhầm cho cả lượt chạy.
        learn = None
        for _ in range(3):
            try:
                data = post_chat(payload)
                if learn:
                    llm_caps[learn] = True
                    logging.warning("Đã xác nhận model từ chối tham số reasoning -> các lượt sau không gửi nữa")
                break
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 400 and any("reasoning_details" in m for m in messages):
                    logging.warning("API từ chối reasoning_details cũ -> thử lại không gửi kèm")
                    messages = [{k: v for k, v in m.items() if k != "reasoning_details"} for m in messages]
                    payload["messages"] = messages
                elif status == 400 and payload.get("reasoning") == {"effort": "none"}:
                    logging.warning("effort none bị từ chối (model không cho tắt suy nghĩ?) -> thử lại không có tham số reasoning")
                    learn = "cannot_disable_reasoning"
                    payload.pop("reasoning")
                elif status == 400 and payload.get("reasoning") == {"enabled": True}:
                    logging.warning("reasoning bị từ chối (model không hỗ trợ?) -> thử lại không có tham số reasoning")
                    learn = "reasoning_rejected"
                    payload.pop("reasoning")
                else:
                    raise
        else:
            raise RuntimeError("Yêu cầu chat vẫn bị từ chối sau khi đã sửa tham số")

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        raw = message.get("content")
        content = strip_think(raw) if isinstance(raw, str) else ""
        # Chỉ lấy reasoning_details khi đang bật suy nghĩ, hoặc khi không gửi tham số reasoning (model tự suy nghĩ theo mặc định)
        details = message.get("reasoning_details") if (reasoning_on or "reasoning" not in payload) else None

        if choice.get("finish_reason") == "length":
            if content:
                content = trim_incomplete(content)   # Bị cắt vì hết token -> bỏ câu dở dang ở cuối
            elif reasoning_on:
                # Model dùng hết token vào phần suy nghĩ, chưa kịp trả lời -> lần thử sau tắt suy nghĩ
                logging.warning("Hết %s token mà chưa có câu trả lời (đang suy nghĩ) -> thử lại không suy nghĩ", LLM_MAX_TOKENS)
                reasoning_on = False

        problem = find_problem(content)
        if problem is None:
            return content, (details if isinstance(details, list) and details else None)

        # Log công khai: chỉ ghi lý do và độ dài, KHÔNG ghi nội dung
        last_problem = problem
        logging.warning("Loại câu trả lời lỗi (%s), lần %s, dài %s ký tự", problem, attempt + 1, len(content))

    # Lỗi cả 2 lần -> ném exception: KHÔNG gửi, KHÔNG lưu vào bộ nhớ, vòng lặp chính sẽ thử lại
    raise ValueError("Model trả lời lỗi 2 lần liền: {}".format(last_problem))


def build_context(history):
    """Ghép system prompt + tối đa 14 tin gần nhất làm ngữ cảnh."""
    window = history[-MEMORY_WINDOW:]
    # Bỏ các tin assistant nằm đầu cửa sổ để ngữ cảnh luôn bắt đầu bằng lượt của user
    while window and window[0]["role"] != "user":
        window = window[1:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in window:
        item = {"role": m["role"], "content": m["content"]}
        details = m.get("reasoning_details")
        # Gửi lại reasoning_details đúng nguyên bản (không sửa) như mẫu của OpenRouter
        if KEEP_REASONING and m["role"] == "assistant" and isinstance(details, list) and details:
            item["reasoning_details"] = details
        messages.append(item)
    return messages


def with_reminder(text):
    """Gắn lời nhắc phong cách (nếu có) vào cuối nội dung gửi API; lịch sử vẫn lưu bản gốc."""
    return (text + "\n\n" + STYLE_REMINDER) if STYLE_REMINDER else text


def ask_llm(user_text, history):
    """Trả lời tin nhắn mới của chủ nhân. Trả về (câu trả lời, reasoning_details hoặc None)."""
    messages = build_context(history)
    messages.append({"role": "user", "content": with_reminder(user_text)})
    return call_llm(messages)


def generate_proactive(history, idle_hours):
    """Tạo tin nhắn Linh tự gửi trước, dựa trên bối cảnh trò chuyện gần đây."""
    messages = build_context(history)
    instruction = PROACTIVE_INSTRUCTION.replace("{hours}", str(idle_hours))
    messages.append({"role": "user", "content": with_reminder(instruction)})
    text, _ = call_llm(messages)   # Chỉ dẫn nội bộ không được lưu -> cũng không lưu reasoning_details của lượt này
    return text


# ============================================================
# GIỌNG NÓI: HeyGen Voice Turbo (hoặc Fish Audio) qua OpenRouter -> Telegram sendVoice
# ============================================================
GESTURE_PATTERN = re.compile(r"\*[^*]*\*")   # *cử chỉ* - không đọc thành tiếng
EMOJI_PATTERN = re.compile(
    "[\U0001F000-\U0001FAFF←-⇿☀-➿⬀-⯿️‍⃣]+"
)


def truncate_at_sentence(text, limit):
    """Cắt gọn về `limit` ký tự, ưu tiên cắt ở cuối câu để giọng đọc không đứt giữa chừng."""
    if len(text) <= limit:
        return text
    cut = max(text.rfind(ch, 0, limit) for ch in ".!?…")
    return text[:cut + 1] if cut > limit * 0.5 else text[:limit]


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
    return truncate_at_sentence(text, TTS_MAX_CHARS)


def looks_like_mp3(data):
    """Dấu hiệu đầu file mp3: thẻ 'ID3' hoặc khung đồng bộ 0xFFEx."""
    return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)


def to_ogg_opus(audio, pcm_rate=None):
    """
    Đổi âm thanh sang ogg/opus bằng ffmpeg - định dạng Telegram hiển thị thành bong bóng tin nhắn thoại.
    - pcm_rate=None: `audio` là mp3/ogg có sẵn tiêu đề, ffmpeg tự nhận dạng.
    - pcm_rate=N: `audio` là PCM thô (16-bit little-endian, mono) với tần số mẫu N Hz.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("máy không có ffmpeg")
    if pcm_rate:
        if len(audio) % 2:
            audio = audio[:-1]   # PCM 16-bit phải chẵn byte
        source = ["-f", "s16le", "-ar", str(pcm_rate), "-ac", "1"]
    else:
        source = []
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", *source, "-i", "pipe:0", "-vn",
         "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
         "-ar", "48000", "-ac", "1", "-f", "ogg", "pipe:1"],
        input=audio, capture_output=True, timeout=60,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg lỗi: {}".format(proc.stderr.decode("utf-8", "ignore")[:150]))
    return proc.stdout


def synthesize(text):
    """
    Gọi TTS qua OpenRouter rồi đổi sang ogg/opus. Trả về (bytes âm thanh, 'ogg' hoặc 'mp3').
    LƯU Ý: response_format mặc định của endpoint là "pcm" (thô, không phát được nếu không xử lý), nên luôn gửi
    định dạng rõ ràng: HeyGen = pcm (đổi bằng ffmpeg), Fish Audio = mp3.
    """
    payload = {
        "model": TTS_MODEL,
        "input": text,
        "response_format": TTS_FORMAT,
    }
    if TTS_VOICE:
        payload["voice"] = TTS_VOICE
    if TTS_INSTRUCTIONS:
        payload["instructions"] = TTS_INSTRUCTIONS
    prefs = provider_prefs()
    if prefs:
        payload["provider"] = prefs

    resp = session.post(TTS_URL, headers=or_headers(), json=payload, timeout=(5, 90))
    check_openrouter_response(resp, "TTS")

    ctype = resp.headers.get("Content-Type", "").lower()
    if "json" in ctype:
        raise ValueError("OpenRouter trả JSON thay vì âm thanh")
    audio = resp.content
    if len(audio) < 1000:
        raise ValueError("Âm thanh rỗng hoặc quá ngắn ({} byte)".format(len(audio)))

    # Nhận diện bằng dấu hiệu CHẶT ('ID3'/'OggS') hoặc Content-Type. Không dò khung mp3 trên PCM thô vì PCM bắt đầu
    # bằng đoạn im lặng (0xFF 0xFF) sẽ bị nhầm thành mp3.
    is_container = audio[:3] == b"ID3" or audio[:4] == b"OggS" or "mpeg" in ctype or "ogg" in ctype
    if not is_container and (TTS_FORMAT == "pcm" or "pcm" in ctype):
        if TTS_FORMAT != "pcm":
            raise ValueError("OpenRouter trả PCM dù đã yêu cầu {}".format(TTS_FORMAT))
        rate = TTS_PCM_RATE
        match = re.search(r"rate=(\d{4,6})", ctype)
        if match:                 # Header nói rõ tần số mẫu thì tin header
            rate = int(match.group(1))
        # PCM thô không gửi thẳng được -> ffmpeg bắt buộc; thiếu/lỗi thì ném lỗi để deliver() gửi bản chữ
        return to_ogg_opus(audio, pcm_rate=rate), "ogg"

    try:
        return to_ogg_opus(audio), "ogg"
    except Exception as e:
        if looks_like_mp3(audio):
            logging.warning("Không đổi được sang ogg/opus (%s) -> gửi thẳng mp3", e)
            return audio, "mp3"
        raise


def text_to_voice(text):
    """synthesize() có thử lại. Hết lượt thử vẫn lỗi thì ném lỗi cuối cùng."""
    last_error = None
    for attempt in range(TTS_ATTEMPTS):
        try:
            return synthesize(text)
        except RateLimitError:
            raise   # Đang bị giới hạn: thử lại ngay chỉ phí lượt -> gửi chữ luôn
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code < 500:
                raise   # Lỗi 4xx (sai khóa, hết credit, sai tham số...) thử lại cũng vậy
            last_error = e
        except Exception as e:
            last_error = e
        logging.warning("TTS lỗi lần %s/%s: %s", attempt + 1, TTS_ATTEMPTS, last_error)
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
            with ChatAction(chat_id, "record_voice"):
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
    except RateLimitError as e:
        # Bị giới hạn tốc độ: dời lượt nhắn trước, không "đốt" thêm yêu cầu free
        logging.error("Nhắn trước bị giới hạn tốc độ, thử lại sau %s giây", max(e.wait, PROACTIVE_RETRY_DELAY))
        timer["next_at"] = time.time() + max(e.wait, PROACTIVE_RETRY_DELAY)
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

    history = load_history()                       # Đọc bộ nhớ
    with ChatAction(chat_id, "typing"):            # Giữ "đang nhập..." trong lúc model suy nghĩ
        reply, details = ask_llm(text, history)    # Gọi AI (lỗi sẽ ném exception ra ngoài)
    deliver(chat_id, reply)                        # Đọc thành giọng nói rồi gửi (lỗi thì gửi chữ)

    # Chỉ lưu vào bộ nhớ SAU KHI đã gửi thành công -> không bao giờ lưu nửa vời
    # (lưu bản chữ gốc, còn nguyên cử chỉ, chứ không phải bản đã lọc để đọc)
    history.append({"role": "user", "content": text})
    entry = {"role": "assistant", "content": reply}
    if KEEP_REASONING and details:
        entry["reasoning_details"] = details
    history.append(entry)
    save_history(history)
    return True


def notify_failure(error=None):
    """Báo nhẹ cho chủ nhân khi một tin nhắn lỗi liên tục và bị bỏ qua."""
    if isinstance(error, RateLimitError):
        text = "Em đang bị nghẽn lượt nhắn nên chưa trả lời được, anh đợi vài phút rồi nhắn lại giúp em nhé 💋"
    else:
        text = "Em bị chập chờn sóng một chút, anh nhắn lại giúp em nhé 💋"
    try:
        send_message(CHAT_ID, text)
    except Exception as e:
        logging.error("Không gửi được thông báo lỗi: %s", e)


def notify_busy():
    """Báo một lần khi phải chờ lâu (nhà cung cấp model quá tải) để chủ nhân không tưởng bot đứng hình."""
    try:
        send_message(CHAT_ID, "Em đang hơi chậm một chút, anh đợi em xíu nhé 💕")
    except Exception as e:
        logging.error("Không gửi được thông báo chờ: %s", e)


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
    logging.info(
        "LLM: %s (suy nghĩ: %s) | TTS: %s (%s), giọng '%s'",
        LLM_MODEL, "bật" if LLM_REASONING else "tắt", TTS_MODEL, TTS_FORMAT, TTS_VOICE or "mặc định",
    )
    if LLM_FALLBACK_MODELS:
        logging.info("Model dự phòng: %s", ", ".join(LLM_FALLBACK_MODELS))
        for name in LLM_FALLBACK_MODELS:
            if not name.endswith(":free"):
                logging.info("Model dự phòng '%s' không có đuôi ':free' -> chỉ dùng khi các model trước lỗi, bị tính tiền và cần credit", name)

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

    offset = None           # update_id kế tiếp cần lấy
    fail_count = 0          # Số lần liên tiếp tin hiện tại bị lỗi
    busy_notified = False   # Đã báo "em đang chậm" cho tin hiện tại chưa

    try:
        while time.time() < deadline:
            try:
                updates = get_updates(offset)  # Treo tối đa 20s, có tin là trả về ngay

                for update in updates:
                    delay = RETRY_DELAY
                    limit_error = None   # Có giá trị nếu lần lỗi này là 429
                    try:
                        ok = handle_update(update)
                    except RateLimitError as e:
                        limit_error = e
                        logging.error("Lỗi khi xử lý tin nhắn: %s", e)
                        ok = False
                    except Exception as e:
                        logging.error("Lỗi khi xử lý tin nhắn: %s", e)
                        ok = False

                    if ok:
                        offset = update["update_id"] + 1   # Xác nhận đã xử lý xong tin này
                        fail_count = 0
                        busy_notified = False
                        continue

                    # Tin bị lỗi: giữ nguyên offset để lần quét sau lấy lại đúng tin này
                    fail_count += 1
                    busy = limit_error is not None and limit_error.upstream
                    # Nhà cung cấp quá tải thường tự hết sau vài phút -> kiên nhẫn hơn (MAX_RETRY_BUSY lần) thay vì bỏ tin sớm
                    max_tries = MAX_RETRY_BUSY if busy else MAX_RETRY_PER_UPDATE
                    if limit_error is not None:
                        delay = min(60, limit_error.wait * fail_count)   # Chờ lâu dần: 25s, 50s, 60s...
                    if fail_count >= max_tries:
                        offset = update["update_id"] + 1   # Quá số lần thử -> bỏ qua tin này
                        fail_count = 0
                        busy_notified = False
                        logging.error("Bỏ qua một tin nhắn sau %s lần lỗi", max_tries)
                        notify_failure(limit_error)
                    else:
                        if busy and not busy_notified:
                            busy_notified = True
                            notify_busy()
                        time.sleep(delay)                  # Đợi rồi thử lại
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
