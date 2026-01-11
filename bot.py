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

DATABASE = "warnings.db"

# --- 데이터베이스 초기화 ---
def init_db():
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            reason TEXT,
            expires_at INTEGER,
            active INTEGER DEFAULT 1
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

init_db()

# --- 헬퍼 함수 ---
def get_guild_settings(guild_id):
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT log_channel_id, removal_log_channel_id, role_1_id, role_2_id, role_3_id, admin_role_id FROM settings WHERE guild_id = ?", (guild_id,))
        return cur.fetchone()

async def is_manager(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator:
        return True
    settings = get_guild_settings(interaction.guild.id)
    if settings and settings[5]: 
        admin_role = interaction.guild.get_role(settings[5])
        return admin_role in interaction.user.roles
    return False

def get_active_warnings(user_id):
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ? AND active = 1", (user_id,))
        row = cur.fetchone()
        return row[0] if row else 0

async def update_warning_role(member: discord.Member, count: int):
    settings = get_guild_settings(member.guild.id)
    if not settings: return
    role_ids = [settings[2], settings[3], settings[4]] # 1, 2, 3단계 역할
    
    target_role_id = role_ids[min(count, 3) - 1] if count > 0 else None
    
    for rid in role_ids:
        if not rid: continue
        role = member.guild.get_role(rid)
        if not role: continue
        try:
            if rid == target_role_id:
                if role not in member.roles: await member.add_roles(role)
            else:
                if role in member.roles: await member.remove_roles(role)
        except: pass

# --- 자동 만료 시스템 (보강 완료) ---
async def remove_expired_warnings():
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, user_id, reason FROM warnings WHERE active = 1 AND expires_at <= ?", (now_ts,))
        expired_list = cur.fetchall()
        
        for w_id, user_id, reason in expired_list:
            cur.execute("UPDATE warnings SET active = 0 WHERE id = ?", (w_id,))
            conn.commit()

            for guild in bot.guilds:
                member = guild.get_member(user_id)
                if not member:
                    try: member = await guild.fetch_member(user_id)
                    except: continue
                
                new_count = get_active_warnings(user_id)
                await update_warning_role(member, new_count)

                s = get_guild_settings(guild.id)
                if s and s[1]: # 해제 로그 채널
                    log_ch = bot.get_channel(s[1]) or await bot.fetch_channel(s[1])
                    if log_ch:
                        embed = discord.Embed(
                            title="경고 기간 만료 알림",
                            description=f"{member.mention}님의 경고가 시간이 경과되어 자동 해제되었습니다.",
                            color=0x3498db,
                            timestamp=datetime.datetime.now(datetime.timezone.utc)
                        )
                        embed.add_field(name="경고 내용", value=f"```\n{reason}\n```", inline=False)
                        embed.add_field(name="잔여 횟수", value=f"**{new_count}회**", inline=True)
                        embed.set_footer(text=f"ID: {w_id} | 시스템 자동 처리")
                        if member.display_avatar: embed.set_thumbnail(url=member.display_avatar.url)
                        await log_ch.send(embed=embed)

# --- 명령어 세트 ---

@bot.tree.command(name="설정", description="서버의 로그 채널과 역할을 설정합니다.")
@app_commands.describe(작업="확인 또는 신규설정", 제제채널="경고 로그", 해제채널="만료/해제 로그")
@app_commands.choices(작업=[
    app_commands.Choice(name="확인", value="check"),
    app_commands.Choice(name="신규설정", value="save")
])
async def setup_integrated(interaction: discord.Interaction, 작업: str, 제제채널: discord.TextChannel = None, 해제채널: discord.TextChannel = None, 경고1단계: discord.Role = None, 경고2단계: discord.Role = None, 경고3단계: discord.Role = None, 관리자역할: discord.Role = None):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ 관리자 권한이 필요합니다.", ephemeral=True)
    
    guild_id = interaction.guild.id
    if 작업 == "check":
        s = get_guild_settings(guild_id)
        if not s: return await interaction.response.send_message("❌ 설정 데이터가 없습니다.", ephemeral=True)
        embed = discord.Embed(title=f"⚙️ {interaction.guild.name} 설정 정보", color=discord.Color.blue())
        embed.add_field(name="제제 로그", value=f"<#{s[0]}>" if s[0] else "❌", inline=True)
        embed.add_field(name="해제 로그", value=f"<#{s[1]}>" if s[1] else "❌", inline=True)
        embed.add_field(name="관리자 역할", value=f"<@&{s[5]}>" if s[5] else "❌", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    elif 작업 == "save":
        if not all([제제채널, 해제채널, 경고1단계, 경고2단계, 경고3단계, 관리자역할]):
            return await interaction.response.send_message("❌ 모든 항목을 입력해야 합니다.", ephemeral=True)
        with sqlite3.connect(DATABASE) as conn:
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?, ?, ?, ?, ?)", 
                        (guild_id, 제제채널.id, 해제채널.id, 경고1단계.id, 경고2단계.id, 경고3단계.id, 관리자역할.id))
        await interaction.response.send_message("✅ 설정 저장 완료.", ephemeral=True)

