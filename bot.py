import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timedelta
import json
import config
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import aiohttp
import urllib.parse

# 간단한 HTTP 서버 (Render 포트 감지용)
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
        # HTTP 서버 로그 출력 비활성화
        return

def start_http_server():
    """HTTP 서버 시작 (Render 포트 감지용)"""
    try:
        port = int(os.environ.get('PORT', 10000))
        server = HTTPServer(('0.0.0.0', port), SimpleHandler)
        print(f"HTTP server started on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"HTTP server error: {e}")

# 인텐트 설정 - 모든 인텐트 활성화
intents = discord.Intents.all()

# 봇 인스턴스 생성
bot = commands.Bot(command_prefix='!', intents=intents)

# 파티 데이터 저장용 딕셔너리
parties = {}

# 사용자별 파티 참여 상태 추적 (user_id: party_message_id)
user_party_status = {}

class PartyData:
    def __init__(self, leader_id, purpose, departure_time, max_members, spec_cuts, notes):
        self.leader_id = leader_id
        self.purpose = purpose
        self.departure_time = departure_time
        self.max_members = max_members
        self.spec_cuts = spec_cuts
        self.notes = notes
        self.members = [leader_id]  # 파티장이 자동으로 포함
        self.channel_id = None
        self.message_id = None
        self.is_full = False
        self.is_completed = False
        self.notification_sent = False

class PartySetupModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="파티 설정")
        
        # 파티 목적
        self.purpose = discord.ui.TextInput(
            label="파티 목적",
            placeholder="파티의 목적을 입력해주세요 (예: 던전 클리어, 레이드 등)",
            required=True,
            max_length=100
        )
        
        # 출발 일시 (YYMMDD HH:MM 형식)
        self.departure_time = discord.ui.TextInput(
            label="출발 일시",
            placeholder="YYMMDD HH:MM (예: 250715 20:50)",
            required=True,
            max_length=14
        )
        
        # 인원수
        self.max_members = discord.ui.TextInput(
            label="총 인원수",
            placeholder=f"2~{config.MAX_PARTY_SIZE}명 사이로 입력해주세요",
            required=True,
            max_length=2
        )
        
        # 스펙컷
        self.spec_cuts = discord.ui.TextInput(
            label="스펙컷",
            placeholder="필요한 스펙을 입력해주세요 (줄바꿈으로 구분)",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500
        )
        
        # 비고
        self.notes = discord.ui.TextInput(
            label="비고",
            placeholder="추가 사항이 있으면 입력해주세요",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500
        )
        
        self.add_item(self.purpose)
        self.add_item(self.departure_time)
        self.add_item(self.max_members)
        self.add_item(self.spec_cuts)
        self.add_item(self.notes)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # 사용자가 이미 다른 파티에 참여중인지 확인
            if interaction.user.id in user_party_status:
                await interaction.response.send_message(
                    "이미 다른 파티에 참여중입니다. 한 번에 하나의 파티에만 참여할 수 있습니다.",
                    ephemeral=True
                )
                return
            
            # 출발 시간 파싱 (YYMMDD HH:MM 형식)
            datetime_input = self.departure_time.value.strip()
            date_time_parts = datetime_input.split(' ')
            if len(date_time_parts) != 2:
                raise ValueError("날짜와 시간을 공백으로 구분해주세요")
            
            date_part = date_time_parts[0]  # YYMMDD
            time_part = date_time_parts[1]  # HH:MM
            
            # 날짜 파싱 (YYMMDD)
            if len(date_part) != 6:
                raise ValueError("날짜는 YYMMDD 형식이어야 합니다")
            
            year = int(date_part[:2]) + 2000  # YY -> 20YY
            month = int(date_part[2:4])
            day = int(date_part[4:6])
            
            # 시간 파싱 (HH:MM)
            hour, minute = time_part.split(':')
            
            departure_dt = datetime(
                year,
                month,
                day,
                int(hour),
                int(minute)
            )
            
            # 과거 시간 체크
            if departure_dt < datetime.now():
                await interaction.response.send_message(
                    "출발 시간은 현재 시간보다 미래여야 합니다.",
                    ephemeral=True
                )
                return
            
            # 인원수 검증
            max_members = int(self.max_members.value)
            if max_members < config.MIN_PARTY_SIZE or max_members > config.MAX_PARTY_SIZE:
                await interaction.response.send_message(
                    f"인원수는 {config.MIN_PARTY_SIZE}~{config.MAX_PARTY_SIZE}명 사이여야 합니다.",
                    ephemeral=True
                )
                return
            
            # 스펙컷 리스트로 변환
            spec_cuts_list = [line.strip() for line in self.spec_cuts.value.split('\n') if line.strip()] if self.spec_cuts.value else []
            
            # 파티 데이터 생성
            party_data = PartyData(
                leader_id=interaction.user.id,
                purpose=self.purpose.value,
                departure_time=departure_dt,
                max_members=max_members,
                spec_cuts=spec_cuts_list,
                notes=self.notes.value
            )
            
            # 파티 임베드 생성 및 전송
            embed = create_party_embed(party_data, interaction.user)
            view = PartyView(party_data)
            
            await interaction.response.send_message(embed=embed, view=view)
            
            # 메시지 정보 저장
            try:
                message = await interaction.original_response()
                party_data.channel_id = interaction.channel.id
                party_data.message_id = message.id
                
                # 파티 데이터 저장
                parties[message.id] = party_data
                
                # 파티장 상태 업데이트
                user_party_status[interaction.user.id] = message.id
                
                # 파티장에게 관리 방법 안내
                await interaction.followup.send(
                    "🎉 **파티가 성공적으로 생성되었습니다!**\n\n"
                    "**파티 관리 방법:**\n"
                    "• **파티장 전용 버튼**: `✅ 파티완료`, `❌ 파티취소`\n"
                    "• **슬래시 명령어**: `/파티완료`, `/파티취소`\n\n"
                    "**파티원들은 파티원 전용 버튼을 사용할 수 있습니다:**\n"
                    "• `📥 참여하기`, `📤 나가기`\n\n"
                    "💡 **팁**: 버튼과 슬래시 명령어 모두 동일한 기능을 제공합니다!",
                    ephemeral=True
                )
                
            except Exception as e:
                print(f"Party setup post-processing error: {e}")
                # 이미 응답은 보냈으므로 사용자에게는 정상적으로 보임
                
        except ValueError as e:
            # interaction이 아직 응답되지 않은 경우에만 응답
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "입력한 날짜/시간 형식이 올바르지 않습니다. YYMMDD HH:MM 형식으로 입력해주세요. (예: 250715 20:50)",
                    ephemeral=True
                )
        except Exception as e:
            print(f"Party creation error: {e}")
            # interaction이 아직 응답되지 않은 경우에만 응답
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                    ephemeral=True
                )




