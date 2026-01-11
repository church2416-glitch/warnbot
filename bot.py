import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import sqlite3
import datetime
import os
import asyncio

# --- 봇 기본 설정 ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True 
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler()

# --- 데이터베이스 연결 ---
conn = sqlite3.connect("warnings.db")
cur = conn.cursor()

# 테이블 생성
cur.execute('''CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    reason TEXT,
    expires_at INTEGER,
    active INTEGER
)''')

cur.execute('''CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER PRIMARY KEY,
    log_channel_id INTEGER,
    removal_log_channel_id INTEGER,
    role_1_id INTEGER,
    role_2_id INTEGER,
    role_3_id INTEGER,
    admin_role_id INTEGER
)''')
conn.commit()

# --- 헬퍼 함수 ---

def get_guild_settings(guild_id):
    # 인덱스: log(0), removal(1), r1(2), r2(3), r3(4), admin(5)
    cur.execute("SELECT log_channel_id, removal_log_channel_id, role_1_id, role_2_id, role_3_id, admin_role_id FROM settings WHERE guild_id = ?", (guild_id,))
    return cur.fetchone()

async def is_manager(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator:
        return True
    settings = get_guild_settings(interaction.guild.id)
    if settings and settings[5]: 
        admin_role = interaction.guild.get_role(settings[5])
        if admin_role in interaction.user.roles:
            return True
    return False

def get_active_warnings(user_id):
    cur.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ? AND active = 1", (user_id,))
    row = cur.fetchone()
    return row[0] if row else 0

async def update_warning_role(member: discord.Member, count: int):
    settings = get_guild_settings(member.guild.id)
    if not settings: return
    role_ids = {1: settings[2], 2: settings[3], 3: settings[4]}
    
    for r_id in role_ids.values():
        if r_id:
            role = member.guild.get_role(r_id)
            if role and role in member.roles:
                try: await member.remove_roles(role)
                except: pass

    if count > 0:
        level = min(count, 3)
        target_role_id = role_ids.get(level)
        if target_role_id:
            role = member.guild.get_role(target_role_id)
            if role: 
                try: await member.add_roles(role)
                except: pass

# --- 자동 시스템 (만료 체크 로직 보강) ---

async def remove_expired_warnings():
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    # 만료된 경고 리스트 가져오기
    cur.execute("SELECT id, user_id, reason FROM warnings WHERE active = 1 AND expires_at <= ?", (now_ts,))
    expired = cur.fetchall()

    for w_id, user_id, reason in expired:
        # DB 업데이트 먼저 수행
        cur.execute("UPDATE warnings SET active = 0 WHERE id = ?", (w_id,))
        conn.commit()

        # 모든 길드(서버)를 확인하여 해당 유저가 있는지 체크
        for guild in bot.guilds:
            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except:
                    continue # 유저를 찾을 수 없으면 다음 길드로
            
            # 역할 업데이트
            count = get_active_warnings(user_id)
            await update_warning_role(member, count)

            # 로그 채널 가져오기
            settings = get_guild_settings(guild.id)
            if settings and settings[1]: # removal_log_channel_id (해제 로그)
                log_ch = bot.get_channel(settings[1])
                # get_channel로 못 가져오면 fetch_channel 시도
                if not log_ch:
                    try: log_ch = await bot.fetch_channel(settings[1])
                    except: continue

                if log_ch:
                    embed = discord.Embed(
                        title=" 경고 기간 만료 알림",
                        description=f"{member.mention}님의 경고가 시간이 경과되어 자동 해제되었습니다.",
                        color=0x3498db,
                        timestamp=datetime.datetime.now(datetime.timezone.utc)
                    )
                    embed.add_field(name=" 만료된 경고 내용", value=f"```\n{reason}\n```", inline=False)
                    embed.add_field(name=" 현재 잔여 횟수", value=f"**{count}회**", inline=True)
                    embed.add_field(name=" 처리 방식", value="시스템 자동 만료", inline=True)
                    embed.set_thumbnail(url=member.display_avatar.url if member.display_avatar else None)
                    embed.set_footer(text=f"ID: {w_id} | Auto Expired")
                    
                    try:
                        await log_ch.send(embed=embed)
                    except Exception as e:
                        print(f"로그 전송 실패: {e}")

# --- 명령어 부분은 그대로 유지하되 settings 인덱스만 점검 ---

@bot.tree.command(name="설정", description="서버 설정을 진행합니다.")
@app_commands.describe(작업="확인 또는 신규설정", 제제채널="경고 부여 기록 채널", 해제채널="만료/해제 기록 채널")
@app_commands.choices(작업=[
    app_commands.Choice(name="확인", value="check"),
    app_commands.Choice(name="신규설정", value="save")
])
@app_commands.checks.has_permissions(administrator=True)
async def setup_integrated(interaction: discord.Interaction, 작업: str, 제제채널: discord.TextChannel = None, 해제채널: discord.TextChannel = None, 경고1단계: discord.Role = None, 경고2단계: discord.Role = None, 경고3단계: discord.Role = None, 관리자역할: discord.Role = None):
    guild_id = interaction.guild.id
    if 작업 == "check":
        settings = get_guild_settings(guild_id)
        if not settings: return await interaction.response.send_message("❌ 설정 데이터가 없습니다.", ephemeral=True)
        
        embed = discord.Embed(title=f"⚙️ {interaction.guild.name} 설정 정보", color=discord.Color.blue())
        embed.add_field(name="제제 로그", value=f"<#{settings[0]}>" if settings[0] else "❌", inline=True)
        embed.add_field(name="해제 로그", value=f"<#{settings[1]}>" if settings[1] else "❌", inline=True)
        embed.add_field(name="관리자 역할", value=f"<@&{settings[5]}>" if settings[5] else "❌", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    elif 작업 == "save":
        if not (제제채널 and 해제채널 and 경고1단계 and 경고2단계 and 경고3단계 and 관리자역할):
            return await interaction.response.send_message("❌ 모든 항목을 입력해야 합니다.", ephemeral=True)
        
        cur.execute("INSERT OR REPLACE INTO settings (guild_id, log_channel_id, removal_log_channel_id, role_1_id, role_2_id, role_3_id, admin_role_id) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                    (guild_id, 제제채널.id, 해제채널.id, 경고1단계.id, 경고2단계.id, 경고3단계.id, 관리자역할.id))
        conn.commit()
        await interaction.response.send_message("✅ 설정이 저장되었습니다. 제제와 해제 기록이 분리되어 저장됩니다.", ephemeral=True)

@bot.tree.command(name="경고", description="유저에게 경고를 부여합니다.")
async def warn(interaction: discord.Interaction, 대상: discord.Member, 사유: str):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    settings = get_guild_settings(interaction.guild.id)
    if not settings or not settings[0]: return await interaction.response.send_message("❌ `/설정`을 먼저 완료해주세요.", ephemeral=True)

    day_options = [discord.SelectOption(label=f"{i}일", value=str(i)) for i in range(1, 22)]
    day_options.append(discord.SelectOption(label="테스트 (10초)", value="test", emoji="🧪"))
    select = discord.ui.Select(placeholder="경고 기간을 선택하세요.", options=day_options)

    async def select_callback(inter2: discord.Interaction):
        val = select.values[0]
        now = datetime.datetime.now(datetime.timezone.utc)
        cur.execute("SELECT MAX(expires_at) FROM warnings WHERE user_id = ? AND active = 1", (대상.id,))
        row = cur.fetchone()
        last_expire = row[0] if row and row[0] else None
        base_time = datetime.datetime.fromtimestamp(last_expire, datetime.timezone.utc) if last_expire and last_expire > now.timestamp() else now
        delta = datetime.timedelta(seconds=10) if val == "test" else datetime.timedelta(days=int(val))
        new_expire = int((base_time + delta).timestamp())

        cur.execute("INSERT INTO warnings (user_id, reason, expires_at, active) VALUES (?, ?, ?, 1)", (대상.id, 사유, new_expire))
        conn.commit()
        
        count = get_active_warnings(대상.id)
        await update_warning_role(대상, count)

        log_channel = bot.get_channel(settings[0])
        if log_channel:
            embed = discord.Embed(title=" 유저 경고 부여", color=discord.Color.red())
            embed.add_field(name="대상 유저", value=f"{대상.mention}", inline=True)
            embed.add_field(name="현재 경고 횟수", value=f"**{count}회**", inline=True)
            embed.add_field(name="만료 예정일", value=f"<t:{new_expire}:F>", inline=False)
            embed.add_field(name="경고 사유", value=f"```\n{사유}\n```", inline=False)
            embed.set_thumbnail(url=대상.display_avatar.url)
            await log_channel.send(embed=embed)
        await inter2.response.edit_message(content=f"✅ {대상.mention} 경고 부여 (현재 **{count}**회)", view=None)

    view = discord.ui.View(); select.callback = select_callback; view.add_item(select)
    await interaction.response.send_message(f"**{대상.display_name}**님의 경고 기간 선택:", view=view, ephemeral=True)

@bot.tree.command(name="해제", description="활성화된 경고를 수동으로 해제합니다.")
async def removewarn(interaction: discord.Interaction, 대상: discord.Member):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    cur.execute("SELECT id, reason, expires_at FROM warnings WHERE user_id = ? AND active = 1 ORDER BY expires_at ASC", (대상.id,))
    rows = cur.fetchall()
    if not rows: return await interaction.response.send_message("❌ 해제할 경고가 없습니다.", ephemeral=True)

    options = [discord.SelectOption(label=f"ID: {r[0]} | {r[1][:20]}", value=str(r[0])) for r in rows[:25]]
    select = discord.ui.Select(placeholder="해제할 경고를 선택하세요.", options=options)

    async def select_callback(inter2: discord.Interaction):
        selected_id = int(select.values[0])
        cur.execute("SELECT reason FROM warnings WHERE id = ?", (selected_id,))
        warn_reason = cur.fetchone()[0]

        cur.execute("UPDATE warnings SET active = 0 WHERE id = ?", (selected_id,))
        conn.commit()
        
        after_count = get_active_warnings(대상.id)
        await update_warning_role(대상, after_count)

        settings = get_guild_settings(interaction.guild.id)
        if settings and settings[1]:
            log_ch = bot.get_channel(settings[1])
            if log_ch:
                embed = discord.Embed(
                    title=" 경고 수동 해제 알림",
                    description=f"{대상.mention}님의 경고 기록이 관리자에 의해 해제되었습니다.",
                    color=0x2ecc71,
                    timestamp=datetime.datetime.now(datetime.timezone.utc)
                )
                embed.add_field(name=" 경고 사유", value=f"```\n{warn_reason}\n```", inline=False)
                embed.add_field(name=" 담당 관리자", value=interaction.user.mention, inline=True)
                embed.add_field(name=" 남은 경고 횟수", value=f"**{after_count}회**", inline=True)
                embed.set_thumbnail(url=대상.display_avatar.url)
                embed.set_footer(text=f"ID: {selected_id} | Manual Release")
                await log_ch.send(embed=embed)
        await inter2.response.edit_message(content=f"✅ {대상.mention}님의 경고(ID: {selected_id})를 해제했습니다.", view=None)

    view = discord.ui.View(); select.callback = select_callback; view.add_item(select)
    await interaction.response.send_message(f"**{대상.display_name}**님의 해제 메뉴", view=view, ephemeral=True)

@bot.tree.command(name="조회", description="경고 기록을 확인합니다.")
async def check_warns(interaction: discord.Interaction, 대상: discord.Member):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    cur.execute("SELECT reason, expires_at FROM warnings WHERE user_id = ? AND active = 1 ORDER BY expires_at ASC", (대상.id,))
    rows = cur.fetchall()
    count = len(rows)
    embed = discord.Embed(title=f" {대상.display_name} 경고 조회", color=discord.Color.gold())
    embed.add_field(name="현재 활성 경고", value=f"총 **{count}**회", inline=False)
    if not rows: embed.description = "✅ 활성화된 경고가 없습니다."
    else:
        warn_list = [f"**{i+1}.** {r[0]} (만료: <t:{r[1]}:R>)" for i, r in enumerate(rows)]
        embed.description = "\n".join(warn_list)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ... (재부팅, 초기화 명령어는 이전과 동일)

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not scheduler.running:
        scheduler.add_job(remove_expired_warnings, "interval", seconds=10)
        scheduler.start()
    print(f"Logged in as {bot.user}")

bot.run(os.environ['token'])