@bot.tree.command(name="경고", description="유저에게 경고를 부여합니다.")
async def warn(interaction: discord.Interaction, 대상: discord.Member, 사유: str):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    options = [discord.SelectOption(label=f"{i}일", value=str(i)) for i in range(1, 22)]
    options.append(discord.SelectOption(label="🧪 테스트 (10초)", value="test"))
    select = discord.ui.Select(placeholder="경고 기간을 선택하세요.", options=options)

    async def callback(inter2: discord.Interaction):
        val = select.values[0]
        delta = datetime.timedelta(seconds=10) if val == "test" else datetime.timedelta(days=int(val))
        exp = int((datetime.datetime.now(datetime.timezone.utc) + delta).timestamp())

        with sqlite3.connect(DATABASE) as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO warnings (user_id, reason, expires_at) VALUES (?, ?, ?)", (대상.id, 사유, exp))
        
        cnt = get_active_warnings(대상.id)
        await update_warning_role(대상, cnt)
        
        s = get_guild_settings(interaction.guild.id)
        if s and s[0]:
            log_ch = bot.get_channel(s[0])
            if log_ch:
                embed = discord.Embed(title=" 유저 경고 부여", color=0xe74c3c, timestamp=datetime.datetime.now(datetime.timezone.utc))
                embed.add_field(name="대상 유저", value=대상.mention, inline=True)
                embed.add_field(name="현재 횟수", value=f"**{cnt}회**", inline=True)
                embed.add_field(name="만료 예정", value=f"<t:{exp}:F>", inline=False)
                embed.add_field(name="사유", value=f"```\n{사유}\n```", inline=False)
                if 대상.display_avatar: embed.set_thumbnail(url=대상.display_avatar.url)
                await log_ch.send(embed=embed)
        await inter2.response.edit_message(content=f"✅ {대상.mention}님에게 경고를 부여했습니다. (현재 {cnt}회)", view=None)

    view = discord.ui.View(); select.callback = callback; view.add_item(select)
    await interaction.response.send_message(f"**{대상.display_name}** 기간 선택:", view=view, ephemeral=True)

@bot.tree.command(name="해제", description="활성화된 경고를 수동으로 해제합니다.")
async def remove(interaction: discord.Interaction, 대상: discord.Member):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, reason FROM warnings WHERE user_id = ? AND active = 1 ORDER BY expires_at ASC", (대상.id,))
        rows = cur.fetchall()
    
    if not rows: return await interaction.response.send_message("❌ 해제할 경고가 없습니다.", ephemeral=True)

    options = [discord.SelectOption(label=f"ID: {r[0]} | {r[1][:20]}", value=str(r[0])) for r in rows[:25]]
    select = discord.ui.Select(placeholder="해제할 항목을 선택하세요.", options=options)

    async def callback(inter2: discord.Interaction):
        wid = int(select.values[0])
        with sqlite3.connect(DATABASE) as conn:
            cur = conn.cursor()
            cur.execute("SELECT reason FROM warnings WHERE id = ?", (wid,))
            reason = cur.fetchone()[0]
            cur.execute("UPDATE warnings SET active = 0 WHERE id = ?", (wid,))
        
        cnt = get_active_warnings(대상.id)
        await update_warning_role(대상, cnt)
        
        s = get_guild_settings(interaction.guild.id)
        if s and s[1]:
            log_ch = bot.get_channel(s[1])
            if log_ch:
                embed = discord.Embed(title=" 경고 수동 해제", color=0x2ecc71, timestamp=datetime.datetime.now(datetime.timezone.utc))
                embed.add_field(name="대상", value=대상.mention, inline=True)
                embed.add_field(name="해제 사유", value=f"```\n{reason}\n```", inline=False)
                embed.add_field(name="담당자", value=interaction.user.mention, inline=True)
                embed.set_footer(text=f"잔여 경고: {cnt}회")
                await log_ch.send(embed=embed)
        await inter2.response.edit_message(content=f"✅ {대상.mention}님의 경고(ID: {wid})를 해제했습니다.", view=None)

    view = discord.ui.View(); select.callback = callback; view.add_item(select)
    await interaction.response.send_message("해제할 경고를 선택하세요:", view=view, ephemeral=True)

@bot.tree.command(name="조회", description="유저의 경고 기록을 조회합니다.")
async def check_warns(interaction: discord.Interaction, 대상: discord.Member):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT reason, expires_at FROM warnings WHERE user_id = ? AND active = 1 ORDER BY expires_at ASC", (대상.id,))
        rows = cur.fetchall()
    
    embed = discord.Embed(title=f" {대상.display_name} 경고 조회", color=0xf1c40f)
    if not rows:
        embed.description = "✅ 활성화된 경고가 없습니다."
    else:
        warn_list = [f"**{i+1}.** {r[0]} (만료: <t:{r[1]}:R>)" for i, r in enumerate(rows)]
        embed.description = "\n".join(warn_list)
        embed.set_footer(text=f"총 {len(rows)}회의 경고가 있습니다.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="초기화", description="DB를 완전히 초기화합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def reset(interaction: discord.Interaction):
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS warnings")
        cur.execute("DROP TABLE IF EXISTS settings")
    init_db()
    await interaction.response.send_message("✅ 데이터베이스가 초기화되었습니다. 다시 `/설정`을 진행해주세요.", ephemeral=True)

@bot.tree.command(name="재부팅", description="봇을 재시작합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def reboot(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 봇을 재부팅합니다...", ephemeral=True)
    os._exit(0)

# --- 봇 시작 ---
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not scheduler.running:
        scheduler.add_job(remove_expired_warnings, "interval", seconds=10)
        scheduler.start()
    print(f"✅ {bot.user} 온라인! 자동 만료 및 모든 명령어 로드됨.")

bot.run(os.environ['token'])