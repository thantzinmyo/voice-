from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
VOICE_DIR = BASE_DIR / "voice_samples"
DB_PATH = BASE_DIR / "bot.sqlite3"
RUNTIME_DIR.mkdir(exist_ok=True)
VOICE_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "841100316"))
F5_DEVICE = os.getenv("F5_DEVICE", "cpu").strip()
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "1000"))
MAX_DAILY_GENERATIONS = int(os.getenv("MAX_DAILY_GENERATIONS", "20"))
MAX_SAMPLE_SECONDS = int(os.getenv("MAX_SAMPLE_SECONDS", "120"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("burmese-voice-bot")
router = Router()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def display_dt(value: Optional[str]) -> str:
    if not value:
        return "မရှိသေးပါ"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    consented INTEGER NOT NULL DEFAULT 0,
                    voice_id TEXT,
                    voice_sample_path TEXT,
                    voice_ref_text TEXT,
                    access_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS redeem_codes (
                    code TEXT PRIMARY KEY,
                    duration_days INTEGER NOT NULL CHECK(duration_days > 0),
                    created_by INTEGER NOT NULL,
                    redeemed_by INTEGER,
                    redeemed_at TEXT,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    char_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_generations_user_date
                    ON generations(telegram_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_codes_status
                    ON redeem_codes(redeemed_by, revoked_at);
                """
            )
            cursor = await db.execute("PRAGMA table_info(users)")
            existing_columns = {row[1] for row in await cursor.fetchall()}
            for column, column_type in (
                ("voice_sample_path", "TEXT"),
                ("voice_ref_text", "TEXT"),
            ):
                if column not in existing_columns:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {column} {column_type}")
            await db.commit()

    async def ensure_user(self, user_id: int, username: Optional[str]) -> None:
        timestamp = iso(now_utc())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users(telegram_id, username, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username=excluded.username,
                    updated_at=excluded.updated_at
                """,
                (user_id, username, timestamp, timestamp),
            )
            await db.commit()

    async def get_user(self, user_id: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
            return await cursor.fetchone()

    async def set_consent(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET consented = 1, updated_at = ? WHERE telegram_id = ?", (iso(now_utc()), user_id))
            await db.commit()

    async def set_voice(self, user_id: int, sample_path: str, ref_text: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE users
                SET voice_id = 'local-f5', voice_sample_path = ?, voice_ref_text = ?, updated_at = ?
                WHERE telegram_id = ?
                """,
                (sample_path, ref_text, iso(now_utc()), user_id),
            )
            await db.commit()

    async def redeem(self, user_id: int, code: str) -> tuple[bool, str]:
        timestamp = now_utc()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM redeem_codes WHERE code = ?",
                (code.upper(),),
            )
            row = await cursor.fetchone()
            if not row:
                await db.rollback()
                return False, "Redeem code မတွေ့ပါ။"
            if row["revoked_at"]:
                await db.rollback()
                return False, "ဒီ code ကို Admin က revoke လုပ်ထားပါတယ်။"
            if row["redeemed_by"]:
                await db.rollback()
                return False, "ဒီ code ကို အသုံးပြုပြီးသားပါ။"

            user_cursor = await db.execute("SELECT access_expires_at FROM users WHERE telegram_id = ?", (user_id,))
            user_row = await user_cursor.fetchone()
            current_expiry = None
            if user_row and user_row[0]:
                try:
                    current_expiry = datetime.fromisoformat(user_row[0])
                except ValueError:
                    current_expiry = None
            base = current_expiry if current_expiry and current_expiry > timestamp else timestamp
            new_expiry = base + timedelta(days=int(row["duration_days"]))

            await db.execute(
                "UPDATE redeem_codes SET redeemed_by = ?, redeemed_at = ? WHERE code = ? AND redeemed_by IS NULL AND revoked_at IS NULL",
                (user_id, iso(timestamp), code.upper()),
            )
            await db.execute(
                "UPDATE users SET access_expires_at = ?, updated_at = ? WHERE telegram_id = ?",
                (iso(new_expiry), iso(timestamp), user_id),
            )
            await db.commit()
            return True, iso(new_expiry)

    async def create_codes(self, count: int, duration_days: int, created_by: int) -> list[str]:
        created: list[str] = []
        timestamp = iso(now_utc())
        async with aiosqlite.connect(self.path) as db:
            for _ in range(count):
                while True:
                    code = "VC-" + "-".join("".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4)) for _ in range(3))
                    try:
                        await db.execute(
                            "INSERT INTO redeem_codes(code, duration_days, created_by, created_at) VALUES (?, ?, ?, ?)",
                            (code, duration_days, created_by, timestamp),
                        )
                        created.append(code)
                        break
                    except aiosqlite.IntegrityError:
                        continue
            await db.commit()
        return created

    async def revoke_code(self, code: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE redeem_codes SET revoked_at = ? WHERE code = ? AND redeemed_by IS NULL AND revoked_at IS NULL",
                (iso(now_utc()), code.upper()),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def code_stats(self) -> tuple[int, int, int]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT
                    SUM(CASE WHEN redeemed_by IS NULL AND revoked_at IS NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN redeemed_by IS NOT NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN revoked_at IS NOT NULL THEN 1 ELSE 0 END)
                FROM redeem_codes
                """
            )
            row = await cursor.fetchone()
            return tuple(int(value or 0) for value in row)

    async def latest_unused_codes(self, limit: int = 30):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT code, duration_days, created_at FROM redeem_codes WHERE redeemed_by IS NULL AND revoked_at IS NULL ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return await cursor.fetchall()

    async def count_users(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            row = await cursor.fetchone()
            return int(row[0])

    async def daily_generations(self, user_id: int) -> int:
        day = now_utc().date().isoformat()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM generations WHERE telegram_id = ? AND substr(created_at, 1, 10) = ?",
                (user_id, day),
            )
            row = await cursor.fetchone()
            return int(row[0])

    async def record_generation(self, user_id: int, char_count: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO generations(telegram_id, char_count, created_at) VALUES (?, ?, ?)",
                (user_id, char_count, iso(now_utc())),
            )
            await db.commit()


class LocalF5Provider:
    """Local Burmese F5-TTS model. The model stays on the VPS; no API key is used."""

    def __init__(self, device: str = "cpu"):
        self.device = device
        self.model = None
        self.lock = asyncio.Lock()

    def load(self) -> None:
        from f5_myanmar_tts import MyanmarTTS

        logger.info("Loading local F5 Myanmar TTS model on %s", self.device)
        self.model = MyanmarTTS(device=self.device)
        logger.info("Local F5 Myanmar TTS model is ready")

    def _synthesize_sync(self, text: str, ref_audio: str, ref_text: str, output_path: str) -> None:
        if self.model is None:
            self.load()
        self.model.speak(
            text=text,
            output_file=output_path,
            ref_audio=ref_audio,
            ref_text=ref_text,
            speed=1.0,
        )

    async def synthesize(self, text: str, ref_audio: str, ref_text: str) -> bytes:
        output_path = RUNTIME_DIR / f"speech_{uuid.uuid4().hex}.wav"
        async with self.lock:
            try:
                await asyncio.to_thread(
                    self._synthesize_sync,
                    text,
                    ref_audio,
                    ref_text,
                    str(output_path),
                )
                return output_path.read_bytes()
            finally:
                output_path.unlink(missing_ok=True)


db = Database(DB_PATH)
provider = LocalF5Provider(F5_DEVICE)


async def prepare_user(message: Message) -> None:
    if message.from_user:
        await db.ensure_user(message.from_user.id, message.from_user.username)


async def active_access(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    row = await db.get_user(user_id)
    if not row or not row["access_expires_at"]:
        return False
    try:
        return datetime.fromisoformat(row["access_expires_at"]) > now_utc()
    except ValueError:
        return False


async def require_access(message: Message) -> bool:
    if not message.from_user or not await active_access(message.from_user.id):
        await message.answer("အသုံးပြုရန် အရင်ဆုံး valid Redeem Code ထည့်ပါ။\nအသုံးပြုပုံ: /redeem CODE")
        return False
    return True


def parse_duration(value: str) -> Optional[int]:
    match = re.fullmatch(r"(\d+)\s*(d|day|days|m|mo|month|months)", value.lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0 or amount > 3650:
        return None
    return amount * (30 if unit in {"m", "mo", "month", "months"} else 1)


def convert_to_wav(source: Path, target: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(source), "-ac", "1", "-ar", "24000", "-codec:a", "pcm_s16le", str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-500:])


def convert_wav_to_mp3(source: Path, target: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(source), "-codec:a", "libmp3lame", "-b:a", "128k", str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-500:])


@router.message(Command("start"))
async def start_handler(message: Message) -> None:
    await prepare_user(message)
    await message.answer(
        "မင်္ဂလာပါ။ Burmese Voice Clone Bot မှ ကြိုဆိုပါတယ်။\n\n"
        "အသုံးပြုရန် Admin ထံမှ Redeem Code ရယူပြီး /redeem CODE ဖြင့် activate လုပ်ပါ။\n"
        "/consent - ကိုယ့်အသံ/ခွင့်ပြုချက်ရှိသောအသံဖြစ်ကြောင်း အတည်ပြုရန်\n"
        "/voice - voice sample ပို့ရန် လမ်းညွှန်\n"
        "/say စာသား - cloned Burmese voice နဲ့ အသံပြောင်းရန်\n"
        "/status - access နှင့် voice အခြေအနေကြည့်ရန်\n"
        "/help - command စာရင်း"
    )


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await prepare_user(message)
    text = (
        "User Commands\n"
        "/redeem CODE - access activate\n"
        "/consent - voice ownership/permission confirm\n"
        "/voice - sample တင်ရန်\n"
        "/say စာသား - Burmese TTS ထုတ်ရန်\n"
        "/status - လက်ကျန် access ကြည့်ရန်\n\n"
        "Voice sample ကို Telegram voice message သို့မဟုတ် audio file အဖြစ် တိုက်ရိုက်ပို့နိုင်ပါတယ်။"
    )
    if message.from_user and is_admin(message.from_user.id):
        text += (
            "\n\nAdmin Commands\n"
            "/gen_code 30d 5 - 30 ရက် code ၅ ခုထုတ်ရန်\n"
            "/gen_code 1m 10 - ၁ လ code ၁၀ ခုထုတ်ရန်\n"
            "/codes - code statistics နှင့် unused code များ\n"
            "/revoke CODE - မသုံးရသေးတဲ့ code ပိတ်ရန်\n"
            "/users - user count"
        )
    await message.answer(text)


@router.message(Command("consent"))
async def consent_handler(message: Message) -> None:
    await prepare_user(message)
    if not await require_access(message):
        return
    await db.set_consent(message.from_user.id)
    await message.answer(
        "အတည်ပြုပြီးပါပြီ။ ကိုယ့်အသံ သို့မဟုတ် အသံပိုင်ရှင်ခွင့်ပြုချက်ရှိတဲ့ sample ကိုသာ ပို့ပါ။\n"
        "အခု Telegram voice message/audio file ကို ပို့နိုင်ပါပြီ။"
    )


@router.message(Command("redeem"))
async def redeem_handler(message: Message, command: CommandObject) -> None:
    await prepare_user(message)
    if not command.args:
        await message.answer("အသုံးပြုပုံ: /redeem CODE")
        return
    ok, result = await db.redeem(message.from_user.id, command.args.strip())
    if ok:
        await message.answer(f"Redeem အောင်မြင်ပါတယ်။ Access သက်တမ်းကုန်ဆုံးမည့်အချိန်: {display_dt(result)}")
    else:
        await message.answer(result)


@router.message(Command("status"))
async def status_handler(message: Message) -> None:
    await prepare_user(message)
    row = await db.get_user(message.from_user.id)
    access = "Admin access" if is_admin(message.from_user.id) else display_dt(row["access_expires_at"] if row else None)
    voice = "တင်ပြီးပါပြီ" if row and row["voice_id"] else "မတင်ရသေးပါ"
    consent = "အတည်ပြုပြီး" if row and row["consented"] else "မအတည်ပြုရသေးပါ"
    usage = await db.daily_generations(message.from_user.id)
    await message.answer(f"Access: {access}\nVoice: {voice}\nConsent: {consent}\nဒီနေ့အသုံးပြုမှု: {usage}/{MAX_DAILY_GENERATIONS}")


@router.message(Command("voice"))
async def voice_help_handler(message: Message) -> None:
    await prepare_user(message)
    if not await require_access(message):
        return
    await message.answer(
        "Voice clone လုပ်ရန် 30–120 စက္ကန့်ခန့် ရှင်းလင်းတဲ့ Burmese အသံကို\n"
        "background music မပါဘဲ Telegram voice message/audio file အဖြစ် ပို့ပါ။\n"
        "Caption ထဲမှာ sample ထဲပြောထားတဲ့ Burmese စာသားအတိအကျကို ရေးပေးပါ။\n\n"
        "အသံက ကိုယ့်အသံ သို့မဟုတ် ခွင့်ပြုချက်ရှိတဲ့အသံ ဖြစ်ရပါမယ်။"
    )


@router.message(Command("say"))
async def say_command_handler(message: Message, command: CommandObject) -> None:
    await prepare_user(message)
    if not await require_access(message):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("အသုံးပြုပုံ: /say မင်္ဂလာပါ")
        return
    await generate_and_send(message, text)


@router.message(F.voice | F.audio)
async def sample_handler(message: Message, bot: Bot) -> None:
    await prepare_user(message)
    if not await require_access(message):
        return
    row = await db.get_user(message.from_user.id)
    if not row or not row["consented"]:
        await message.answer("အရင်ဆုံး /consent ကိုနှိပ်ပြီး voice ownership/permission ကို အတည်ပြုပါ။")
        return

    media = message.voice or message.audio
    if not media:
        return
    ref_text = (message.caption or "").strip()
    if not ref_text:
        await message.answer(
            "Local voice clone အတွက် caption မဖြစ်မနေလိုပါတယ်။\n"
            "အသံထဲမှာ ပြောထားတဲ့ Burmese စာသားအတိအကျကို caption ထဲရေးပြီး ထပ်ပို့ပါ။"
        )
        return
    if len(ref_text) > 1000:
        await message.answer("Voice sample caption ကို စာလုံး 1000 အောက်ထားပေးပါ။")
        return
    duration = getattr(media, "duration", None) or 0
    if duration and duration > MAX_SAMPLE_SECONDS:
        await message.answer(f"Sample ကို {MAX_SAMPLE_SECONDS} စက္ကန့်အောက်ထားပေးပါ။")
        return

    suffix = ".ogg" if message.voice else ".mp3"
    token = uuid.uuid4().hex
    source = RUNTIME_DIR / f"sample_{message.from_user.id}_{token}{suffix}"
    prepared = RUNTIME_DIR / f"sample_{message.from_user.id}_{token}.wav"
    destination = VOICE_DIR / f"user_{message.from_user.id}.wav"
    try:
        telegram_file = await bot.get_file(media.file_id)
        await bot.download_file(telegram_file.file_path, destination=source)
        await message.answer("Voice sample ကို VPS ထဲမှာ သိမ်းနေပါတယ်…")
        await asyncio.to_thread(convert_to_wav, source, prepared)
        await asyncio.to_thread(shutil.copy2, prepared, destination)
        await db.set_voice(message.from_user.id, str(destination), ref_text)
        await message.answer("Local Burmese voice sample သိမ်းပြီးပါပြီ။ အခု /say မင်္ဂလာပါ လို့ စမ်းနိုင်ပါပြီ။")
    except Exception as exc:
        logger.exception("Unexpected error while storing local voice sample: %s", exc)
        await message.answer("Voice sample processing မအောင်မြင်ပါ။ အသံရှင်းလင်းတဲ့ sample နဲ့ ထပ်စမ်းပါ။")
    finally:
        source.unlink(missing_ok=True)
        prepared.unlink(missing_ok=True)


async def generate_and_send(message: Message, text: str) -> None:
    if len(text) > MAX_TEXT_CHARS:
        await message.answer(f"စာသားကို {MAX_TEXT_CHARS} လုံးအောက်ထားပေးပါ။")
        return
    usage = await db.daily_generations(message.from_user.id)
    if usage >= MAX_DAILY_GENERATIONS and not is_admin(message.from_user.id):
        await message.answer("ဒီနေ့အသုံးပြုခွင့် limit ပြည့်ပါပြီ။")
        return
    row = await db.get_user(message.from_user.id)
    if not row or not row["voice_sample_path"] or not row["voice_ref_text"]:
        await message.answer("အရင်ဆုံး /consent ပြီး caption နဲ့ voice sample ပို့ပါ။")
        return

    await message.answer("အသံဖန်တီးနေပါတယ်…")
    wav_path = RUNTIME_DIR / f"speech_{uuid.uuid4().hex}.wav"
    mp3_path = RUNTIME_DIR / f"speech_{uuid.uuid4().hex}.mp3"
    try:
        audio = await provider.synthesize(text, row["voice_sample_path"], row["voice_ref_text"])
        wav_path.write_bytes(audio)
        await asyncio.to_thread(convert_wav_to_mp3, wav_path, mp3_path)
        await db.record_generation(message.from_user.id, len(text))
        await message.answer_audio(
            BufferedInputFile(mp3_path.read_bytes(), filename="burmese_voice.mp3"),
            caption="Burmese local voice clone",
        )
    except Exception as exc:
        logger.exception("Local F5 generation failed: %s", exc)
        await message.answer("အသံဖန်တီးရာမှာ အခက်အခဲဖြစ်သွားပါတယ်။")
    finally:
        wav_path.unlink(missing_ok=True)
        mp3_path.unlink(missing_ok=True)


@router.message(Command("gen_code"))
async def gen_code_handler(message: Message, command: CommandObject) -> None:
    await prepare_user(message)
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if not parts or len(parts) > 2:
        await message.answer("အသုံးပြုပုံ: /gen_code 30d 5\nသို့မဟုတ် /gen_code 1m 10")
        return
    duration_days = parse_duration(parts[0])
    if duration_days is None:
        await message.answer("Duration ကို 30d, 7d, 1m ပုံစံနဲ့ ထည့်ပါ။")
        return
    try:
        count = int(parts[1]) if len(parts) == 2 else 1
    except ValueError:
        count = 0
    if count < 1 or count > 100:
        await message.answer("Code အရေအတွက်ကို 1 မှ 100 အတွင်း ထည့်ပါ။")
        return
    codes = await db.create_codes(count, duration_days, message.from_user.id)
    await message.answer(f"{duration_days} ရက်သက်တမ်းရှိ code {len(codes)} ခု:\n\n" + "\n".join(codes))


@router.message(Command("codes"))
async def codes_handler(message: Message) -> None:
    await prepare_user(message)
    if not is_admin(message.from_user.id):
        return
    unused, redeemed, revoked = await db.code_stats()
    rows = await db.latest_unused_codes()
    output = [f"Unused: {unused} | Redeemed: {redeemed} | Revoked: {revoked}", "", "နောက်ဆုံး unused codes:"]
    output.extend(f"{row['code']} — {row['duration_days']} days" for row in rows)
    await message.answer("\n".join(output))


@router.message(Command("revoke"))
async def revoke_handler(message: Message, command: CommandObject) -> None:
    await prepare_user(message)
    if not is_admin(message.from_user.id):
        return
    code = (command.args or "").strip()
    if not code:
        await message.answer("အသုံးပြုပုံ: /revoke VC-XXXX-XXXX-XXXX")
        return
    result = await db.revoke_code(code)
    await message.answer("Code revoke လုပ်ပြီးပါပြီ။" if result else "Code မတွေ့ပါ သို့မဟုတ် အသုံးပြုပြီးသားပါ။")


@router.message(Command("users"))
async def users_handler(message: Message) -> None:
    await prepare_user(message)
    if is_admin(message.from_user.id):
        await message.answer(f"Registered users: {await db.count_users()}")


@router.message(F.text & ~F.text.startswith("/"))
async def plain_text_handler(message: Message) -> None:
    await prepare_user(message)
    if not message.text:
        return
    if not await require_access(message):
        return
    await generate_and_send(message, message.text.strip())


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing in .env")
    await db.init()
    await asyncio.to_thread(provider.load)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started in polling mode")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
