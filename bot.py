import hashlib
import os
import random
import threading
import time
from datetime import datetime, time as dt_time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from storage import store


# ============================================
# HTTP 서버 (Render 포트 감지용)
# ============================================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        if self.path == '/health':
            status = "연결됨" if bot.is_ready() else "연결중"
            self.wfile.write(f"Discord Bot 상태: {status}".encode('utf-8'))
        else:
            self.wfile.write("Discord Bot이 실행중입니다!".encode('utf-8'))

    def log_message(self, format, *args):
        return


def start_http_server():
    try:
        port = int(os.environ.get('PORT', 10000))
        server = HTTPServer(('0.0.0.0', port), SimpleHandler)
        print(f"HTTP server started on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"HTTP server error: {e}")


intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

KST = ZoneInfo(config.TIMEZONE)

# 임베드 색상
COLOR_NEUTRAL = discord.Color.from_str('#5865F2')
COLOR_WIN = discord.Color.from_str('#3BA55D')
COLOR_LOSE = discord.Color.from_str('#4E5058')
COLOR_ERROR = discord.Color.from_str('#ED4245')


# ============================================
# 놀이 잠금 (서버당 1명)
# ============================================
class PlayLock:
    """한 서버에서 한 번에 한 명만 놀이를 진행하도록 제한한다."""

    def __init__(self):
        # guild_id -> (user_id, 만료 시각)
        self._holders: Dict[int, Tuple[int, float]] = {}

    def holder(self, guild_id: int) -> Optional[int]:
        entry = self._holders.get(guild_id)
        if entry is None:
            return None
        user_id, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._holders[guild_id]
            return None
        return user_id

    def acquire(self, guild_id: int, user_id: int) -> Optional[int]:
        """잠금을 얻으면 None, 이미 사용 중이면 사용 중인 사용자 ID를 돌려준다."""
        current = self.holder(guild_id)
        if current is not None and current != user_id:
            return current
        self._holders[guild_id] = (user_id, time.monotonic() + config.PLAY_LOCK_TIMEOUT)
        return None

    def refresh(self, guild_id: int, user_id: int) -> None:
        if self.holder(guild_id) == user_id:
            self._holders[guild_id] = (user_id, time.monotonic() + config.PLAY_LOCK_TIMEOUT)

    def release(self, guild_id: int, user_id: int) -> None:
        if self._holders.get(guild_id, (None, 0.0))[0] == user_id:
            self._holders.pop(guild_id, None)


play_lock = PlayLock()


async def try_acquire(interaction: discord.Interaction) -> bool:
    """잠금을 시도하고, 실패하면 안내 메시지를 보낸 뒤 False를 돌려준다."""
    busy_user_id = play_lock.acquire(interaction.guild_id, interaction.user.id)
    if busy_user_id is None:
        return True

    member = interaction.guild.get_member(busy_user_id)
    name = member.display_name if member else f"<@{busy_user_id}>"
    await interaction.response.send_message(
        f"{name}님이 놀고 있어요. 다 놀때까지 기다려주세요.",
        ephemeral=True,
    )
    return False


# ============================================
# 공통 도구
# ============================================
def fmt(amount: int) -> str:
    return f"{amount:,}"


def roll() -> int:
    return random.randint(config.DICE_MIN, config.DICE_MAX)


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(description=message, color=COLOR_ERROR)


def elapsed_over_limit(started_at: float) -> bool:
    return (time.monotonic() - started_at) > config.MODAL_TIME_LIMIT


async def reply_timeout(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=error_embed(
            f"입력 제한 시간 {config.MODAL_TIME_LIMIT}초를 넘겨 종료되었습니다. 토큰 변동은 없습니다."
        ),
        ephemeral=True,
    )


class BaseModal(discord.ui.Modal):
    """처리 중 오류가 나면 잠금을 풀고 사용자에게 알린다."""

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        print(f"Modal error ({type(self).__name__}): {error}")
        if interaction.guild_id:
            play_lock.release(interaction.guild_id, interaction.user.id)

        message = "처리 중 오류가 발생했습니다. 토큰 변동은 없습니다."
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
            else:
                await interaction.followup.send(embed=error_embed(message), ephemeral=True)
        except discord.HTTPException:
            pass


# ============================================
# 1. 채널 추천
# ============================================
@bot.tree.command(name="채널추천", description="접속할 채널 번호를 하나 추천합니다.")
async def recommend_channel(interaction: discord.Interaction):
    number = random.randint(config.CHANNEL_MIN, config.CHANNEL_MAX)
    await interaction.response.send_message(f"{number}채널로 가세요!!")


# ============================================
# 2. 토큰 지급
# ============================================
def human_members(guild: discord.Guild) -> List[int]:
    return [m.id for m in guild.members if not m.bot]


async def ensure_account(guild_id: int, user_id: int) -> int:
    """계정이 없으면 만들고 현재 보유량을 돌려준다."""
    if not store.has_account(guild_id, user_id):
        await store.grant_initial(guild_id, [user_id])
    return store.get_balance(guild_id, user_id)


async def grant_initial_tokens(guild: discord.Guild) -> int:
    granted = await store.grant_initial(guild.id, human_members(guild))
    if granted:
        print(f"[tokens] {guild.name}: {granted}명에게 최초 {config.INITIAL_TOKENS} 토큰을 지급했습니다.")
    return granted


def today_kst() -> str:
    return datetime.now(KST).date().isoformat()


def topup_missed(guild_id: int) -> bool:
    """오늘 보정 시각이 지났는데 아직 오늘 보정을 하지 않았는지 확인한다."""
    now = datetime.now(KST)
    if now.hour < config.DAILY_RESET_HOUR:
        # 오늘 보정 시각이 아직 오지 않았다. 예정된 실행을 기다리면 된다.
        return False
    return store.get_last_topup(guild_id) != now.date().isoformat()


async def run_topup(guild: discord.Guild) -> None:
    members = human_members(guild)
    await store.grant_initial(guild.id, members)
    changed = await store.daily_topup(guild.id, members, today_kst())
    print(f"[tokens] {guild.name}: {changed}명의 보유량을 {config.DAILY_FLOOR}으로 맞췄습니다.")


@tasks.loop(time=dt_time(hour=config.DAILY_RESET_HOUR, tzinfo=KST))
async def daily_topup():
    """매일 지정 시각에 보유량이 기준선 미만인 인원을 기준선으로 맞춘다."""
    for guild in bot.guilds:
        try:
            await run_topup(guild)
        except Exception as e:
            print(f"Daily topup error ({guild.id}): {e}")


async def catch_up_topup() -> None:
    """봇이 보정 시각에 꺼져 있었으면 시작 직후에 한 번 따라잡는다.

    시간 지정 루프는 놓친 실행을 다시 하지 않으므로, 재시작 시점이 언제든
    그날 보정이 한 번은 이뤄지도록 여기서 확인한다.
    """
    for guild in bot.guilds:
        try:
            if topup_missed(guild.id):
                print(f"[tokens] {guild.name}: 오늘 보정 기록이 없어 지금 실행합니다.")
                await run_topup(guild)
        except Exception as e:
            print(f"Catch-up topup error ({guild.id}): {e}")


@daily_topup.before_loop
async def before_daily_topup():
    await bot.wait_until_ready()


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    try:
        await store.grant_initial(member.guild.id, [member.id])
    except Exception as e:
        print(f"Member join grant error: {e}")


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await grant_initial_tokens(guild)
    except Exception as e:
        print(f"Guild join grant error: {e}")


# ============================================
# 3. 혼자놀기
# ============================================
GAME_ODD_EVEN = '1'
GAME_NUMBER = '2'
GAME_NAMES = {GAME_ODD_EVEN: "홀짝 맞추기", GAME_NUMBER: "숫자 맞추기"}


class GameSelectModal(BaseModal, title="혼자놀기"):
    """진행할 게임을 고르는 첫 번째 단계."""

    def __init__(self):
        super().__init__()
        self.started_at = time.monotonic()

        self.add_item(discord.ui.TextDisplay(
            f"**1** 홀짝 맞추기 — 1~{config.DICE_MAX} 중 뽑힌 숫자가 홀수인지 짝수인지 맞춥니다. "
            f"정답 시 {fmt(config.ODD_EVEN_REWARD)} 토큰 지급.\n"
            f"**2** 숫자 맞추기 — 1~{config.DICE_MAX} 중 뽑힌 숫자를 맞춥니다. "
            f"정답 시 {fmt(config.NUMBER_REWARD)} 토큰 지급.\n\n"
            f"오답 시 {fmt(config.SOLO_BET)} 토큰이 회수됩니다.\n"
            f"## {config.MODAL_TIME_LIMIT}초 안에 입력을 완료하지 않으면 종료됩니다."
        ))

        self.choice = discord.ui.Select(
            placeholder="진행할 게임을 선택하세요",
            required=True,
            options=[
                discord.SelectOption(
                    label=GAME_NAMES[GAME_ODD_EVEN],
                    value=GAME_ODD_EVEN,
                    description=f"정답 시 {fmt(config.ODD_EVEN_REWARD)} 토큰 지급",
                ),
                discord.SelectOption(
                    label=GAME_NAMES[GAME_NUMBER],
                    value=GAME_NUMBER,
                    description=f"정답 시 {fmt(config.NUMBER_REWARD)} 토큰 지급",
                ),
            ],
        )
        self.add_item(discord.ui.Label(text="게임 선택", component=self.choice))

    async def on_submit(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild_id, interaction.user.id

        if elapsed_over_limit(self.started_at):
            play_lock.release(guild_id, user_id)
            await reply_timeout(interaction)
            return

        value = self.choice.values[0] if self.choice.values else ''
        if value not in GAME_NAMES:
            play_lock.release(guild_id, user_id)
            await interaction.response.send_message(
                embed=error_embed("게임을 선택해주세요. 토큰 변동은 없습니다."),
                ephemeral=True,
            )
            return

        play_lock.refresh(guild_id, user_id)
        view = SoloStartView(user_id, value)
        await interaction.response.send_message(
            embed=discord.Embed(
                title=GAME_NAMES[value],
                description=(
                    "아래 버튼을 누르면 입력창이 열립니다.\n"
                    f"입력창이 열린 뒤 {config.MODAL_TIME_LIMIT}초 안에 답을 제출해야 합니다."
                ),
                color=COLOR_NEUTRAL,
            ),
            view=view,
            ephemeral=True,
        )
        view.interaction = interaction


class SoloStartView(discord.ui.View):
    """모달 제출에 대한 응답으로는 모달을 띄울 수 없어 중간에 두는 버튼."""

    def __init__(self, user_id: int, game: str):
        super().__init__(timeout=config.BUTTON_TIME_LIMIT)
        self.user_id = user_id
        self.game = game
        self.interaction: Optional[discord.Interaction] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="게임 시작", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        play_lock.refresh(interaction.guild_id, self.user_id)
        modal = OddEvenModal() if self.game == GAME_ODD_EVEN else NumberModal()
        await interaction.response.send_modal(modal)
        self.stop()
        await self.clear_prompt()

    async def clear_prompt(self) -> None:
        """버튼이 달린 안내 메시지에서 버튼을 없앤다."""
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(
                embed=discord.Embed(description="입력창이 열렸습니다.", color=COLOR_NEUTRAL),
                view=None,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if self.interaction is None:
            return
        play_lock.release(self.interaction.guild_id, self.user_id)
        try:
            await self.interaction.edit_original_response(
                embed=error_embed("시간이 지나 종료되었습니다. 토큰 변동은 없습니다."),
                view=None,
            )
        except discord.HTTPException:
            pass


async def finish_solo_game(
    interaction: discord.Interaction,
    game: str,
    answer_text: str,
    correct: bool,
    number: int,
) -> None:
    """정산하고 결과를 본인에게 보여준 뒤 채널에 게시한다."""
    guild_id, user = interaction.guild_id, interaction.user

    await ensure_account(guild_id, user.id)

    reward = config.ODD_EVEN_REWARD if game == GAME_ODD_EVEN else config.NUMBER_REWARD
    delta = reward if correct else -config.SOLO_BET
    balance = await store.adjust(guild_id, user.id, delta)

    play_lock.release(guild_id, user.id)

    verdict = "정답!" if correct else "오답!"
    result_embed = discord.Embed(
        title=GAME_NAMES[game],
        description=f"# {number}\n# {verdict}",
        color=COLOR_WIN if correct else COLOR_LOSE,
    )
    result_embed.add_field(name="입력", value=answer_text, inline=True)
    result_embed.add_field(
        name="토큰",
        value=f"{'+' if delta > 0 else ''}{fmt(delta)}",
        inline=True,
    )
    result_embed.add_field(name="보유 토큰", value=fmt(balance), inline=True)

    await interaction.response.send_message(embed=result_embed, ephemeral=True)

    public_embed = discord.Embed(
        description=(
            f"{user.display_name}님이 {GAME_NAMES[game]}을(를) 진행했습니다.\n"
            f"뽑힌 숫자 **{number}** / 입력 **{answer_text}**\n"
            f"결과 **{verdict}**\n"
            f"남은 토큰 **{fmt(balance)}**"
        ),
        color=COLOR_WIN if correct else COLOR_LOSE,
    )
    try:
        await interaction.followup.send(embed=public_embed)
    except discord.HTTPException as e:
        print(f"Solo result post error: {e}")


class OddEvenModal(BaseModal, title="홀짝 맞추기"):
    def __init__(self):
        super().__init__()
        self.started_at = time.monotonic()

        self.add_item(discord.ui.TextDisplay(
            f"# 홀짝 맞추기\n"
            f"1~{config.DICE_MAX} 중 하나가 무작위로 뽑힙니다. 그 숫자가 홀수인지 짝수인지 맞추세요.\n"
            f"정답 시 {fmt(config.ODD_EVEN_REWARD)} 토큰 지급, 오답 시 {fmt(config.SOLO_BET)} 토큰 회수.\n"
            f"## {config.MODAL_TIME_LIMIT}초 안에 제출하지 않으면 종료됩니다."
        ))

        self.answer = discord.ui.Select(
            placeholder="짝 또는 홀을 선택하세요",
            required=True,
            options=[
                discord.SelectOption(label="짝", value="짝", description="2, 4, 6, 8, 10"),
                discord.SelectOption(label="홀", value="홀", description="1, 3, 5, 7, 9"),
            ],
        )
        self.add_item(discord.ui.Label(text="정답 선택", component=self.answer))

    async def on_submit(self, interaction: discord.Interaction):
        if elapsed_over_limit(self.started_at):
            play_lock.release(interaction.guild_id, interaction.user.id)
            await reply_timeout(interaction)
            return

        chosen = self.answer.values[0] if self.answer.values else ''
        if not chosen:
            play_lock.release(interaction.guild_id, interaction.user.id)
            await interaction.response.send_message(
                embed=error_embed("정답을 선택해주세요. 토큰 변동은 없습니다."),
                ephemeral=True,
            )
            return

        number = roll()
        actual = "짝" if number % 2 == 0 else "홀"
        await finish_solo_game(interaction, GAME_ODD_EVEN, chosen, chosen == actual, number)


class NumberModal(BaseModal, title="숫자 맞추기"):
    def __init__(self):
        super().__init__()
        self.started_at = time.monotonic()

        self.add_item(discord.ui.TextDisplay(
            f"# 숫자 맞추기\n"
            f"1~{config.DICE_MAX} 중 하나가 무작위로 뽑힙니다. 그 숫자를 맞추세요.\n"
            f"정답 시 {fmt(config.NUMBER_REWARD)} 토큰 지급, 오답 시 {fmt(config.SOLO_BET)} 토큰 회수.\n"
            f"## {config.MODAL_TIME_LIMIT}초 안에 제출하지 않으면 종료됩니다."
        ))

        self.answer = discord.ui.Select(
            placeholder="숫자를 선택하세요",
            required=True,
            options=[
                discord.SelectOption(label=str(n), value=str(n))
                for n in range(config.DICE_MIN, config.DICE_MAX + 1)
            ],
        )
        self.add_item(discord.ui.Label(text="정답 선택", component=self.answer))

    async def on_submit(self, interaction: discord.Interaction):
        if elapsed_over_limit(self.started_at):
            play_lock.release(interaction.guild_id, interaction.user.id)
            await reply_timeout(interaction)
            return

        chosen = self.answer.values[0] if self.answer.values else ''
        if not chosen:
            play_lock.release(interaction.guild_id, interaction.user.id)
            await interaction.response.send_message(
                embed=error_embed("숫자를 선택해주세요. 토큰 변동은 없습니다."),
                ephemeral=True,
            )
            return

        number = roll()
        await finish_solo_game(interaction, GAME_NUMBER, chosen, int(chosen) == number, number)


@bot.tree.command(name="혼자놀기", description="토큰을 걸고 혼자 하는 게임을 진행합니다.")
@app_commands.guild_only()
async def solo_play(interaction: discord.Interaction):
    balance = await ensure_account(interaction.guild_id, interaction.user.id)
    if balance < config.SOLO_BET:
        await interaction.response.send_message(
            embed=error_embed(
                f"보유 토큰이 {fmt(config.SOLO_BET)} 미만이라 진행할 수 없습니다. "
                f"현재 보유 {fmt(balance)} 토큰입니다.\n"
                f"매일 오전 {config.DAILY_RESET_HOUR}시에 {fmt(config.DAILY_FLOOR)} 토큰으로 보정됩니다."
            ),
            ephemeral=True,
        )
        return

    if not await try_acquire(interaction):
        return
    await interaction.response.send_modal(GameSelectModal())


# ============================================
# 4. 같이놀기
# ============================================
def bet_options(max_bet: int) -> List[int]:
    """걸 수 있는 금액 선택지. 사다리 값 중 한도 이하인 것들과 한도 자체를 합친다."""
    amounts = {a for a in config.DUO_BET_LADDER if a <= max_bet}
    if max_bet >= config.DUO_MIN_BET:
        amounts.add(max_bet)
    ordered = sorted(amounts)
    if len(ordered) > config.SELECT_MAX_OPTIONS:
        # 넘칠 일은 없지만, 넘치면 가장 큰 값(한도)은 반드시 남긴다.
        ordered = ordered[: config.SELECT_MAX_OPTIONS - 1] + [ordered[-1]]
    return ordered


def duo_invite_embed(
    challenger: discord.Member, target: discord.Member, amount: int
) -> discord.Embed:
    embed = discord.Embed(
        title="같이놀기 신청",
        description=(
            f"## 걸린 토큰 {fmt(amount)}\n"
            f"{challenger.mention} 대 {target.mention}\n\n"
            f"이기면 {fmt(amount)} 토큰을 얻고, 지면 {fmt(amount)} 토큰을 잃습니다."
        ),
        color=COLOR_NEUTRAL,
    )
    embed.set_footer(
        text=f"{target.display_name}님만 응답할 수 있습니다 · "
             f"{config.INVITE_TIME_LIMIT}초 내 무응답 시 자동 거절"
    )
    return embed


class DuoSetupModal(BaseModal, title="같이놀기"):
    def __init__(self, max_bet: int):
        super().__init__()
        self.started_at = time.monotonic()
        self.max_bet_hint = max_bet

        self.add_item(discord.ui.TextDisplay(
            f"상대와 각각 1~{config.DICE_MAX} 중 하나를 뽑아 더 높은 쪽이 이깁니다.\n"
            f"이긴 쪽은 건 토큰만큼 얻고, 진 쪽은 그만큼 잃습니다.\n"
            f"상대의 보유량이 내 보유량보다 적으면, 적은 쪽에 맞춰 다시 선택해야 합니다.\n"
            f"## {config.MODAL_TIME_LIMIT}초 안에 제출하지 않으면 종료됩니다."
        ))

        self.opponent = discord.ui.UserSelect(
            placeholder="같이 놀 상대를 선택하세요",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(discord.ui.Label(text="상대", component=self.opponent))

        self.bet = discord.ui.Select(
            placeholder="걸 토큰을 선택하세요",
            required=True,
            options=[
                discord.SelectOption(label=f"{fmt(amount)} 토큰", value=str(amount))
                for amount in bet_options(max_bet)
            ],
        )
        self.add_item(discord.ui.Label(
            text="걸 토큰",
            description=f"현재 보유량 기준 최대 {fmt(max_bet)} 토큰까지 걸 수 있습니다.",
            component=self.bet,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        guild_id, user = interaction.guild_id, interaction.user

        if elapsed_over_limit(self.started_at):
            play_lock.release(guild_id, user.id)
            await reply_timeout(interaction)
            return

        selected = self.opponent.values
        target = selected[0] if selected else None

        if target is None:
            await self.reject(interaction, "상대를 선택해주세요.")
            return
        if target.id == user.id:
            await self.reject(interaction, "자기 자신은 상대로 선택할 수 없습니다.")
            return
        if getattr(target, 'bot', False):
            await self.reject(interaction, "봇은 상대로 선택할 수 없습니다.")
            return

        my_balance = await ensure_account(guild_id, user.id)
        their_balance = await ensure_account(guild_id, target.id)
        max_bet = min(my_balance, their_balance) // config.DUO_UNIT * config.DUO_UNIT

        if max_bet < config.DUO_MIN_BET:
            short = "상대" if their_balance < my_balance else "내"
            await self.reject(
                interaction,
                f"{short} 보유 토큰이 {fmt(config.DUO_MIN_BET)} 미만이라 진행할 수 없습니다. "
                f"(내 보유 {fmt(my_balance)} / 상대 보유 {fmt(their_balance)})",
            )
            return

        chosen = self.bet.values[0] if self.bet.values else ''
        if not chosen.isdigit():
            await self.reject(interaction, "걸 토큰을 선택해주세요.")
            return

        amount = int(chosen)
        if amount > max_bet:
            # 상대의 보유량이 내 선택지 기준보다 적은 경우.
            await self.reject(
                interaction,
                f"{target.display_name}님의 보유량이 부족해 {fmt(amount)} 토큰은 걸 수 없습니다. "
                f"최대 {fmt(max_bet)} 토큰까지 가능합니다. "
                f"(내 보유 {fmt(my_balance)} / 상대 보유 {fmt(their_balance)})",
                max_bet=max_bet,
            )
            return

        play_lock.refresh(guild_id, user.id)

        view = DuoInviteView(challenger=user, target=target, amount=amount)
        await interaction.response.send_message(
            content=f"{target.mention} 대결 신청이 도착했습니다.",
            embed=duo_invite_embed(user, target, amount),
            view=view,
        )
        view.message = await interaction.original_response()

    async def reject(
        self, interaction: discord.Interaction, message: str, max_bet: Optional[int] = None
    ) -> None:
        """검증에 실패했을 때 사유와 다시 선택 버튼을 보여준다."""
        view = DuoRetryView(interaction.user.id, max_bet if max_bet is not None else self.max_bet_hint)
        await interaction.response.send_message(embed=error_embed(message), view=view, ephemeral=True)
        view.interaction = interaction
        play_lock.refresh(interaction.guild_id, interaction.user.id)


class DuoRetryView(discord.ui.View):
    def __init__(self, user_id: int, max_bet: int):
        super().__init__(timeout=config.BUTTON_TIME_LIMIT)
        self.user_id = user_id
        self.max_bet = max_bet
        self.interaction: Optional[discord.Interaction] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="다시 선택", style=discord.ButtonStyle.secondary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        play_lock.refresh(interaction.guild_id, self.user_id)
        await interaction.response.send_modal(DuoSetupModal(self.max_bet))
        self.stop()
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(
                embed=discord.Embed(description="입력창이 열렸습니다.", color=COLOR_NEUTRAL),
                view=None,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if self.interaction is None:
            return
        play_lock.release(self.interaction.guild_id, self.user_id)
        try:
            await self.interaction.edit_original_response(
                embed=error_embed("시간이 지나 종료되었습니다. 토큰 변동은 없습니다."),
                view=None,
            )
        except discord.HTTPException:
            pass


class DuoInviteView(discord.ui.View):
    def __init__(self, challenger: discord.Member, target: discord.Member, amount: int):
        super().__init__(timeout=config.INVITE_TIME_LIMIT)
        self.challenger = challenger
        self.target = target
        self.amount = amount
        self.message: Optional[discord.Message] = None
        self.resolved = False

        # 얼마가 걸린 판인지 버튼에서도 바로 보이게 한다.
        self.accept.label = f"수락 ({fmt(amount)} 토큰)"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message(
                "이 대결의 상대만 응답할 수 있습니다.", ephemeral=True
            )
            return False
        return True

    def release(self, guild_id: int) -> None:
        play_lock.release(guild_id, self.challenger.id)

    @discord.ui.button(label="수락", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.resolved = True
        self.stop()

        guild_id = interaction.guild_id
        my_balance = store.get_balance(guild_id, self.challenger.id)
        their_balance = store.get_balance(guild_id, self.target.id)

        if min(my_balance, their_balance) < self.amount:
            self.release(guild_id)
            await interaction.response.edit_message(
                embed=error_embed("보유 토큰이 부족해져 대결이 취소되었습니다. 토큰 변동은 없습니다."),
                view=None,
            )
            return

        # 무승부가 나오지 않도록 서로 다른 숫자가 나올 때까지 다시 뽑는다.
        my_roll, their_roll = roll(), roll()
        while my_roll == their_roll:
            my_roll, their_roll = roll(), roll()

        if my_roll > their_roll:
            winner, loser = self.challenger, self.target
        else:
            winner, loser = self.target, self.challenger

        winner_balance, loser_balance = await store.transfer(
            guild_id, winner.id, loser.id, self.amount
        )
        self.release(guild_id)

        balances = {winner.id: winner_balance, loser.id: loser_balance}
        embed = discord.Embed(
            title="같이놀기 결과",
            description=(
                f"# {self.challenger.display_name} : {my_roll}\n"
                f"# {self.target.display_name} : {their_roll}\n"
                f"# {winner.display_name} 승리!"
            ),
            color=COLOR_WIN,
        )
        embed.add_field(name="걸린 토큰", value=f"**{fmt(self.amount)}**", inline=True)
        embed.add_field(
            name=f"{self.challenger.display_name} 보유 토큰",
            value=fmt(balances[self.challenger.id]),
            inline=True,
        )
        embed.add_field(
            name=f"{self.target.display_name} 보유 토큰",
            value=fmt(balances[self.target.id]),
            inline=True,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="거절", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.resolved = True
        self.stop()
        self.release(interaction.guild_id)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="같이놀기 종료",
                description=f"{self.target.display_name}님이 거절했습니다. 토큰 변동은 없습니다.",
                color=COLOR_LOSE,
            ),
            view=None,
        )

    async def on_timeout(self):
        if self.resolved:
            return
        self.release(self.challenger.guild.id)
        if self.message is None:
            return
        try:
            await self.message.edit(
                embed=discord.Embed(
                    title="같이놀기 종료",
                    description=(
                        f"{config.INVITE_TIME_LIMIT}초 안에 응답이 없어 자동으로 거절되었습니다. "
                        "토큰 변동은 없습니다."
                    ),
                    color=COLOR_LOSE,
                ),
                view=None,
            )
        except discord.HTTPException:
            pass


@bot.tree.command(name="같이놀기", description="다른 인원과 토큰을 걸고 대결합니다.")
@app_commands.guild_only()
async def duo_play(interaction: discord.Interaction):
    balance = await ensure_account(interaction.guild_id, interaction.user.id)
    if balance < config.DUO_MIN_BET:
        await interaction.response.send_message(
            embed=error_embed(
                f"보유 토큰이 {fmt(config.DUO_MIN_BET)} 미만이라 진행할 수 없습니다. "
                f"현재 보유 {fmt(balance)} 토큰입니다.\n"
                f"매일 오전 {config.DAILY_RESET_HOUR}시에 {fmt(config.DAILY_FLOOR)} 토큰으로 보정됩니다."
            ),
            ephemeral=True,
        )
        return

    if not await try_acquire(interaction):
        return
    max_bet = balance // config.DUO_UNIT * config.DUO_UNIT
    await interaction.response.send_modal(DuoSetupModal(max_bet))


# ============================================
# 5. 토큰선물
# ============================================
def gift_received(amount: int) -> int:
    """선물한 금액 중 실제로 상대에게 들어가는 양."""
    return int(amount * config.GIFT_RATIO)


class GiftModal(BaseModal, title="토큰선물"):
    def __init__(self):
        super().__init__()

        self.add_item(discord.ui.TextDisplay(
            f"보유한 토큰을 다른 인원에게 보냅니다.\n"
            f"보내는 쪽은 선택한 금액이 그대로 차감되고, "
            f"받는 쪽에는 그 중 {int(config.GIFT_RATIO * 100)}%가 들어갑니다.\n"
            f"예를 들어 {fmt(config.GIFT_MIN)} 토큰을 보내면 "
            f"상대는 {fmt(gift_received(config.GIFT_MIN))} 토큰을 받습니다."
        ))

        self.target = discord.ui.UserSelect(
            placeholder="선물할 인원을 선택하세요",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(discord.ui.Label(text="받는 사람", component=self.target))

        self.amount = discord.ui.Select(
            placeholder="보낼 토큰을 선택하세요",
            required=True,
            options=[
                discord.SelectOption(
                    label=f"{fmt(value)} 토큰",
                    value=str(value),
                    description=f"상대는 {fmt(gift_received(value))} 토큰을 받습니다",
                )
                for value in range(config.GIFT_MIN, config.GIFT_MAX + 1, config.GIFT_STEP)
            ],
        )
        self.add_item(discord.ui.Label(text="보낼 토큰", component=self.amount))

    async def on_submit(self, interaction: discord.Interaction):
        guild_id, user = interaction.guild_id, interaction.user

        selected = self.target.values
        target = selected[0] if selected else None

        if target is None:
            await interaction.response.send_message(
                embed=error_embed("받는 사람을 선택해주세요."), ephemeral=True
            )
            return
        if target.id == user.id:
            await interaction.response.send_message(
                embed=error_embed("자기 자신에게는 선물할 수 없습니다."), ephemeral=True
            )
            return
        if getattr(target, 'bot', False):
            await interaction.response.send_message(
                embed=error_embed("봇에게는 선물할 수 없습니다."), ephemeral=True
            )
            return

        chosen = self.amount.values[0] if self.amount.values else ''
        if not chosen.isdigit():
            await interaction.response.send_message(
                embed=error_embed("보낼 토큰을 선택해주세요."), ephemeral=True
            )
            return

        amount = int(chosen)
        if not (config.GIFT_MIN <= amount <= config.GIFT_MAX):
            await interaction.response.send_message(
                embed=error_embed(
                    f"{fmt(config.GIFT_MIN)} ~ {fmt(config.GIFT_MAX)} 토큰만 선물할 수 있습니다."
                ),
                ephemeral=True,
            )
            return

        my_balance = await ensure_account(guild_id, user.id)
        await ensure_account(guild_id, target.id)

        if my_balance < amount:
            await interaction.response.send_message(
                embed=error_embed(
                    f"보유 토큰이 부족합니다. 현재 보유 {fmt(my_balance)} 토큰입니다."
                ),
                ephemeral=True,
            )
            return

        received = gift_received(amount)
        sender_balance, receiver_balance = await store.gift(
            guild_id, user.id, target.id, amount, received
        )

        embed = discord.Embed(
            title="토큰 선물",
            description=(
                f"## {fmt(received)} 토큰\n"
                f"{user.mention} → {target.mention}"
            ),
            color=COLOR_WIN,
        )
        embed.add_field(name="보낸 토큰", value=fmt(amount), inline=True)
        embed.add_field(name="받은 토큰", value=fmt(received), inline=True)
        embed.add_field(
            name=f"{user.display_name} 보유 토큰", value=fmt(sender_balance), inline=False
        )
        embed.add_field(
            name=f"{target.display_name} 보유 토큰", value=fmt(receiver_balance), inline=False
        )

        await interaction.response.send_message(content=target.mention, embed=embed)


@bot.tree.command(name="토큰선물", description="보유한 토큰을 다른 인원에게 선물합니다.")
@app_commands.guild_only()
async def gift_tokens(interaction: discord.Interaction):
    balance = await ensure_account(interaction.guild_id, interaction.user.id)
    if balance < config.GIFT_MIN:
        await interaction.response.send_message(
            embed=error_embed(
                f"보유 토큰이 {fmt(config.GIFT_MIN)} 미만이라 선물할 수 없습니다. "
                f"현재 보유 {fmt(balance)} 토큰입니다."
            ),
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(GiftModal())


# ============================================
# 6. 토큰보유
# ============================================
class BalanceModal(BaseModal, title="토큰보유"):
    def __init__(self):
        super().__init__()

        self.add_item(discord.ui.TextDisplay(
            "선택한 인원의 보유 토큰량을 확인합니다.\n"
            "서버에서 토큰을 가장 많이 보유한 5명도 함께 표시됩니다."
        ))

        self.target = discord.ui.UserSelect(
            placeholder="확인할 인원을 선택하세요",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(discord.ui.Label(text="대상", component=self.target))

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        selected = self.target.values
        target = selected[0] if selected else None

        if target is None:
            await interaction.response.send_message(
                embed=error_embed("대상을 선택해주세요."), ephemeral=True
            )
            return

        is_bot = getattr(target, 'bot', False)
        if not is_bot:
            await ensure_account(guild_id, target.id)

        lines = []
        for rank, (user_id, amount) in enumerate(store.top(guild_id, 5), start=1):
            member = interaction.guild.get_member(user_id)
            name = member.display_name if member else f"<@{user_id}>"
            lines.append(f"{rank}등 : {name} / 토큰 보유량 {fmt(amount)}")

        embed = discord.Embed(title="토큰 보유 현황", color=COLOR_NEUTRAL)
        embed.add_field(
            name="TOP 5",
            value="\n".join(lines) if lines else "기록이 없습니다.",
            inline=False,
        )

        if is_bot:
            target_value = "봇은 토큰을 보유하지 않습니다."
        else:
            target_value = (
                f"{target.display_name} / 토큰 보유량 "
                f"{fmt(store.get_balance(guild_id, target.id))}"
            )
        embed.add_field(name="선택한 대상", value=target_value, inline=False)

        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="토큰보유", description="선택한 인원의 보유 토큰량을 확인합니다.")
@app_commands.guild_only()
async def check_balance(interaction: discord.Interaction):
    await interaction.response.send_modal(BalanceModal())


# ============================================
# 이벤트
# ============================================
def command_signature() -> str:
    """등록된 명령어 목록의 지문. 내용이 바뀌면 값이 달라진다."""
    items = []
    for cmd in bot.tree.walk_commands():
        params = ",".join(f"{p.name}:{p.type}" for p in getattr(cmd, 'parameters', []))
        items.append(f"{cmd.name}|{getattr(cmd, 'description', '')}|{params}")
    return hashlib.sha256("\n".join(sorted(items)).encode('utf-8')).hexdigest()


def signature_path() -> str:
    return os.path.join(config.DATA_DIR, 'commands.sig')


def read_saved_signature() -> str:
    try:
        with open(signature_path(), 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def save_signature(signature: str) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(signature_path(), 'w', encoding='utf-8') as f:
            f.write(signature)
    except OSError as e:
        print(f'Signature save error: {e}')


async def sync_commands_if_changed() -> None:
    """명령어가 바뀌었을 때만 동기화한다.

    글로벌 동기화는 디스코드에서 강하게 제한하는 요청이라, 재배포마다 호출하면
    IP 단위로 차단(Cloudflare 1015)될 수 있다.
    """
    signature = command_signature()
    if not config.FORCE_SYNC and signature == read_saved_signature():
        print(f'Commands unchanged, skipping sync ({len(bot.tree.get_commands())} commands)')
        return

    try:
        synced = await bot.tree.sync()
        save_signature(signature)
        print(f'Synced {len(synced)} slash commands')
        for cmd in synced:
            print(f'  - /{cmd.name}: {cmd.description}')
    except Exception as e:
        print(f'Sync error: {e}')


@bot.event
async def on_ready():
    print('=== BOT READY EVENT TRIGGERED ===')
    print(f'Bot logged in as: {bot.user}')
    print(f'Bot ID: {bot.user.id}')
    print(f'Bot in {len(bot.guilds)} servers')

    await sync_commands_if_changed()

    for guild in bot.guilds:
        try:
            await grant_initial_tokens(guild)
        except Exception as e:
            print(f'Initial grant error ({guild.id}): {e}')

    await catch_up_topup()

    if not daily_topup.is_running():
        daily_topup.start()
        print(f'Daily topup scheduled at {config.DAILY_RESET_HOUR:02d}:00 {config.TIMEZONE}')

    print('=== BOT INITIALIZATION COMPLETE ===')


async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    print(f"App command error: {error}")
    if interaction.guild_id:
        play_lock.release(interaction.guild_id, interaction.user.id)

    message = "명령어 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        else:
            await interaction.followup.send(embed=error_embed(message), ephemeral=True)
    except discord.HTTPException:
        pass


bot.tree.on_error = on_app_command_error


# ============================================
# 봇 실행
# ============================================
if __name__ == "__main__":
    if config.DATA_IS_PERSISTENT:
        print(f"[storage] 퍼시스턴트 디스크에 저장합니다: {config.DATA_DIR}")
    else:
        print(
            f"[storage] 경고: {config.DATA_DIR} 은(는) 퍼시스턴트 디스크가 아닙니다. "
            "재배포·재시작 시 토큰 데이터가 사라집니다."
        )
    store.load()

    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()

    try:
        bot.run(config.DISCORD_TOKEN)
    except discord.LoginFailure as e:
        # 토큰이 잘못된 경우는 기다려도 달라지지 않는다. 바로 종료해 로그에 드러나게 한다.
        print(f"봇 실행 실패: 토큰이 올바르지 않습니다. DISCORD_TOKEN 환경변수를 확인하세요. ({e})")
        raise
    except Exception as e:
        # 곧바로 종료하면 호스팅 쪽에서 즉시 재시작하고, 그 재시도가 디스코드의
        # 속도 제한을 더 길게 만든다. 기다렸다가 종료해 재시도 간격을 벌린다.
        rate_limited = isinstance(e, discord.HTTPException) and e.status == 429
        wait = config.RATE_LIMIT_BACKOFF if rate_limited else config.RESTART_BACKOFF

        if rate_limited:
            print(
                "봇 실행 실패: 디스코드 속도 제한(429 / Cloudflare 1015)에 걸렸습니다. "
                "재시도를 계속하면 차단이 연장되므로 길게 대기합니다."
            )
        else:
            print(f"봇 실행 실패: {type(e).__name__}: {e}")

        print(f"{wait}초 후 종료합니다. (재시작 간격 확보)")
        time.sleep(wait)
        raise
