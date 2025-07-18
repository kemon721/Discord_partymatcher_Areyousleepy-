import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timedelta
import json
import config
import threading
from flask import Flask
import os
import aiohttp

# Flask 앱 생성 (keep-alive용)
app = Flask(__name__)

@app.route('/')
def home():
    return "Discord Bot is running!"

@app.route('/ping')
def ping():
    return "pong"

def run_flask():
    """Flask 서버를 별도 스레드에서 실행"""
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

# 인텐트 설정
intents = discord.Intents.default()
intents.message_content = True

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
                
                # Keep-alive 트리거 (응답 후에 실행)
                asyncio.create_task(trigger_keep_alive())
                
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
        
        # Keep-alive 트리거 (응답 후에 실행)
        asyncio.create_task(trigger_keep_alive())
    
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
        
        # Keep-alive 트리거 (응답 후에 실행)
        asyncio.create_task(trigger_keep_alive())
    
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
        
        # Keep-alive 트리거 (응답 후에 실행)
        asyncio.create_task(trigger_keep_alive())
    
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
        
        # Keep-alive 트리거 (응답 후에 실행)
        asyncio.create_task(trigger_keep_alive())

@bot.event
async def on_ready():
    print('[DEBUG] on_ready event triggered!')
    print(f'[DEBUG] Bot user: {bot.user}')
    print('[DEBUG] Starting bot initialization...')
    
    print(f'Discord Bot {bot.user} is now online!')
    print(f'Bot logged in successfully')
    
    print('[DEBUG] Starting slash command sync...')
    try:
        synced = await bot.tree.sync()
        print(f'Slash commands synced: {len(synced)} commands')
        print('[DEBUG] Slash command sync completed successfully')
    except Exception as e:
        print(f'[ERROR] Slash command sync failed: {e}')
        import traceback
        traceback.print_exc()
    
    print('[DEBUG] Starting notification checker...')
    try:
        # 알림 작업 시작
        if not check_notifications.is_running():
            check_notifications.start()
            print('Notification checker started successfully')
        else:
            print('Notification checker was already running')
        print('[DEBUG] Notification checker setup completed')
    except Exception as e:
        print(f'[ERROR] Failed to start notification checker: {e}')
        import traceback
        traceback.print_exc()
    
    print('[DEBUG] Starting keep-alive system...')
    try:
        # Keep-alive 작업 시작
        if not keep_alive.is_running():
            keep_alive.start()
            print('Keep-alive system started (runs every 25 minutes)')
        else:
            print('Keep-alive system was already running')
        print('[DEBUG] Keep-alive system setup completed')
    except Exception as e:
        print(f'[ERROR] Failed to start keep-alive system: {e}')
        import traceback
        traceback.print_exc()
    
    print('[DEBUG] Bot initialization completed successfully!')
    print('Bot initialization completed successfully!')

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
    
    # Keep-alive 트리거 (응답 후에 실행)
    asyncio.create_task(trigger_keep_alive())

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
    
    # Keep-alive 트리거 (응답 후에 실행)
    asyncio.create_task(trigger_keep_alive())

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
    
    # Keep-alive 트리거 (응답 후에 실행)
    asyncio.create_task(trigger_keep_alive())

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

@tasks.loop(minutes=25)
async def keep_alive():
    """25분마다 자신의 서버에 요청을 보내서 잠들지 않도록 함"""
    try:
        # Render URL 가져오기
        render_url = os.environ.get('RENDER_EXTERNAL_URL')
        if not render_url:
            # 환경변수가 없으면 기본 URL 사용
            render_url = "https://discord-partymatcher-areyousleepy.onrender.com"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{render_url}/ping") as response:
                if response.status == 200:
                    print(f"Keep-alive ping successful: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                else:
                    print(f"Keep-alive ping failed: {response.status}")
    except Exception as e:
        print(f"Keep-alive error: {e}")

async def trigger_keep_alive():
    """Discord 활동이 있을 때 즉시 keep-alive 실행"""
    try:
        render_url = os.environ.get('RENDER_EXTERNAL_URL')
        if not render_url:
            render_url = "https://discord-partymatcher-areyousleepy.onrender.com"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{render_url}/ping") as response:
                if response.status == 200:
                    print(f"Activity-based keep-alive successful: {datetime.now().strftime('%H:%M:%S')}")
    except Exception as e:
        print(f"Activity-based keep-alive error: {e}")

# 봇 실행
if __name__ == "__main__":
    # Flask 서버 시작
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    bot.run(config.DISCORD_TOKEN) 