def create_party_embed(party_data: PartyData, leader: discord.User):
    if party_data.is_completed:
        embed = discord.Embed(
            title="파티 모집 완료",
            description=f"**목적:** {party_data.purpose}",
            color=discord.Color.green()
        )
    elif party_data.is_full:
        embed = discord.Embed(
            title="파티 모집 마감",
            description=f"**목적:** {party_data.purpose}",
            color=discord.Color.orange()
        )
    else:
        embed = discord.Embed(
            title="파티 모집",
            description=f"**목적:** {party_data.purpose}",
            color=discord.Color.blue()
        )
    
    # 파티장 정보
    embed.add_field(
        name="파티장",
        value=leader.display_name if leader else f"<@{party_data.leader_id}>",
        inline=True
    )
    
    # 출발 시간
    embed.add_field(
        name="출발 시간",
        value=party_data.departure_time.strftime("%Y년 %m월 %d일 %H:%M"),
        inline=True
    )
    
    # 인원 현황
    current_members = len(party_data.members)
    embed.add_field(
        name="인원 현황",
        value=f"{current_members}/{party_data.max_members}명",
        inline=True
    )
    
    # 스펙컷
    if party_data.spec_cuts:
        spec_text = "\n".join([f"• {spec}" for spec in party_data.spec_cuts])
        embed.add_field(
            name="스펙컷",
            value=spec_text,
            inline=False
        )
    
    # 비고
    if party_data.notes:
        embed.add_field(
            name="비고",
            value=party_data.notes,
            inline=False
        )
    
    # 참여 멤버 목록
    if party_data.members:
        member_list = []
        for i, member_id in enumerate(party_data.members, 1):
            user = bot.get_user(member_id)
            role = "파티장" if member_id == party_data.leader_id else "멤버"
            
            if user:
                member_list.append(f"{i}. {user.display_name} ({role})")
            else:
                member_list.append(f"{i}. <@{member_id}> ({role})")
        
        embed.add_field(
            name="참여 멤버",
            value="\n".join(member_list),
            inline=False
        )
    
    return embed

