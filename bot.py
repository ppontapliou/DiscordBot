import os
import random
import asyncio
import shutil
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
AUDIO_FOLDER = Path(os.getenv("AUDIO_FOLDER", "./audio"))
FFMPEG_EXECUTABLE = shutil.which("ffmpeg")
SUPPORTED_FORMATS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus"}


def load_tracked_user_ids() -> set[int]:
    raw = os.getenv("TRACKED_USER_IDS", "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise SystemExit(f"[ERROR] TRACKED_USER_IDS содержит не число: {part}")
    return ids


TRACKED_USER_IDS = load_tracked_user_ids()

# (guild_id, channel_id) канала, куда бот идёт прямо сейчас.
# Новый заход, в том числе на другом сервере, сменяет цель.
current_target: tuple[int, int] | None = None
follow_lock = asyncio.Lock()

intents = discord.Intents.default()
intents.voice_states = True

bot = discord.Client(intents=intents)
VocalChannel = discord.VoiceChannel | discord.StageChannel


def get_random_audio() -> Path | None:
    """Случайный аудиофайл из папки."""
    if not AUDIO_FOLDER.exists():
        print(f"[ERROR] Папка с аудио не найдена: {AUDIO_FOLDER}")
        return None

    audio_files = [
        f for f in AUDIO_FOLDER.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_FORMATS
    ]
    if not audio_files:
        print(f"[ERROR] В папке нет аудиофайлов: {AUDIO_FOLDER}")
        return None

    return random.choice(audio_files)


def is_current(target: tuple[int, int]) -> bool:
    return current_target == target


async def wait_until(seconds: float, target: tuple[int, int]) -> bool:
    """Ждать, пока цель не сменилась. False — заход уже устарел."""
    elapsed = 0.0
    while elapsed < seconds:
        if not is_current(target):
            return False
        await asyncio.sleep(0.1)
        elapsed += 0.1
    return is_current(target)


async def stop_and_disconnect(voice_client: discord.VoiceClient | None):
    if voice_client is None:
        return
    if voice_client.is_playing():
        voice_client.stop()
    if voice_client.is_connected():
        await voice_client.disconnect(force=True)


async def leave_other_guilds(keep_guild_id: int):
    """Отключить бота от всех серверов, кроме того, куда он переходит."""
    for voice_client in list(bot.voice_clients):
        guild = voice_client.guild
        if guild is None or guild.id == keep_guild_id:
            continue
        print(f"[LEAVE] Бот отключился от сервера: {guild.name}")
        await stop_and_disconnect(voice_client)


async def connect_to(channel: VocalChannel) -> discord.VoiceClient:
    voice_client = channel.guild.voice_client
    if voice_client and voice_client.is_connected():
        if voice_client.channel != channel:
            await voice_client.move_to(channel)
            print(f"[MOVE] Бот перешёл в канал: {channel.name} ({channel.guild.name})")
        return voice_client

    voice_client = await channel.connect()
    print(f"[JOIN] Бот подключился к каналу: {channel.name} ({channel.guild.name})")
    return voice_client


async def play_audio(
    voice_client: discord.VoiceClient,
    audio_path: Path,
    target: tuple[int, int],
) -> bool:
    """Играть, пока файл не кончится или пользователь не уйдёт в другой канал."""
    if not FFMPEG_EXECUTABLE or not is_current(target):
        return False

    if voice_client.is_playing():
        voice_client.stop()

    voice_client.play(discord.FFmpegPCMAudio(str(audio_path), executable=FFMPEG_EXECUTABLE))
    print(f"[PLAY] Воспроизводится: {audio_path.name}")

    while voice_client.is_playing():
        if not is_current(target):
            voice_client.stop()
            return False
        await asyncio.sleep(0.1)

    return is_current(target)


async def follow(channel: VocalChannel, target: tuple[int, int]):
    """Зайти в канал, проиграть звук и выйти. Устаревший заход ничего не рвёт."""
    async with follow_lock:
        if not is_current(target):
            return

        voice_client: discord.VoiceClient | None = None
        try:
            await leave_other_guilds(channel.guild.id)
            if not is_current(target):
                return

            voice_client = await connect_to(channel)
            if not await wait_until(0.5, target):
                await stop_and_disconnect(voice_client)
                return

            audio_file = get_random_audio()
            played = audio_file is not None and await play_audio(voice_client, audio_file, target)
            if not played or not await wait_until(1, target):
                await stop_and_disconnect(voice_client)
                return

            await stop_and_disconnect(voice_client)
            print(f"[LEAVE] Бот отключился от канала: {channel.name} ({channel.guild.name})")
        except discord.ClientException as e:
            print(f"[ERROR] Ошибка подключения: {e}")
            await stop_and_disconnect(voice_client)
        except Exception as e:
            print(f"[ERROR] Непредвиденная ошибка: {e}")
            await stop_and_disconnect(voice_client)


@bot.event
async def on_ready():
    print(f"[BOT] Бот запущен как {bot.user}")
    print(f"[CONFIG] Отслеживаемые пользователи: {sorted(TRACKED_USER_IDS)}")
    print(f"[CONFIG] Папка с аудио: {AUDIO_FOLDER}")
    print(f"[CONFIG] FFmpeg: {FFMPEG_EXECUTABLE}")

    if not AUDIO_FOLDER.exists():
        AUDIO_FOLDER.mkdir(parents=True, exist_ok=True)
        print(f"[WARNING] Папка с аудио не существовала, создана: {AUDIO_FOLDER}")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    global current_target

    if member.id not in TRACKED_USER_IDS or member.bot:
        return
    # Мут, стрим и прочие обновления без смены канала.
    if before.channel == after.channel:
        return

    if after.channel is None:
        # Выход с сервера не должен оборвать заход на другой сервер:
        # тот уже записал свою цель.
        if current_target and current_target[0] == member.guild.id:
            current_target = None
        return

    channel = after.channel
    current_target = (channel.guild.id, channel.id)
    print(f"[EVENT] {member.display_name} зашёл в канал: {channel.name} ({channel.guild.name})")
    await follow(channel, current_target)


if __name__ == "__main__":
    if not FFMPEG_EXECUTABLE:
        raise SystemExit("[ERROR] ffmpeg не найден в PATH. Установите ffmpeg и добавьте его в PATH.")

    if not DISCORD_TOKEN:
        raise SystemExit("[ERROR] DISCORD_TOKEN не задан в .env")

    if not TRACKED_USER_IDS:
        print("[WARNING] Список отслеживаемых пользователей пуст!")

    bot.run(DISCORD_TOKEN)
