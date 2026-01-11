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

# --- 데이터베이스 연결 ---
conn = sqlite3.connect("warnings.db")
cur = conn.cursor()

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
    role_1_id INTEGER,
    role_2_id INTEGER,
    role_3_id INTEGER,
    admin_role_id INTEGER
)''')
conn.commit()

# --- 헬퍼 함수 ---

def get_guild_settings(guild_id):
    cur.execute("SELECT log_channel_id, role_1_id, role_2_id, role_3_id, admin_role_id FROM settings WHERE guild_id = ?", (guild_id,))
    return cur.fetchone()

async def is_manager(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator:
        return True
    settings = get_guild_settings(interaction.guild.id)
    if settings and settings[4]: 
        admin_role = interaction.guild.get_role(settings[4])
        if admin_role in interaction.user.roles:
            return True
    return False

def get_active_warnings(user_id):
    cur.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ? AND active = 1", (user_id,))
    return cur.fetchone()[0]

async def update_warning_role(member: discord.Member, count: int):
    settings = get_guild_settings(member.guild.id)
    if not settings: return
    role_ids = {1: settings[1], 2: settings[2], 3: settings[3]}
    
    # 기존 경고 역할 모두 제거
    for r_id in role_ids.values():
        if r_id:
            role = member.guild.get_role(r_id)
            if role and role in member.roles:
                try: await member.remove_roles(role)
                except: pass

    # 현재 횟수에 맞는 역할 부여
    if count > 0:
        level = min(count, 3)
        target_role_id = role_ids.get(level)
        if target_role_id:
            role = member.guild.get_role(target_role_id)
            if role: 
                try: await member.add_roles(role)
                except: pass

# --- 명령어 ---

@bot.tree.command(name="설정", description="서버 설정을 확인하거나 새로 등록합니다.")
@app_commands.describe(작업="확인 또는 신규설정", 관리자역할="봇 관리 권한을 줄 역할")
@app_commands.choices(작업=[
    app_commands.Choice(name="확인", value="check"),
    app_commands.Choice(name="신규설정", value="save")
])
@app_commands.checks.has_permissions(administrator=True)
async def setup_integrated(interaction: discord.Interaction, 작업: str, 로그채널: discord.TextChannel = None, 경고1단계: discord.Role = None, 경고2단계: discord.Role = None, 경고3단계: discord.Role = None, 관리자역할: discord.Role = None):
    guild_id = interaction.guild.id
    if 작업 == "check":
        settings = get_guild_settings(guild_id)
        if not settings: return await interaction.response.send_message("❌ 설정 데이터가 없습니다.", ephemeral=True)
        log_ch = interaction.guild.get_channel(settings[0])
        admin_r = interaction.guild.get_role(settings[4])
        embed = discord.Embed(title=f"⚙️ {interaction.guild.name} 설정 정보", color=discord.Color.blue())
        embed.add_field(name="로그 채널", value=log_ch.mention if log_ch else "❌ 미설정", inline=False)
        embed.add_field(name="관리자 역할", value=admin_r.mention if admin_r else "❌ 미설정", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    elif 작업 == "save":
        if not (로그채널 and 경고1단계 and 경고2단계 and 경고3단계 and 관리자역할):
            return await interaction.response.send_message("❌ 모든 항목을 입력해야 합니다.", ephemeral=True)
        cur.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (guild_id,))
        cur.execute("UPDATE settings SET log_channel_id = ?, role_1_id = ?, role_2_id = ?, role_3_id = ?, admin_role_id = ? WHERE guild_id = ?", (로그채널.id, 경고1단계.id, 경고2단계.id, 경고3단계.id, 관리자역할.id, guild_id))
        conn.commit()
        await interaction.response.send_message("✅ 설정 저장 완료.", ephemeral=True)

@bot.tree.command(name="경고", description="유저에게 경고를 부여합니다.")
async def warn(interaction: discord.Interaction, 대상: discord.Member, 사유: str):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    settings = get_guild_settings(interaction.guild.id)
    if not settings: return await interaction.response.send_message("❌ `/설정`을 먼저 해주세요.", ephemeral=True)

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
        
        current_count = get_active_warnings(대상.id)
        await update_warning_role(대상, current_count)

        log_channel = bot.get_channel(settings[0])
        if log_channel:
            embed = discord.Embed(title="🚨 유저 경고 부여", color=discord.Color.red(), timestamp=now)
            embed.add_field(name="대상 유저", value=f"{대상.mention} ({대상.name})", inline=True)
            embed.add_field(name="현재 경고 횟수", value=f"**{current_count}회**", inline=True)
            embed.add_field(name="누적 만료일", value=f"<t:{new_expire}:F> (<t:{new_expire}:R>)", inline=False)
            embed.add_field(name="경고 사유", value=f"```\n{사유}\n```", inline=False)
            embed.set_thumbnail(url=대상.display_avatar.url)
            embed.set_footer(text=f"담당 관리자: {interaction.user.display_name}")
            await log_channel.send(embed=embed)
        await inter2.response.edit_message(content=f"✅ {대상.mention}님에게 경고를 부여했습니다. (현재 **{current_count}**회)", view=None)

    view = discord.ui.View(); select.callback = select_callback; view.add_item(select)
    await interaction.response.send_message(f"**{대상.display_name}**님의 경고 기간 선택:", view=view, ephemeral=True)

@bot.tree.command(name="해제", description="활성화된 경고 중 하나를 선택하여 해제합니다.")
async def removewarn(interaction: discord.Interaction, 대상: discord.Member):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    cur.execute("SELECT id, reason, expires_at FROM warnings WHERE user_id = ? AND active = 1 ORDER BY expires_at ASC", (대상.id,))
    warnings_list = cur.fetchall()
    if not warnings_list: return await interaction.response.send_message("❌ 해제할 활성 경고가 없습니다.", ephemeral=True)

    options = [discord.SelectOption(label=f"ID: {wid} | {reason[:20]}", description=f"만료: {datetime.datetime.fromtimestamp(expire).strftime('%m-%d %H:%M')}", value=str(wid)) for wid, reason, expire in warnings_list[:25]]
    select = discord.ui.Select(placeholder="해제할 경고를 선택하세요.", options=options)

    async def select_callback(inter2: discord.Interaction):
        selected_id = int(select.values[0])
        cur.execute("UPDATE warnings SET active = 0 WHERE id = ?", (selected_id,))
        conn.commit()
        
        after_count = get_active_warnings(대상.id)
        await update_warning_role(대상, after_count)

        settings = get_guild_settings(interaction.guild.id)
        if settings and settings[0]:
            log_ch = bot.get_channel(settings[0])
            if log_ch:
                embed = discord.Embed(title="🗑️ 유저 경고 해제", color=discord.Color.green())
                embed.add_field(name="대상 유저", value=대상.mention, inline=True)
                embed.add_field(name="남은 경고 횟수", value=f"**{after_count}회**", inline=True)
                embed.set_footer(text=f"해제 관리자: {interaction.user.display_name}")
                await log_ch.send(embed=embed)
        await inter2.response.edit_message(content=f"✅ 경고를 해제했습니다. (남은 경고: **{after_count}**회)", view=None)

    view = discord.ui.View(); select.callback = select_callback; view.add_item(select)
    await interaction.response.send_message(f"**{대상.display_name}**님의 경고 해제 메뉴", view=view, ephemeral=True)

@bot.tree.command(name="조회", description="경고 기록을 확인합니다.")
async def check_warns(interaction: discord.Interaction, 대상: discord.Member):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    cur.execute("SELECT reason, expires_at FROM warnings WHERE user_id = ? AND active = 1 ORDER BY expires_at ASC", (대상.id,))
    rows = cur.fetchall()
    count = len(rows)
    
    embed = discord.Embed(title=f"📊 {대상.display_name} 경고 리포트", color=discord.Color.gold())
    embed.add_field(name="현재 활성 경고", value=f"총 **{count}**회", inline=False)
    
    if not rows:
        embed.description = "✅ 현재 활성화된 경고가 없습니다."
    else:
        warn_list = []
        for i, r in enumerate(rows, 1):
            warn_list.append(f"**{i}.** {r[0]}\n└ 만료: <t:{r[1]}:F> (<t:{r[1]}:R>)")
        embed.description = "\n\n".join(warn_list)
    
    embed.set_thumbnail(url=대상.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="재부팅", description="봇을 재시작합니다. (관리자 전용)")
@app_commands.checks.has_permissions(administrator=True)
async def reboot(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 봇을 재부팅합니다...", ephemeral=True)
    conn.close()
    os._exit(0)

@bot.tree.command(name="초기화", description="DB를 완전 초기화합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def reset_db(interaction: discord.Interaction):
    cur.execute("DROP TABLE IF EXISTS warnings")
    cur.execute("DROP TABLE IF EXISTS settings")
    cur.execute('''CREATE TABLE warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, reason TEXT, expires_at INTEGER, active INTEGER)''')
    cur.execute('''CREATE TABLE settings (guild_id INTEGER PRIMARY KEY, log_channel_id INTEGER, role_1_id INTEGER, role_2_id INTEGER, role_3_id INTEGER, admin_role_id INTEGER)''')
    conn.commit()
    await interaction.response.send_message("✅ DB 초기화 성공.", ephemeral=True)

# --- 실행 ---
scheduler = AsyncIOScheduler()

async def remove_expired_warnings():
    """시간이 지난 경고를 자동으로 해제하고 로그 채널에 알림을 보냅니다."""
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    
    # 만료된 경고들 가져오기
    cur.execute("SELECT id, user_id, reason FROM warnings WHERE active = 1 AND expires_at <= ?", (now_ts,))
    expired = cur.fetchall()

    for w_id, user_id, reason in expired:
        # 1. DB 상태 업데이트
        cur.execute("UPDATE warnings SET active = 0 WHERE id = ?", (w_id,))
        conn.commit()

        # 2. 모든 서버를 돌며 해당 유저 찾기 및 역할 갱신
        for guild in bot.guilds:
            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except:
                    continue # 유저를 찾을 수 없는 서버는 패스

            # 역할 갱신
            count = get_active_warnings(user_id)
            await update_warning_role(member, count)

            # 3. 로그 채널에 자동 만료 알림 전송
            settings = get_guild_settings(guild.id)
            if settings and settings[0]: # log_channel_id가 설정되어 있다면
                log_channel = bot.get_channel(settings[0])
                if log_channel:
                    embed = discord.Embed(
                        title="경고 기간 만료 (자동 해제)",
                        description=f"{member.mention}님의 경고 기간이 종료되었습니다.",
                        color=discord.Color.blue(),
                        timestamp=datetime.datetime.now(datetime.timezone.utc)
                    )
                    embed.add_field(name="경고 사유", value=f"```\n{reason}\n```", inline=False)
                    embed.add_field(name="현재 잔여 경고", value=f"**{count}회**", inline=True)
                    embed.set_thumbnail(url=member.display_avatar.url if member.display_avatar else None)
                    embed.set_footer(text=f"서버: {guild.name}")
                    
                    try:
                        await log_channel.send(embed=embed)
                    except:
                        pass

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not scheduler.running:
        scheduler.add_job(remove_expired_warnings, "interval", seconds=10)
        scheduler.start()
    print(f"Logged in as {bot.user}")

bot.run(os.environ['token'])