class PartyView(discord.ui.View):
    def __init__(self, party_data: PartyData):
        super().__init__(timeout=None)
        self.party_data = party_data
        self.setup_buttons()
    
    def setup_buttons(self):
        # 파티가 완료된 경우 버튼 없음
        if self.party_data.is_completed:
            return
        
        # 파티원 전용: 참여하기 버튼
        join_button = discord.ui.Button(
            label="📥 참여하기 (파티원용)",
            style=discord.ButtonStyle.primary,
            custom_id="join_party"
        )
        join_button.callback = self.join_party
        self.add_item(join_button)
        
        # 파티원 전용: 나가기 버튼
        leave_button = discord.ui.Button(
            label="📤 나가기 (파티원용)",
            style=discord.ButtonStyle.secondary,
            custom_id="leave_party"
        )
        leave_button.callback = self.leave_party
        self.add_item(leave_button)
        
        # 파티장 전용: 파티완료 버튼
        complete_button = discord.ui.Button(
            label="✅ 파티완료 (파티장용)",
            style=discord.ButtonStyle.success,
            custom_id="complete_party"
        )
        complete_button.callback = self.complete_party
        self.add_item(complete_button)
        
        # 파티장 전용: 파티취소 버튼
        cancel_button = discord.ui.Button(
            label="❌ 파티취소 (파티장용)",
            style=discord.ButtonStyle.danger,
            custom_id="cancel_party"
        )
        cancel_button.callback = self.cancel_party
        self.add_item(cancel_button)
    
    async def join_party(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        
        # 파티장인 경우 제한
        if user_id == self.party_data.leader_id:
            await interaction.response.send_message(
                "🚫 **파티장은 파티원용 버튼을 사용할 수 없습니다.**\n"
                "파티 관리를 위해 **파티장 전용 버튼**을 사용해주세요.",
                ephemeral=True
            )
            return
        
        # 이미 다른 파티에 참여중인지 확인
        if user_id in user_party_status:
            await interaction.response.send_message("이미 다른 파티에 참여중입니다.", ephemeral=True)
            return
        
        # 이미 참여한 경우
        if user_id in self.party_data.members:
            await interaction.response.send_message("이미 파티에 참여하고 있습니다.", ephemeral=True)
            return
        
        # 파티가 가득 찬 경우
        if len(self.party_data.members) >= self.party_data.max_members:
            await interaction.response.send_message("파티 인원이 가득 찼습니다.", ephemeral=True)
            return
        
        # 파티 참여
        self.party_data.members.append(user_id)
        user_party_status[user_id] = self.party_data.message_id
        
        # 파티가 가득 찬 경우 상태 업데이트
        if len(self.party_data.members) >= self.party_data.max_members:
            self.party_data.is_full = True
        
        # 임베드 업데이트
        leader = bot.get_user(self.party_data.leader_id)
        embed = create_party_embed(self.party_data, leader)
        
        await interaction.response.edit_message(embed=embed, view=self)
        
    async def leave_party(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        
        # 파티장인 경우 제한
        if user_id == self.party_data.leader_id:
            await interaction.response.send_message(
                "🚫 **파티장은 파티원용 버튼을 사용할 수 없습니다.**\n"
                "파티 관리를 위해 **파티장 전용 버튼**을 사용해주세요.",
                ephemeral=True
            )
            return
        
        # 파티에 참여하지 않은 경우
        if user_id not in self.party_data.members:
            await interaction.response.send_message("파티에 참여하고 있지 않습니다.", ephemeral=True)
            return
        
        # 파티 나가기
        self.party_data.members.remove(user_id)
        if user_id in user_party_status:
            del user_party_status[user_id]
        
        # 파티가 가득 차지 않은 상태로 변경
        if self.party_data.is_full:
            self.party_data.is_full = False
        
        # 임베드 업데이트
        leader = bot.get_user(self.party_data.leader_id)
        embed = create_party_embed(self.party_data, leader)
        
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def complete_party(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        
        # 파티장 권한 확인
        if user_id != self.party_data.leader_id:
            await interaction.response.send_message(
                "🚫 **파티장만 사용할 수 있는 기능입니다.**\n"
                "파티원은 **파티원 전용 버튼**을 사용해주세요.",
                ephemeral=True
            )
            return
        
        # 이미 완료된 파티인지 확인
        if self.party_data.is_completed:
            await interaction.response.send_message("이미 완료된 파티입니다.", ephemeral=True)
            return
        
        # 파티 완료 처리
        await complete_party_function(interaction, self.party_data)
    
    async def cancel_party(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        
        # 파티장 권한 확인
        if user_id != self.party_data.leader_id:
            await interaction.response.send_message(
                "🚫 **파티장만 사용할 수 있는 기능입니다.**\n"
                "파티원은 **파티원 전용 버튼**을 사용해주세요.",
                ephemeral=True
            )
            return
        
        # 이미 완료된 파티인지 확인
        if self.party_data.is_completed:
            await interaction.response.send_message("이미 완료된 파티는 취소할 수 없습니다.", ephemeral=True)
            return
        
        # 버튼 클릭으로 파티 취소 처리 (메시지 직접 삭제)
        await cancel_party_by_button(interaction, self.party_data)

@bot.event
async def on_ready():
    print('=== BOT READY EVENT TRIGGERED ===')
    print(f'Bot logged in as: {bot.user}')
    print(f'Bot ID: {bot.user.id}')
    print(f'Bot in {len(bot.guilds)} servers')
    
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} slash commands')
        for cmd in synced:
            print(f'  - /{cmd.name}: {cmd.description}')
    except Exception as e:
        print(f'Sync error: {e}')
    
    try:
        check_notifications.start()
        print('Notification checker started')
    except Exception as e:
        print(f'Notification checker error: {e}')
    
    print('=== BOT INITIALIZATION COMPLETE ===')

@bot.event
async def on_command_error(ctx, error):
    """명령어 에러 핸들링"""
    print(f"Command error: {error}")

@bot.event  
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    """슬래시 명령어 에러 핸들링"""
    print(f"App command error: {error}")
    
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "❌ 명령어 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            "❌ 명령어 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True
        )

@bot.tree.command(name="파티매칭", description="파티 모집을 시작합니다.")
async def party_matching(interaction: discord.Interaction):
    # 이미 파티에 참여중인지 확인
    if interaction.user.id in user_party_status:
        await interaction.response.send_message(
            "이미 파티에 참여중입니다. 한 번에 하나의 파티에만 참여할 수 있습니다.",
            ephemeral=True
        )
        return
    
    modal = PartySetupModal()
    await interaction.response.send_modal(modal)

@bot.tree.command(name="파티완료", description="파티장만 사용 가능: 파티 활동을 완료 처리합니다.")
async def complete_party_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    
    # 사용자가 파티에 참여중인지 확인
    if user_id not in user_party_status:
        await interaction.response.send_message("참여중인 파티가 없습니다.", ephemeral=True)
        return
    
    party_message_id = user_party_status[user_id]
    if party_message_id not in parties:
        await interaction.response.send_message("파티 정보를 찾을 수 없습니다.", ephemeral=True)
        return
    
    party_data = parties[party_message_id]
    
    # 파티장 권한 확인
    if user_id != party_data.leader_id:
        await interaction.response.send_message("파티장만 활동 완료 처리를 할 수 있습니다.", ephemeral=True)
        return
    
    # 이미 완료된 파티인지 확인
    if party_data.is_completed:
        await interaction.response.send_message("이미 완료된 파티입니다.", ephemeral=True)
        return
    
    # 파티 완료 처리
    await complete_party_function(interaction, party_data)

@bot.tree.command(name="파티취소", description="파티장만 사용 가능: 파티 모집을 취소합니다.")
async def disband_party_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    
    # 사용자가 파티에 참여중인지 확인
    if user_id not in user_party_status:
        await interaction.response.send_message("참여중인 파티가 없습니다.", ephemeral=True)
        return
    
    party_message_id = user_party_status[user_id]
    if party_message_id not in parties:
        await interaction.response.send_message("파티 정보를 찾을 수 없습니다.", ephemeral=True)
        return
    
    party_data = parties[party_message_id]
    
    # 파티장 권한 확인
    if user_id != party_data.leader_id:
        await interaction.response.send_message("파티장만 모집을 취소할 수 있습니다.", ephemeral=True)
        return
    
    # 이미 완료된 파티인지 확인
    if party_data.is_completed:
        await interaction.response.send_message("이미 완료된 파티는 취소할 수 없습니다.", ephemeral=True)
        return
    
    # 파티 취소 처리
    await disband_party_function(interaction, party_data)

async def complete_party_function(interaction: discord.Interaction, party_data: PartyData):
    """파티 완료 처리 함수"""
    completion_time = datetime.now()
    
    # 파티 완료 상태로 변경
    party_data.is_completed = True
    
    # 모든 멤버의 파티 상태 해제
    for member_id in party_data.members:
        if member_id in user_party_status:
            del user_party_status[member_id]
    
    # 원본 메시지 업데이트
    try:
        channel = bot.get_channel(party_data.channel_id)
        message = await channel.fetch_message(party_data.message_id)
        leader = bot.get_user(party_data.leader_id)
        embed = create_party_embed(party_data, leader)
        await message.edit(embed=embed, view=None)
    except:
        pass  # 메시지를 찾을 수 없는 경우 무시
    
    # 파티 완료 기록을 채널에 남김
    completion_embed = discord.Embed(
        title="파티 활동 완료 기록",
        description=f"**{party_data.purpose}** 파티가 성공적으로 완료되었습니다!",
        color=discord.Color.green(),
        timestamp=completion_time
    )
    
    # 파티 정보
    leader = bot.get_user(party_data.leader_id)
    completion_embed.add_field(
        name="파티장",
        value=leader.display_name if leader else f"<@{party_data.leader_id}>",
        inline=True
    )
    
    completion_embed.add_field(
        name="출발 시간",
        value=party_data.departure_time.strftime("%Y년 %m월 %d일 %H:%M"),
        inline=True
    )
    
    completion_embed.add_field(
        name="완료 시간",
        value=completion_time.strftime("%Y년 %m월 %d일 %H:%M"),
        inline=True
    )
    
    # 참여 멤버 목록
    member_list = []
    for i, member_id in enumerate(party_data.members, 1):
        user = bot.get_user(member_id)
        role = "파티장" if member_id == party_data.leader_id else "멤버"
        
        if user:
            member_list.append(f"{i}. {user.display_name} ({role})")
        else:
            member_list.append(f"{i}. <@{member_id}> ({role})")
    
    completion_embed.add_field(
        name=f"참여 멤버 ({len(party_data.members)}명)",
        value="\n".join(member_list),
        inline=False
    )
    
    # 스펙컷이 있었다면 기록
    if party_data.spec_cuts:
        spec_text = "\n".join([f"• {spec}" for spec in party_data.spec_cuts])
        completion_embed.add_field(
            name="스펙컷",
            value=spec_text,
            inline=False
        )
    
    # 활동 시간 계산
    activity_duration = completion_time - party_data.departure_time
    hours = int(activity_duration.total_seconds() // 3600)
    minutes = int((activity_duration.total_seconds() % 3600) // 60)
    
    if hours > 0:
        duration_text = f"{hours}시간 {minutes}분"
    else:
        duration_text = f"{minutes}분"
    
    completion_embed.add_field(
        name="활동 시간",
        value=duration_text,
        inline=True
    )
    
    completion_embed.set_footer(text="파티 시스템에 의해 자동 기록됨")
    
    # 완료 기록을 채널에 전송
    await interaction.response.send_message(embed=completion_embed)
    
    # 파티 데이터에서 제거 (기록은 이미 채널에 남겨짐)
    if party_data.message_id in parties:
        del parties[party_data.message_id]

async def cancel_party_by_button(interaction: discord.Interaction, party_data: PartyData):
    """버튼 클릭으로 파티 취소 처리 (메시지 직접 삭제)"""
    
    # 파티원들에게 취소 알림 전송 (파티장 제외)
    party_members = [member_id for member_id in party_data.members if member_id != party_data.leader_id]
    
    if party_members:
        try:
            # 파티원들에게 DM으로 알림
            for member_id in party_members:
                user = bot.get_user(member_id)
                if user:
                    try:
                        await user.send(
                            f"📢 **파티 취소 알림**\n\n"
                            f"참여하고 계신 **'{party_data.purpose}'** 파티가 파티장에 의해 취소되었습니다.\n"
                            f"출발 예정 시간: {party_data.departure_time.strftime('%Y년 %m월 %d일 %H:%M')}"
                        )
                    except:
                        # DM 전송 실패 시 무시 (DM 차단된 경우 등)
                        pass
        except Exception as e:
            print(f"Party disband notification sending error: {e}")
    
    # 모든 멤버의 파티 상태 해제
    for member_id in party_data.members:
        if member_id in user_party_status:
            del user_party_status[member_id]
    
    # 파티 데이터 삭제
    if party_data.message_id in parties:
        del parties[party_data.message_id]
    
    try:
        # 파티장에게 취소 완료 응답 (먼저 응답)
        cancel_message = f"✅ **'{party_data.purpose}'** 파티 모집이 취소되었습니다.\n모집창을 삭제합니다."
        
        if party_members:
            cancel_message += f"\n📨 파티원 {len(party_members)}명에게 취소 알림을 전송했습니다."
        
        await interaction.response.send_message(cancel_message, ephemeral=True)
        
        # 그 다음 메시지 삭제 (버튼이 있는 원본 메시지)
        await interaction.message.delete()
        
    except Exception as e:
        print(f"Button party disband error: {e}")
        # 메시지 삭제 실패 시 대체 응답
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "파티 모집이 취소되었습니다.",
                    ephemeral=True
                )
        except:
            pass

async def disband_party_function(interaction: discord.Interaction, party_data: PartyData):
    """파티 취소 처리 함수 (슬래시 명령어용)"""
    
    # 파티원들에게 취소 알림 전송 (파티장 제외)
    party_members = [member_id for member_id in party_data.members if member_id != party_data.leader_id]
    
    if party_members:
        try:
            # 파티원들에게 DM으로 알림
            for member_id in party_members:
                user = bot.get_user(member_id)
                if user:
                    try:
                        await user.send(
                            f"📢 **파티 취소 알림**\n\n"
                            f"참여하고 계신 **'{party_data.purpose}'** 파티가 파티장에 의해 취소되었습니다.\n"
                            f"출발 예정 시간: {party_data.departure_time.strftime('%Y년 %m월 %d일 %H:%M')}"
                        )
                    except:
                        # DM 전송 실패 시 무시 (DM 차단된 경우 등)
                        pass
        except Exception as e:
            print(f"Party disband notification sending error: {e}")
    
    # 모든 멤버의 파티 상태 해제
    for member_id in party_data.members:
        if member_id in user_party_status:
            del user_party_status[member_id]
    
    # 파티 데이터 삭제
    if party_data.message_id in parties:
        del parties[party_data.message_id]
    
    # 원본 메시지 삭제
    try:
        channel = bot.get_channel(party_data.channel_id)
        message = await channel.fetch_message(party_data.message_id)
        
        # 메시지 완전 삭제
        await message.delete()
        
        # 취소 완료 응답
        cancel_message = f"✅ **'{party_data.purpose}'** 파티 모집이 취소되었습니다.\n모집창이 삭제되었습니다."
        
        if party_members:
            cancel_message += f"\n📨 파티원 {len(party_members)}명에게 취소 알림을 전송했습니다."
        
        await interaction.response.send_message(cancel_message, ephemeral=True)
        
    except Exception as e:
        print(f"Party disband error: {e}")
        # 메시지 삭제 실패 시 대체 응답
        await interaction.response.send_message(
            "파티 모집이 취소되었습니다.",
            ephemeral=True
        )

@tasks.loop(minutes=1)
async def check_notifications():
    """1분마다 실행되어 출발 시간 10분 전 알림을 확인"""
    current_time = datetime.now()
    
    for message_id, party_data in parties.items():
        if party_data.departure_time and not party_data.notification_sent and not party_data.is_completed:
            time_diff = party_data.departure_time - current_time
            
            # 출발 시간 10분 전이고 아직 알림을 보내지 않은 경우
            if timedelta(minutes=9) <= time_diff <= timedelta(minutes=10):
                try:
                    channel = bot.get_channel(party_data.channel_id)
                    if channel:
                        # 참여 멤버들에게 알림
                        mentions = " ".join([f"<@{member_id}>" for member_id in party_data.members])
                        await channel.send(
                            f"**파티 출발 알림**\n"
                            f"{mentions}\n"
                            f"'{party_data.purpose}' 파티가 10분 후 출발합니다!\n"
                            f"출발 시간: {party_data.departure_time.strftime('%Y년 %m월 %d일 %H:%M')}"
                        )
                        party_data.notification_sent = True
                except Exception as e:
                    print(f"Notification sending error: {e}")

# ============================================
# 마비노기 경매장 기능
# ============================================

# 마비노기 아이템 카테고리 리스트
MABINOGI_CATEGORIES = [
    "개조석", "검", "경갑옷", "기타", "기타 소모품", "기타 스크롤", "기타 장비", 
    "기타 재료", "꼬리", "날개", "낭만농장/달빛섬", "너클", "던전 통행증", "도끼", 
    "도면", "둔기", "듀얼건", "랜스", "로브", "마기그래프", "마기그래프 도안", 
    "마도서", "마리오네트", "마법가루", "마비노벨", "마족 스크롤", "말풍선 스티커", 
    "매직 크래프트", "모자/가발", "방패", "변신 메달", "보석", "분양 메달", 
    "불타래", "뷰티 쿠폰", "생활 도구", "석궁", "수리검", "스케치", "스태프", 
    "신발", "실린더", "아틀라틀", "악기", "알반 훈련석", "액세서리", "양손 장비", 
    "얼굴 장식", "에이도스", "에코스톤", "염색 앰플", "오브", "옷본", 
    "원거리 소모품", "원드", "음식", "의자/사물", "인챈트 스크롤", "장갑", 
    "제련/블랙스미스", "제스처", "주머니", "중갑옷", "책", "천옷", "천옷/방직", 
    "체인 블레이드", "토템", "팔리아스 유물", "퍼퓸", "페이지", "포션", 
    "피니 펫", "핀즈비즈", "한손 장비", "핸들", "허브", "활", "힐웬 공학"
]

async def call_mabinogi_api(endpoint: str, params: dict = None):
    """마비노기 API 호출 함수"""
    url = f"{config.MABINOGI_API_BASE_URL}{endpoint}"
    headers = {
        "x-nxopen-api-key": config.MABINOGI_API_KEY,
        "Content-Type": "application/json"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    print(f"API Error: {response.status} - {await response.text()}")
                    return None
    except Exception as e:
        print(f"API Exception: {e}")
        return None

async def search_auction_items(item_name: str = None, category: str = None, keyword: str = None, cursor: str = ""):
    """경매장 아이템 검색"""
    params = {"cursor": cursor}
    
    if keyword:
        endpoint = "/mabinogi/v1/auction/keyword-search"
        params["keyword"] = keyword
    else:
        endpoint = "/mabinogi/v1/auction/list"
        if item_name:
            params["item_name"] = item_name
        if category:
            params["auction_item_category"] = category
    
    return await call_mabinogi_api(endpoint, params)

async def search_auction_history(item_name: str = None, category: str = None, cursor: str = ""):
    """경매장 거래 내역 조회"""
    params = {"cursor": cursor}
    
    if item_name:
        params["item_name"] = item_name
    if category:
        params["auction_item_category"] = category
    
    return await call_mabinogi_api("/mabinogi/v1/auction/history", params)

class AuctionSearchModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="마비노기 경매장 검색")
        
        # 검색 방식 선택 (아이템명/키워드)
        self.search_type = discord.ui.TextInput(
            label="검색 방식",
            placeholder="1: 아이템명 검색, 2: 키워드 검색, 3: 거래내역 조회",
            required=True,
            max_length=1
        )
        
        # 검색어
        self.search_term = discord.ui.TextInput(
            label="검색어",
            placeholder="검색할 아이템명 또는 키워드를 입력하세요",
            required=True,
            max_length=100
        )
        
        # 카테고리 (선택사항)
        self.category = discord.ui.TextInput(
            label="카테고리 (선택사항)",
            placeholder="예: 검, 방패, 포션 등 (빈칸 가능)",
            required=False,
            max_length=50
        )
        
        self.add_item(self.search_type)
        self.add_item(self.search_term)
        self.add_item(self.category)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            search_type = self.search_type.value.strip()
            search_term = self.search_term.value.strip()
            category = self.category.value.strip() if self.category.value.strip() else None
            
            # 검색 방식 유효성 검사
            if search_type not in ['1', '2', '3']:
                await interaction.response.send_message(
                    "❌ 검색 방식은 1(아이템명), 2(키워드), 3(거래내역) 중 하나를 입력해주세요.",
                    ephemeral=True
                )
                return
            
            # 카테고리 유효성 검사
            if category and category not in MABINOGI_CATEGORIES:
                await interaction.response.send_message(
                    f"❌ 올바르지 않은 카테고리입니다.\n"
                    f"**사용 가능한 카테고리:** {', '.join(MABINOGI_CATEGORIES[:10])}...",
                    ephemeral=True
                )
                return
            
            await interaction.response.defer()
            
            # API 호출
            if search_type == '1':  # 아이템명 검색
                result = await search_auction_items(item_name=search_term, category=category)
            elif search_type == '2':  # 키워드 검색
                result = await search_auction_items(keyword=search_term, category=category)
            else:  # 거래내역 조회
                result = await search_auction_history(item_name=search_term, category=category)
            
            if not result:
                await interaction.followup.send("❌ API 호출에 실패했습니다. 잠시 후 다시 시도해주세요.")
                return
            
            # 결과가 없는 경우
            items_key = "auction_item" if search_type != '3' else "auction_history"
            items = result.get(items_key, [])
            
            if not items:
                search_type_text = "아이템명" if search_type == '1' else "키워드" if search_type == '2' else "거래내역"
                await interaction.followup.send(f"🔍 **{search_type_text} 검색 결과**\n검색어: `{search_term}`\n\n❌ 검색 결과가 없습니다.")
                return
            
            # 결과 표시
            embed = create_auction_embed(items, search_term, search_type, 0)
            view = AuctionView(items, search_term, search_type, result.get("next_cursor"))
            
            await interaction.followup.send(embed=embed, view=view)
            
        except Exception as e:
            print(f"Auction search error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ 검색 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                    ephemeral=True
                )

def create_auction_embed(items: list, search_term: str, search_type: str, page: int):
    """경매장 검색 결과 임베드 생성"""
    search_type_text = "아이템명" if search_type == '1' else "키워드" if search_type == '2' else "거래내역"
    
    if search_type == '3':  # 거래내역
        embed = discord.Embed(
            title="💰 마비노기 경매장 거래내역",
            description=f"**{search_type_text} 검색:** `{search_term}`",
            color=discord.Color.gold()
        )
    else:  # 현재 매물
        embed = discord.Embed(
            title="🏪 마비노기 경매장 검색",
            description=f"**{search_type_text} 검색:** `{search_term}`",
            color=discord.Color.blue()
        )
    
    # 페이지 처리 (한 페이지에 5개씩)
    items_per_page = 5
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    page_items = items[start_idx:end_idx]
    
    if not page_items:
        embed.add_field(
            name="❌ 검색 결과 없음",
            value="해당 페이지에 표시할 아이템이 없습니다.",
            inline=False
        )
        return embed
    
    for i, item in enumerate(page_items, 1):
        # 가격 포맷팅
        price = item.get('auction_price_per_unit', 0)
        price_text = f"{price:,}골드"
        
        # 만료/거래 시간
        if search_type == '3':  # 거래내역
            time_field = item.get('date_auction_buy', '')
            time_text = f"거래시간: {time_field.replace('T', ' ').replace('Z', ' UTC')}"
        else:  # 현재 매물
            time_field = item.get('date_auction_expire', '')
            time_text = f"만료시간: {time_field.replace('T', ' ').replace('Z', ' UTC')}"
        
        # 아이템 정보
        item_name = item.get('item_display_name', item.get('item_name', '알 수 없음'))
        item_count = item.get('item_count', 1)
        category = item.get('auction_item_category', '기타')
        
        count_text = f" x{item_count}" if item_count > 1 else ""
        
        embed.add_field(
            name=f"{start_idx + i}. {item_name}{count_text}",
            value=f"💰 **{price_text}** (개당)\n"
                  f"📁 카테고리: {category}\n"
                  f"⏰ {time_text}",
            inline=False
        )
    
    # 페이지 정보
    total_pages = (len(items) - 1) // items_per_page + 1
    embed.set_footer(text=f"페이지 {page + 1}/{total_pages} • 총 {len(items)}개 아이템")
    
    return embed

class QuickAuctionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="🗡️ 검 검색", style=discord.ButtonStyle.primary)
    async def search_sword(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.quick_search(interaction, "검", "검")
    
    @discord.ui.button(label="🛡️ 방패 검색", style=discord.ButtonStyle.primary)
    async def search_shield(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.quick_search(interaction, "방패", "방패")
    
    @discord.ui.button(label="🧪 포션 검색", style=discord.ButtonStyle.primary)
    async def search_potion(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.quick_search(interaction, "포션", "포션")
    
    @discord.ui.button(label="📜 인챈트 스크롤", style=discord.ButtonStyle.secondary)
    async def search_enchant(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.quick_search(interaction, "인챈트", "인챈트 스크롤")
    
    async def quick_search(self, interaction: discord.Interaction, keyword: str, category: str = None):
        await interaction.response.defer()
        
        try:
            result = await search_auction_items(keyword=keyword, category=category)
            
            if not result:
                await interaction.followup.send("❌ API 호출에 실패했습니다.")
                return
            
            items = result.get("auction_item", [])
            
            if not items:
                await interaction.followup.send(f"🔍 **{keyword} 검색 결과**\n\n❌ 검색 결과가 없습니다.")
                return
            
            embed = create_auction_embed(items, keyword, '2', 0)
            view = AuctionView(items, keyword, '2', result.get("next_cursor"))
            
            await interaction.followup.send(embed=embed, view=view)
            
        except Exception as e:
            print(f"Quick search error: {e}")
            await interaction.followup.send("❌ 검색 중 오류가 발생했습니다.")

class AuctionView(discord.ui.View):
    def __init__(self, items: list, search_term: str, search_type: str, next_cursor: str = None):
        super().__init__(timeout=300)  # 5분 타임아웃
        self.items = items
        self.search_term = search_term
        self.search_type = search_type
        self.next_cursor = next_cursor
        self.current_page = 0
        self.items_per_page = 5
        self.total_pages = (len(items) - 1) // self.items_per_page + 1
        
        # 페이지가 1페이지뿐이면 이전/다음 버튼 비활성화
        if self.total_pages <= 1:
            self.prev_button.disabled = True
            self.next_button.disabled = True
    
    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            embed = create_auction_embed(self.items, self.search_term, self.search_type, self.current_page)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message("❌ 첫 번째 페이지입니다.", ephemeral=True)
    
    @discord.ui.button(label="▶ 다음", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            embed = create_auction_embed(self.items, self.search_term, self.search_type, self.current_page)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message("❌ 마지막 페이지입니다.", ephemeral=True)
    
    @discord.ui.button(label="🔄 새로고침", style=discord.ButtonStyle.primary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        try:
            # API 재호출
            if self.search_type == '1':  # 아이템명 검색
                result = await search_auction_items(item_name=self.search_term)
            elif self.search_type == '2':  # 키워드 검색
                result = await search_auction_items(keyword=self.search_term)
            else:  # 거래내역 조회
                result = await search_auction_history(item_name=self.search_term)
            
            if result:
                items_key = "auction_item" if self.search_type != '3' else "auction_history"
                self.items = result.get(items_key, [])
                self.next_cursor = result.get("next_cursor")
                self.current_page = 0  # 첫 페이지로 리셋
                self.total_pages = (len(self.items) - 1) // self.items_per_page + 1
                
                embed = create_auction_embed(self.items, self.search_term, self.search_type, self.current_page)
                await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)
            else:
                await interaction.followup.send("❌ 새로고침에 실패했습니다.", ephemeral=True)
                
        except Exception as e:
            print(f"Refresh error: {e}")
            await interaction.followup.send("❌ 새로고침 중 오류가 발생했습니다.", ephemeral=True)

@bot.tree.command(name="경매장테스트", description="경매장 기능 테스트")
async def auction_test(interaction: discord.Interaction):
    """경매장 기능 테스트용 명령어"""
    await interaction.response.send_message(
        "✅ **경매장 기능 테스트 성공!**\n\n"
        "기본 상호작용이 정상 작동합니다.\n"
        "이제 `/경매장` 명령어를 시도해보세요!",
        ephemeral=True
    )

@bot.tree.command(name="경매장", description="마비노기 경매장에서 아이템을 검색합니다.")
async def auction_search(interaction: discord.Interaction):
    try:
        modal = AuctionSearchModal()
        await interaction.response.send_modal(modal)
    except discord.errors.NotFound:
        # Interaction이 만료된 경우 대체 응답
        await interaction.followup.send(
            "❌ 상호작용이 만료되었습니다. 명령어를 다시 시도해주세요.",
            ephemeral=True
        )
    except Exception as e:
        print(f"Auction command error: {e}")
        # 모달 전송 실패 시 대체 방법 제공
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "🏪 **마비노기 경매장 검색 (임시 버전)**\n\n"
                "현재 모달창에 문제가 있어 임시로 이 방식을 사용합니다.\n"
                "아래 버튼을 눌러 검색해보세요!",
                view=QuickAuctionView(),
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "❌ 경매장 기능에 문제가 발생했습니다. 관리자에게 문의해주세요.",
                ephemeral=True
            )

# ============================================
# 봇 실행
# ============================================

# 봇 실행
if __name__ == "__main__":
    # HTTP 서버 시작
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()

    bot.run(config.DISCORD_TOKEN) 

    
