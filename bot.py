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

# --- 데이터베이스 연결 및 초기화 ---
DATABASE = "warnings.db"

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

def get_db():
    return sqlite3.connect(DATABASE)

def get_guild_settings(guild_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT log_channel_id, removal_log_channel_id, role_1_id, role_2_id, role_3_id, admin_role_id FROM settings WHERE guild_id = ?", (guild_id,))
        return cur.fetchone()

async def is_manager(interaction: discord.Interaction):
    if interaction.user.guild_permissions.administrator:
        return True
    settings = get_guild_settings(interaction.guild.id)
    if settings and settings[5]: 
        return any(role.id == settings[5] for role in interaction.user.roles)
    return False

def get_active_warnings(user_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ? AND active = 1", (user_id,))
        row = cur.fetchone()
        return row[0] if row else 0

async def update_warning_role(member: discord.Member, count: int):
    settings = get_guild_settings(member.guild.id)
    if not settings: return
    
    # 설정된 역할 ID 리스트 (경고 1, 2, 3단계)
    warning_role_ids = [settings[2], settings[3], settings[4]]
    target_role_id = warning_role_ids[min(count, 3) - 1] if count > 0 else None
    
    roles_to_remove = [member.guild.get_role(rid) for rid in warning_role_ids if rid and rid != target_role_id]
    roles_to_add = member.guild.get_role(target_role_id) if target_role_id else None

    try:
        # 제거해야 할 역할 중 멤버가 가진 것만 제거
        to_remove = [r for r in roles_to_remove if r and r in member.roles]
        if to_remove: await member.remove_roles(*to_remove)
        # 추가해야 할 역할이 있고 멤버가 아직 없다면 추가
        if roles_to_add and roles_to_add not in member.roles:
            await member.add_roles(roles_to_add)
    except discord.Forbidden:
        print(f"권한 부족: {member.guild.name}에서 역할을 수정할 수 없습니다.")

# --- 자동 시스템 (만료 체크) ---

async def remove_expired_warnings():
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, user_id, reason FROM warnings WHERE active = 1 AND expires_at <= ?", (now_ts,))
        expired = cur.fetchall()
        
        if not expired: return

        for w_id, user_id, reason in expired:
            cur.execute("UPDATE warnings SET active = 0 WHERE id = ?", (w_id,))
            conn.commit()

            for guild in bot.guilds:
                member = guild.get_member(user_id) or await (lambda: None if not (m := None) else m)() # 캐시 우선
                if not member:
                    try: member = await guild.fetch_member(user_id)
                    except: continue
                
                count = get_active_warnings(user_id)
                await update_warning_role(member, count)

                settings = get_guild_settings(guild.id)
                if settings and settings[1]:
                    log_ch = bot.get_channel(settings[1]) or await bot.fetch_channel(settings[1])
                    if log_ch:
                        embed = discord.Embed(
                            title="경고 기간 만료 알림",
                            description=f"{member.mention}님의 경고가 자동 해제되었습니다.",
                            color=0x3498db,
                            timestamp=datetime.datetime.now(datetime.timezone.utc)
                        )
                        embed.add_field(name="경고 내용", value=f"```\n{reason}\n```", inline=False)
                        embed.add_field(name="잔여 횟수", value=f"**{count}회**", inline=True)
                        embed.set_footer(text=f"경고 ID: {w_id} | 자동 처리")
                        if member.display_avatar: embed.set_thumbnail(url=member.display_avatar.url)
                        await log_ch.send(embed=embed)

# --- 슬래시 명령어 ---

@bot.tree.command(name="설정", description="서버의 경고 시스템 채널과 역할을 설정합니다.")
@app_commands.describe(작업="확인 또는 신규설정", 제제채널="경고 알림 채널", 해제채널="해제/만료 알림 채널")
@app_commands.choices(작업=[
    app_commands.Choice(name="설정 확인", value="check"),
    app_commands.Choice(name="신규 설정 저장", value="save")
])
@app_commands.checks.has_permissions(administrator=True)
async def setup_integrated(interaction: discord.Interaction, 작업: str, 제제채널: discord.TextChannel = None, 해제채널: discord.TextChannel = None, 경고1단계: discord.Role = None, 경고2단계: discord.Role = None, 경고3단계: discord.Role = None, 관리자역할: discord.Role = None):
    if 작업 == "check":
        s = get_guild_settings(interaction.guild.id)
        if not s: return await interaction.response.send_message("❌ 설정된 데이터가 없습니다.", ephemeral=True)
        
        embed = discord.Embed(title="⚙️ 서버 설정 현황", color=0x3498db)
        embed.add_field(name="로그 채널", value=f"제제: <#{s[0]}>\n해제: <#{s[1]}>", inline=False)
        embed.add_field(name="역할 설정", value=f"1단계: <@&{s[2]}>\n2단계: <@&{s[3]}>\n3단계: <@&{s[4]}>", inline=True)
        embed.add_field(name="관리 역할", value=f"<@&{s[5]}>", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    elif 작업 == "save":
        if not all([제제채널, 해제채널, 경고1단계, 경고2단계, 경고3단계, 관리자역할]):
            return await interaction.response.send_message("❌ 모든 옵션을 입력해야 저장할 수 있습니다.", ephemeral=True)
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?, ?, ?, ?, ?)", 
                        (interaction.guild.id, 제제채널.id, 해제채널.id, 경고1단계.id, 경고2단계.id, 경고3단계.id, 관리자역할.id))
        await interaction.response.send_message("✅ 설정이 성공적으로 저장되었습니다.", ephemeral=True)

@bot.tree.command(name="경고", description="유저에게 경고를 부여합니다.")
async def warn(interaction: discord.Interaction, 대상: discord.Member, 사유: str):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    settings = get_guild_settings(interaction.guild.id)
    if not settings: return await interaction.response.send_message("❌ `/설정`을 먼저 완료해주세요.", ephemeral=True)

    options = [discord.SelectOption(label=f"{i}일", value=str(i)) for i in range(1, 22)]
    options.append(discord.SelectOption(label="🧪 테스트 (10초)", value="test"))
    select = discord.ui.Select(placeholder="경고 유지 기간을 선택하세요.", options=options)

    async def callback(inter2: discord.Interaction):
        val = select.values[0]
        delta = datetime.timedelta(seconds=10) if val == "test" else datetime.timedelta(days=int(val))
        expires_at = int((datetime.datetime.now(datetime.timezone.utc) + delta).timestamp())

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO warnings (user_id, reason, expires_at) VALUES (?, ?, ?)", (대상.id, 사유, expires_at))
        
        count = get_active_warnings(대상.id)
        await update_warning_role(대상, count)

        log_ch = bot.get_channel(settings[0])
        if log_ch:
            embed = discord.Embed(title="🚨 경고 부여", color=0xe74c3c, timestamp=datetime.datetime.now(datetime.timezone.utc))
            embed.add_field(name="피경고자", value=대상.mention, inline=True)
            embed.add_field(name="현재 누적", value=f"**{count}회**", inline=True)
            embed.add_field(name="만료일", value=f"<t:{expires_at}:F> (<t:{expires_at}:R>)", inline=False)
            embed.add_field(name="사유", value=f"```\n{사유}\n```", inline=False)
            embed.set_thumbnail(url=대상.display_avatar.url)
            await log_ch.send(embed=embed)
        await inter2.response.edit_message(content=f"✅ {대상.mention}님에게 경고를 부여했습니다. (누적 {count}회)", view=None)

    view = discord.ui.View(); select.callback = callback; view.add_item(select)
    await interaction.response.send_message(f"**{대상.display_name}**님의 기간 설정:", view=view, ephemeral=True)

@bot.tree.command(name="해제", description="활성화된 경고를 직접 해제합니다.")
async def removewarn(interaction: discord.Interaction, 대상: discord.Member):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, reason FROM warnings WHERE user_id = ? AND active = 1", (대상.id,))
        rows = cur.fetchall()
    
    if not rows: return await interaction.response.send_message("❌ 해당 유저는 활성화된 경고가 없습니다.", ephemeral=True)

    options = [discord.SelectOption(label=f"ID: {r[0]} | {r[1][:20]}...", value=str(r[0])) for r in rows[:25]]
    select = discord.ui.Select(placeholder="해제할 경고를 선택하세요.", options=options)

    async def callback(inter2: discord.Interaction):
        wid = int(select.values[0])
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT reason FROM warnings WHERE id = ?", (wid,))
            reason = cur.fetchone()[0]
            cur.execute("UPDATE warnings SET active = 0 WHERE id = ?", (wid,))
        
        count = get_active_warnings(대상.id)
        await update_warning_role(대상, count)

        settings = get_guild_settings(interaction.guild.id)
        if settings and settings[1]:
            log_ch = bot.get_channel(settings[1])
            if log_ch:
                embed = discord.Embed(title="수동 해제", color=0x2ecc71, timestamp=datetime.datetime.now(datetime.timezone.utc))
                embed.add_field(name="대상", value=대상.mention, inline=True)
                embed.add_field(name="처리자", value=interaction.user.mention, inline=True)
                embed.add_field(name="해제된 사유", value=f"```\n{reason}\n```", inline=False)
                embed.set_footer(text=f"잔여 경고: {count}회")
                await log_ch.send(embed=embed)
        await inter2.response.edit_message(content=f"✅ {대상.mention}님의 경고(ID: {wid})를 해제했습니다.", view=None)

    view = discord.ui.View(); select.callback = callback; view.add_item(select)
    await interaction.response.send_message(f"**{대상.display_name}** 경고 해제 선택:", view=view, ephemeral=True)

@bot.tree.command(name="조회", description="유저의 경고 기록을 조회합니다.")
async def check_warns(interaction: discord.Interaction, 대상: discord.Member):
    if not await is_manager(interaction): return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT reason, expires_at FROM warnings WHERE user_id = ? AND active = 1 ORDER BY expires_at ASC", (대상.id,))
        rows = cur.fetchall()
    
    embed = discord.Embed(title=f" {대상.display_name} 경고 조회", color=0xf1c40f)
    if not rows:
        embed.description = "활성화된 경고가 없습니다."
    else:
        desc = ""
        for i, r in enumerate(rows, 1):
            desc += f"**{i}.** {r[0]}\n└ 만료: <t:{r[1]}:R>\n"
        embed.description = desc
        embed.set_footer(text=f"총 {len(rows)}개의 경고가 활성화되어 있습니다.")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="초기화", description="데이터베이스를 초기화합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def reset_db(interaction: discord.Interaction):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS warnings")
        cur.execute("DROP TABLE IF EXISTS settings")
    init_db()
    await interaction.response.send_message(" 모든 데이터가 초기화되었습니다. 다시 `/설정`을 해주세요.", ephemeral=True)

@bot.tree.command(name="재부팅", description="봇을 재부팅합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def reboot(interaction: discord.Interaction):
    await interaction.response.send_message(" 봇을 재부팅합니다...", ephemeral=True)
    os._exit(0)

# --- 실행부 ---

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not scheduler.running:
        scheduler.add_job(remove_expired_warnings, "interval", seconds=30) # 30초마다 체크로 부하 감소
        scheduler.start()
    print(f"✅ 로그인 완료: {bot.user}")

bot.run(os.environ['token'])