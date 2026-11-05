# -*- coding: utf-8 -*-
"""
bot.py - Linh Voice Bot chạy trên GitHub Actions (Long Polling).

Bản CROUTON + VOICE + ẢNH:
- Crouton Alpha (stealth/crouton-alpha, qua OpenRouter, có reasoning) viết câu trả lời.
- HeyGen Voice Turbo (qua OpenRouter) đọc thành giọng nói -> đổi PCM sang ogg/opus bằng ffmpeg -> sendVoice.
- Khi cần, Gemini (khóa từ aistudio.google.com) tạo ảnh -> sendPhoto.
Voice lỗi thì gửi chữ; ảnh hết lượt/lỗi thì bỏ qua và chỉ gửi chữ/voice. Bot không bao giờ im lặng.

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
from collections import Counter, namedtuple
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
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")  # Một khóa OpenRouter dùng cho cả Crouton Alpha (chat) lẫn HeyGen (giọng nói)
GOOGLE_API_KEY = env("GOOGLE_API_KEY")          # Khóa từ aistudio.google.com để tạo ảnh (TÙY CHỌN, trống = tắt tính năng ảnh)
STATE_REPO = env("STATE_REPO")                  # Dạng "ten-github/linh-bot-data" (repo RIÊNG TƯ)
STATE_REPO_TOKEN = env("STATE_REPO_TOKEN")      # Token quyền Contents: Read and write cho repo đó

RUN_SECONDS = int(env("RUN_SECONDS", "20400"))  # Thời gian chạy mỗi lượt (5h40m < giới hạn 6h)
TZ_OFFSET_HOURS = float(env("TZ_OFFSET_HOURS", "7"))  # Múi giờ để tính giờ yên lặng (VN = +7)

# --- Chat: Crouton Alpha qua OpenRouter ---
# Đây là model "stealth": nhà phát triển ẩn danh, nhà cung cấp có thể lưu prompt/câu trả lời, và OpenRouter
# có thể gỡ hoặc đổi sang trả phí bất cứ lúc nào. Đổi sang model khác chỉ cần sửa LLM_MODEL.
LLM_MODEL = env("LLM_MODEL", "stealth/crouton-alpha")
# Token suy luận TÍNH VÀO max_tokens. Đặt dư để model còn chỗ viết câu trả lời sau khi suy nghĩ.
LLM_MAX_TOKENS = int(env("LLM_MAX_TOKENS", "4000"))
REASONING_ENABLED = env("REASONING_ENABLED", "1") == "1"
# Tùy chọn: "low"/"medium"/"high"... để chỉnh độ sâu suy luận (bỏ trống = mặc định của model).
REASONING_EFFORT = env("REASONING_EFFORT", "")
# Giữ lại reasoning_details của N câu trả lời gần nhất của Linh và gửi lại cho model (OpenRouter khuyên làm vậy để
# model "suy luận tiếp từ chỗ đang dở"). 0 = không giữ. Chỉ giữ ít câu để file bộ nhớ không phình to.
KEEP_REASONING_TURNS = int(env("KEEP_REASONING_TURNS", "1"))

# --- Ảnh: Gemini (Google AI Studio) ---
IMAGE_ENABLED = bool(GOOGLE_API_KEY)
# Nano Banana 2 Lite (rẻ nhất). Các lựa chọn khác: gemini-3.1-flash-image, gemini-3-pro-image, gemini-2.5-flash-image
IMAGE_MODEL = env("IMAGE_MODEL", "gemini-3.1-flash-lite-image")
IMAGE_ASPECT = env("IMAGE_ASPECT", "3:4")        # Tỉ lệ khung hình; để trống nếu model báo lỗi tham số này
IMAGE_AUTO = env("IMAGE_AUTO", "1") == "1"       # "1" = Linh tự quyết định lúc nào gửi ảnh; "0" = chỉ gửi khi bạn gõ /anh
# Trần số ảnh mỗi ngày (theo múi giờ của bạn): chống tốn tiền ngoài ý muốn nếu bạn bật thanh toán. 0 = không giới hạn.
IMAGE_MAX_PER_DAY = int(env("IMAGE_MAX_PER_DAY", "10"))
IMAGE_COOLDOWN_MIN = int(env("IMAGE_COOLDOWN_MIN", "30"))   # Hết lượt (429) thì nghỉ thử ảnh bao nhiêu phút
# Mô tả ngoại hình cố định của Linh để ảnh nhất quán (đặt trong Secrets, vd "cô gái Việt Nam 25 tuổi, tóc dài...")
LINH_APPEARANCE = env("LINH_APPEARANCE", "")
IMAGE_STYLE = env("IMAGE_STYLE", "Ảnh chụp đời thường, tự nhiên như chụp bằng điện thoại, ánh sáng đẹp. Không có chữ, logo hay watermark.")
# Tin nhắn chữ gửi thêm khi ảnh Linh định gửi không tạo được. Mặc định trống = chỉ gửi chữ/voice như bình thường.
IMAGE_FAIL_NOTE = env("IMAGE_FAIL_NOTE", "")

# --- Giọng nói (TTS) ---
# Bản ":free" sẽ bị OpenRouter gỡ ngày 30/11/2026 -> sau đó đổi TTS_MODEL thành "heygen/heygen-voice-1-turbo" (trả phí).
TTS_MODEL = env("TTS_MODEL", "heygen/heygen-voice-1-turbo:free")
# 10 giọng có sẵn: nadine, tabitha, bram, gareth, noelle, meredith, sabrina, lucian, weston, vincent
TTS_VOICE = env("TTS_VOICE", "nadine")
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
# Chỉ gắn vào prompt khi tính năng ảnh đang bật. Thẻ [ANH: ...] được bot tự thay bằng ảnh thật.
DEFAULT_IMAGE_SUFFIX = (
    "[Gửi ảnh: khi anh muốn xem ảnh của em, hoặc em thật sự muốn khoe một khoảnh khắc đời thường "
    "(selfie, bữa ăn, góc phòng...), hãy nói bình thường rồi thêm ở CUỐI tin đúng MỘT thẻ dạng "
    "[ANH: mô tả ngắn cảnh trong ảnh]. Chỉ dùng khi thật sự cần, không dùng liên tục mỗi tin. "
    "Ảnh luôn đời thường, kín đáo, phù hợp mọi lứa tuổi. Thẻ sẽ tự được thay bằng ảnh nên đừng giải thích về nó.]"
)
DEFAULT_PROACTIVE_INSTRUCTION = (
    "[Tình huống: đã khoảng {hours} giờ anh không nhắn tin cho em. "
    "Hãy nhắn trước một tin ngắn, ngọt ngào, nói em nhớ anh.]"
)
_voice_suffix = env("VOICE_PROMPT_SUFFIX", DEFAULT_VOICE_SUFFIX)
SYSTEM_PROMPT = env("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
if _voice_suffix and _voice_suffix != "-":
    SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + _voice_suffix
if IMAGE_ENABLED and IMAGE_AUTO:
    SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + env("IMAGE_PROMPT_SUFFIX", DEFAULT_IMAGE_SUFFIX)
STYLE_REMINDER = env("STYLE_REMINDER", "")      # Lời nhắc gắn vào cuối mỗi tin của chủ nhân (có thể để trống)
PROACTIVE_INSTRUCTION = env("PROACTIVE_INSTRUCTION", DEFAULT_PROACTIVE_INSTRUCTION)

# ============================================================
# HẰNG SỐ HỆ THỐNG
# ============================================================
LLM_URL = "https://openrouter.ai/api/v1/chat/completions"
IMAGE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
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
        for secret in (TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, GOOGLE_API_KEY, STATE_REPO_TOKEN):
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
    "image_usage": {"day": "", "count": 0},   # Số ảnh đã tạo trong ngày (để áp trần IMAGE_MAX_PER_DAY)
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
        cleaned = []
        for m in history:
            if not (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                    and isinstance(m.get("content"), str)):
                continue
            entry = {"role": m["role"], "content": m["content"]}
            # reasoning_details của model: giữ nguyên, không sửa (chỉ nhận dạng danh sách)
            if m["role"] == "assistant" and isinstance(m.get("reasoning_details"), list) and m["reasoning_details"]:
                entry["reasoning_details"] = m["reasoning_details"]
            cleaned.append(entry)
        store["history"] = cleaned
    usage = data.get("image_usage")
    if isinstance(usage, dict):
        try:
            store["image_usage"] = {"day": str(usage.get("day", "")), "count": int(usage.get("count", 0))}
        except (TypeError, ValueError):
            pass
    for key, cast in (("last_user", float), ("last_event", float), ("unanswered", int)):
        try:
            store[key] = cast(data.get(key, store[key]))
        except (TypeError, ValueError):
            pass


def load_history():
    return list(store["history"])


def prune_reasoning(history):
    """Chỉ giữ reasoning_details ở KEEP_REASONING_TURNS câu trả lời gần nhất của Linh, các câu cũ hơn bỏ đi cho nhẹ."""
    seen = 0
    for message in reversed(history):
        if message.get("role") == "assistant":
            seen += 1
            if seen > KEEP_REASONING_TURNS:
                message.pop("reasoning_details", None)


def save_history(history):
    """Cập nhật lịch sử trong store rồi đẩy lên repo riêng tư."""
    prune_reasoning(history)
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


def send_photo(chat_id, image, mime):
    """Gửi ảnh vào Telegram (upload multipart)."""
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    url = "https://api.telegram.org/bot{}/sendPhoto".format(TELEGRAM_BOT_TOKEN)
    resp = session.post(
        url,
        data={"chat_id": chat_id},
        files={"photo": ("linh.{}".format(ext), image, mime)},
        timeout=(5, 60),
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram từ chối sendPhoto: {}".format(data))


def send_action(chat_id, action):
    """Hiện trạng thái 'đang nhập...' / 'đang ghi âm...'. Lỗi ở đây không quan trọng nên bỏ qua."""
    try:
        tg_call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=5)
    except Exception:
        pass


def send_typing(chat_id):
    send_action(chat_id, "typing")


# ============================================================
# LLM: CROUTON ALPHA QUA OPENROUTER
# ============================================================
# Các mẫu nhận diện câu trả lời "vô nghĩa" do model sinh lỗi
CJK_PATTERN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")  # chữ Trung/Nhật/Hàn
REPEAT_CHAR_PATTERN = re.compile(r"(.)\1{14,}")   # 1 ký tự lặp liên tiếp từ 15 lần
LEAK_MARKERS = ("[Nhắc Linh", "[Tình huống", "[BỐI CẢNH", "[Lưu ý giọng nói", "[Gửi ảnh")   # model lỡ đọc lại chỉ dẫn nội bộ
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


# Câu trả lời của model có thể kèm thẻ [ANH: mô tả] (cuối tin) để xin gửi ảnh. Tách thẻ ra khỏi lời nói.
IMAGE_TAG_WORDS = r"(?:ảnh|anh|img|image|photo|selfie)"
IMAGE_TAG_PATTERN = re.compile(r"\[\s*" + IMAGE_TAG_WORDS + r"\s*:\s*([^\]]{3,500}?)\s*\]", re.IGNORECASE)
IMAGE_TAG_OPEN_PATTERN = re.compile(r"\[\s*" + IMAGE_TAG_WORDS + r"\s*:[^\]]*$", re.IGNORECASE)   # thẻ bị cắt dở ở cuối

# Kết quả một lượt chat: lời nói, mô tả ảnh muốn gửi (hoặc None), reasoning_details để lưu (hoặc None)
Reply = namedtuple("Reply", ["text", "image", "reasoning"])

LLM_HINTS = {
    401: "sai hoặc thiếu OPENROUTER_API_KEY",
    402: "OpenRouter hết credit, hoặc model free đã chuyển sang trả phí",
    404: "không tìm thấy model - model stealth có thể bị gỡ bất cứ lúc nào, hãy đổi LLM_MODEL",
    429: "chạm giới hạn tốc độ/hạn mức của model free, chờ một lúc rồi thử lại",
}


def extract_image_tag(text):
    """Tách thẻ [ANH: ...] khỏi câu trả lời. Trả về (lời nói sạch, mô tả ảnh đầu tiên hoặc None)."""
    text = unicodedata.normalize("NFC", text)
    prompts = IMAGE_TAG_PATTERN.findall(text)
    cleaned = IMAGE_TAG_PATTERN.sub(" ", text)
    cleaned = IMAGE_TAG_OPEN_PATTERN.sub("", cleaned)
    if cleaned != text:
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, (prompts[0].strip() if prompts else None)


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
    """Lấy phần chữ của câu trả lời. Chỉ lấy `content`, TUYỆT ĐỐI không dùng `reasoning` (suy nghĩ nội bộ)."""
    content = message.get("content")
    if isinstance(content, list):   # Một số model trả content dạng danh sách các phần
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def call_llm(messages):
    """
    Gửi messages tới Crouton Alpha (bật reasoning). Câu trả lời lỗi/vô nghĩa bị loại và thử lại 1 lần
    với nhiệt độ thấp hơn và gấp đôi max_tokens. Lỗi HTTP/timeout ném exception cho vòng lặp chính xử lý.
    Trả về Reply(text, image, reasoning).
    """
    last_problem = "không rõ"
    for attempt in range(2):
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": 1.0 if attempt == 0 else 0.7,   # Lần thử lại hạ nhiệt độ cho ổn định
            # Token suy luận tính vào max_tokens -> lần thử lại gấp đôi để chắc chắn còn chỗ cho câu trả lời
            "max_tokens": LLM_MAX_TOKENS * (attempt + 1),
            "stream": False,
        }
        if REASONING_ENABLED:
            reasoning = {"enabled": True}
            if REASONING_EFFORT:
                reasoning["effort"] = REASONING_EFFORT
            if KEEP_REASONING_TURNS <= 0:
                reasoning["exclude"] = True   # Không lưu suy luận thì khỏi nhận về cho nhẹ (vẫn suy luận bên trong)
            payload["reasoning"] = reasoning

        # (5s kết nối, 120s chờ phản hồi: model suy luận trả lời chậm hơn model thường)
        resp = session.post(LLM_URL, headers=openrouter_headers(), json=payload, timeout=(5, 120))
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
        message = choice.get("message") or {}
        content = message_text(message).strip()
        details = message.get("reasoning_details")
        if KEEP_REASONING_TURNS <= 0 or not isinstance(details, list) or not details:
            details = None

        text, image = extract_image_tag(content)

        # Bị cắt vì hết token -> bỏ câu dở dang ở cuối
        if choice.get("finish_reason") == "length":
            if not text:
                logging.warning("Hết max_tokens trước khi có câu trả lời (suy luận ăn hết) -> "
                                "tăng LLM_MAX_TOKENS hoặc đặt REASONING_EFFORT=low")
            text = trim_incomplete(text)

        problem = find_problem(text)
        if problem is None:
            return Reply(text, image, details)

        # Log công khai: chỉ ghi lý do và độ dài, KHÔNG ghi nội dung
        last_problem = problem
        logging.warning("Loại câu trả lời lỗi (%s), lần %s, dài %s ký tự", problem, attempt + 1, len(text))

    # Lỗi cả 2 lần -> ném exception: KHÔNG gửi, KHÔNG lưu vào bộ nhớ, vòng lặp chính sẽ thử lại
    raise ValueError("LLM trả lời lỗi 2 lần liền: {}".format(last_problem))


def build_context(history):
    """Ghép system prompt + tối đa 14 tin gần nhất làm ngữ cảnh (kèm reasoning_details nếu có, giữ nguyên)."""
    window = history[-MEMORY_WINDOW:]
    # Bỏ các tin assistant nằm đầu cửa sổ để ngữ cảnh luôn bắt đầu bằng lượt của user
    while window and window[0]["role"] != "user":
        window = window[1:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(dict(m) for m in window)
    return messages


def with_reminder(text):
    """Gắn lời nhắc phong cách (nếu có) vào cuối nội dung gửi API; lịch sử vẫn lưu bản gốc."""
    return (text + "\n\n" + STYLE_REMINDER) if STYLE_REMINDER else text


def ask_llm(user_text, history):
    """Trả lời tin nhắn mới của chủ nhân. Trả về Reply."""
    messages = build_context(history)
    messages.append({"role": "user", "content": with_reminder(user_text)})
    return call_llm(messages)


def generate_proactive(history, idle_hours):
    """Tạo tin nhắn Linh tự gửi trước, dựa trên bối cảnh trò chuyện gần đây. Trả về Reply."""
    messages = build_context(history)
    instruction = PROACTIVE_INSTRUCTION.replace("{hours}", str(idle_hours))
    messages.append({"role": "user", "content": with_reminder(instruction)})
    return call_llm(messages)


def assistant_entry(reply):
    """Một mục lịch sử cho câu trả lời của Linh: lưu lời nói (không có thẻ ảnh) + reasoning_details nếu cần."""
    entry = {"role": "assistant", "content": reply.text}
    if reply.reasoning and KEEP_REASONING_TURNS > 0:
        entry["reasoning_details"] = reply.reasoning
    return entry


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
# ẢNH: Gemini (Google AI Studio) -> Telegram sendPhoto
# ============================================================
# Quan trọng: theo trang giá của Google, model ảnh Gemini có thể KHÔNG có gói miễn phí (cần bật thanh toán).
# Khi đó mọi lời gọi báo 429 hết hạn mức -> bot nghỉ thử ảnh một lúc và chỉ gửi chữ/voice, không bị hỏng.
DEFAULT_SELFIE = "Một tấm selfie tự nhiên của em ở nhà, nụ cười dịu dàng."
IMAGE_SAFETY_NOTE = "Ảnh đời thường, an toàn, phù hợp mọi lứa tuổi, trang phục kín đáo, không nhạy cảm."
IMAGE_FATAL_BLOCK = 6 * 3600    # Lỗi cấu hình (key sai, model không tồn tại): nghỉ gần hết lượt chạy
IMAGE_TEMP_BLOCK = 120          # Google lỗi tạm thời: nghỉ 2 phút


class ImageError(Exception):
    """Không tạo/gửi được ảnh. Chỉ là chuyện bỏ qua ảnh, không phải lỗi của cả tin nhắn."""


image_state = {"blocked_until": 0.0, "reason": ""}   # Ngắt mạch: tạm ngừng gọi API ảnh khi hết lượt/sai cấu hình


def today_key():
    return datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS))).strftime("%Y-%m-%d")


def image_usage_today():
    usage = store.get("image_usage") or {}
    return usage.get("count", 0) if usage.get("day") == today_key() else 0


def count_image_usage():
    store["image_usage"] = {"day": today_key(), "count": image_usage_today() + 1}


def block_images(seconds, reason):
    image_state["blocked_until"] = time.time() + seconds
    image_state["reason"] = reason


def image_unavailable_reason():
    """Lý do hiện không nên tạo ảnh (chuỗi rỗng = được phép)."""
    if not IMAGE_ENABLED:
        return "chưa cấu hình GOOGLE_API_KEY"
    if time.time() < image_state["blocked_until"]:
        return "đang tạm nghỉ ({})".format(image_state["reason"])
    if IMAGE_MAX_PER_DAY > 0 and image_usage_today() >= IMAGE_MAX_PER_DAY:
        return "đã đủ {} ảnh hôm nay".format(IMAGE_MAX_PER_DAY)
    return ""


def build_image_prompt(description):
    """Ghép phong cách + ngoại hình cố định của Linh + cảnh cần chụp + ghi chú an toàn."""
    description = " ".join(description.split())[:400].rstrip(".")
    parts = [IMAGE_STYLE]
    if LINH_APPEARANCE:
        parts.append("Nhân vật chính: {}.".format(LINH_APPEARANCE.rstrip(".")))
    parts.append("Cảnh: {}.".format(description))
    parts.append(IMAGE_SAFETY_NOTE)
    return " ".join(parts)


def google_error_message(resp):
    try:
        data = resp.json()
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or "")
    except ValueError:
        pass
    return resp.text or ""


def raise_for_image_status(resp):
    """Nếu Google trả lỗi HTTP: đặt thời gian nghỉ phù hợp, ghi lý do + gợi ý (không ghi nội dung chat) rồi ném ImageError."""
    code = resp.status_code
    if code < 400:
        return
    message = google_error_message(resp)
    hint = ""
    if code == 429:
        block_images(IMAGE_COOLDOWN_MIN * 60, "hết hạn mức, HTTP 429")
        hint = ("hết lượt hoặc gói miễn phí không có hạn mức tạo ảnh (thường ghi 'limit: 0'); "
                "kiểm tra hạn mức thật ở aistudio.google.com/rate-limit, muốn tạo ảnh cần bật thanh toán cho project")
    elif code in (401, 403):
        block_images(IMAGE_FATAL_BLOCK, "khóa/quyền bị từ chối, HTTP {}".format(code))
        hint = "kiểm tra GOOGLE_API_KEY, hoặc khu vực của bạn chưa được hỗ trợ tạo ảnh"
    elif code == 404:
        block_images(IMAGE_FATAL_BLOCK, "model không tồn tại, HTTP 404")
        hint = "đổi IMAGE_MODEL (vd gemini-3.1-flash-image hoặc gemini-2.5-flash-image)"
    elif code == 400 and re.search(r"api[ _]?key", message, re.IGNORECASE):
        block_images(IMAGE_FATAL_BLOCK, "API key không hợp lệ, HTTP 400")
        hint = "kiểm tra lại GOOGLE_API_KEY"
    elif code >= 500:
        block_images(IMAGE_TEMP_BLOCK, "Google lỗi tạm thời, HTTP {}".format(code))
    raise ImageError("Gemini ảnh HTTP {}: {}{}".format(code, message[:200], " -> " + hint if hint else ""))


def parse_image_response(data):
    """Lấy ảnh cuối cùng (bỏ qua ảnh nháp 'thought') từ phản hồi của Gemini. Trả về (bytes, mime)."""
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise ImageError("Gemini chặn yêu cầu ảnh ({})".format(feedback["blockReason"]))
    image = None
    reasons = []
    for candidate in data.get("candidates") or []:
        reasons.append(str(candidate.get("finishReason") or ""))
        for part in (candidate.get("content") or {}).get("parts") or []:
            if part.get("thought"):
                continue
            blob = part.get("inlineData") or part.get("inline_data")
            if isinstance(blob, dict) and blob.get("data"):
                image = (base64.b64decode(blob["data"]), blob.get("mimeType") or blob.get("mime_type") or "image/png")
    if image is None:
        raise ImageError("Gemini không trả về ảnh (finishReason: {})".format(", ".join(reasons) or "không rõ"))
    return image


def generate_image(description):
    """Gọi Gemini tạo ảnh. Trả về (bytes, mime). Mọi thất bại đều ném ImageError."""
    url = IMAGE_URL.format(IMAGE_MODEL)
    headers = {"x-goog-api-key": GOOGLE_API_KEY, "Content-Type": "application/json"}   # Khóa ở header, không nằm trong URL
    config = {"responseModalities": ["TEXT", "IMAGE"]}
    if IMAGE_ASPECT:
        config["imageConfig"] = {"aspectRatio": IMAGE_ASPECT}
    body = {"contents": [{"parts": [{"text": build_image_prompt(description)}]}]}

    for attempt in range(2):
        body["generationConfig"] = config
        try:
            resp = session.post(url, headers=headers, json=body, timeout=(5, 90))
        except requests.RequestException as e:
            raise ImageError("lỗi mạng khi tạo ảnh: {}".format(e))
        # Model không nhận tham số tỉ lệ khung hình -> thử lại 1 lần không có nó
        if (resp.status_code == 400 and "imageConfig" in config and attempt == 0
                and re.search(r"image_?config|aspect", google_error_message(resp), re.IGNORECASE)):
            logging.warning("Gemini từ chối tham số tỉ lệ khung hình, thử lại không có nó (đặt IMAGE_ASPECT trống để bỏ hẳn)")
            config = {"responseModalities": config["responseModalities"]}
            continue
        break

    raise_for_image_status(resp)
    try:
        return parse_image_response(resp.json())
    except ValueError:
        raise ImageError("Gemini trả về dữ liệu không đọc được")


def try_image(chat_id, description):
    """
    Tạo ảnh bằng Gemini rồi gửi vào Telegram. Hết lượt, bị chặn, lỗi mạng... đều chỉ ghi log và trả về False,
    KHÔNG ném lỗi (để phần chữ/voice đã gửi không bị gửi lại).
    """
    reason = image_unavailable_reason()
    if reason:
        logging.info("Bỏ qua ảnh: %s", reason)
        return False
    try:
        send_action(chat_id, "upload_photo")
        image, mime = generate_image(description)
        count_image_usage()    # Tính ngay khi tạo xong (lúc này mới phát sinh phí), kể cả khi gửi Telegram lỗi
        send_photo(chat_id, image, mime)
        return True
    except ImageError as e:
        logging.warning("%s -> chỉ gửi chữ/voice", e)
    except Exception as e:
        logging.error("Lỗi khi gửi ảnh (%s) -> chỉ gửi chữ/voice", e)
    return False


def deliver_image(chat_id, description):
    """Gửi ảnh Linh tự quyết định gửi. Ảnh không được thì gửi ghi chú (nếu bạn đặt IMAGE_FAIL_NOTE), còn lại im lặng."""
    if try_image(chat_id, description):
        return True
    if IMAGE_FAIL_NOTE:
        try:
            send_message(chat_id, IMAGE_FAIL_NOTE)
        except Exception as e:
            logging.error("Không gửi được ghi chú ảnh: %s", e)
    return False


def send_reply(chat_id, reply):
    """Gửi câu trả lời của Linh: voice (hoặc chữ) trước, rồi ảnh nếu Linh có xin gửi ảnh."""
    deliver(chat_id, reply.text)
    if reply.image:
        deliver_image(chat_id, reply.image)


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
        reply = generate_proactive(history, idle_hours)
        send_reply(CHAT_ID, reply)

        # Chỉ lưu tin của Linh (không lưu chỉ dẫn nội bộ)
        history.append(assistant_entry(reply))
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

    # Lệnh /anh [mô tả]: tạo ảnh theo ý bạn, không qua AI chat (tiết kiệm hạn mức, tiện để thử tính năng ảnh)
    parts = text.strip().split(None, 1)
    if parts and parts[0].lower().split("@")[0] == "/anh":
        description = parts[1].strip() if len(parts) > 1 else DEFAULT_SELFIE
        if not try_image(chat_id, description):
            send_message(chat_id, "Em chưa gửi được ảnh lúc này, anh thử lại sau nhé 💕")
        push_state()   # Lưu bộ đếm ảnh trong ngày
        return True

    send_typing(chat_id)

    history = load_history()                       # Đọc bộ nhớ
    reply = ask_llm(text, history)                 # Gọi AI (lỗi sẽ ném exception ra ngoài)
    send_reply(chat_id, reply)                     # Voice (lỗi thì chữ), rồi ảnh nếu Linh xin gửi (lỗi thì bỏ qua)

    # Chỉ lưu vào bộ nhớ SAU KHI đã gửi thành công -> không bao giờ lưu nửa vời
    # (lưu lời nói gốc, còn nguyên cử chỉ, không có thẻ ảnh; chứ không phải bản đã lọc để đọc)
    history.append({"role": "user", "content": text})
    history.append(assistant_entry(reply))
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
    logging.info("LLM: %s | TTS: %s, giọng '%s'", LLM_MODEL, TTS_MODEL, TTS_VOICE)
    logging.info("Ảnh: %s", "{} (tối đa {}/ngày)".format(IMAGE_MODEL, IMAGE_MAX_PER_DAY or "không giới hạn")
                 if IMAGE_ENABLED else "TẮT (chưa có GOOGLE_API_KEY)")

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
