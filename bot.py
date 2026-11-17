import asyncio
import logging
import os
import sqlite3
import time

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv('DISCORD_TOKEN')
DB_PATH = os.getenv('DB_PATH', 'moderation.db')

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='.', intents=intents, help_command=None)
db = sqlite3.connect(DB_PATH)
db.execute('PRAGMA journal_mode=WAL')
db.executescript('''
CREATE TABLE IF NOT EXISTS access (guild INTEGER, user INTEGER, kind TEXT, PRIMARY KEY(guild,user,kind));
CREATE TABLE IF NOT EXISTS bans (guild INTEGER, user INTEGER, kind TEXT, expires INTEGER, reason TEXT, PRIMARY KEY(guild,user,kind));
CREATE TABLE IF NOT EXISTS protected_roles (guild INTEGER, user INTEGER, role INTEGER, PRIMARY KEY(guild,user,role));
''')
db.commit()


def has(guild, user, kind):
    return db.execute('SELECT 1 FROM access WHERE guild=? AND user=? AND kind=?', (guild, user, kind)).fetchone() is not None


def rank(member):
    if member.id == member.guild.owner_id:
        return 4
    for kind, value in (('ceo', 3), ('semi', 2), ('dope', 1), ('low', 1)):
        if has(member.guild.id, member.id, kind):
            return value
    return 0


def require(level):
    async def predicate(ctx):
        if not ctx.guild:
            raise commands.NoPrivateMessage()
        if rank(ctx.author) < level:
            raise commands.CheckFailure('Недостаточно прав для этой команды.')
        return True
    return commands.check(predicate)


def user_id(value):
    value = value.strip().removeprefix('<@').removesuffix('>').removeprefix('!')
    if not value.isdecimal() or not 1 <= len(value) <= 20:
        raise commands.BadArgument('Укажите упоминание пользователя или его числовой ID.')
    return int(value)


def target_ok(ctx, uid):
    if uid in (ctx.guild.owner_id, bot.user.id, ctx.author.id) or has(ctx.guild.id, uid, 'wl'):
        raise commands.BadArgument('Нельзя применить действие к владельцу, боту, себе или пользователю вайтлиста.')
    member = ctx.guild.get_member(uid)
    if member and (rank(member) >= rank(ctx.author) or member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id):
        raise commands.BadArgument('Нельзя применить действие к пользователю с равными или большими правами или ролью.')
    # Stored administrator status also protects absent users.
    if any(has(ctx.guild.id, uid, k) for k in ('ceo', 'semi', 'dope', 'low')) and rank(ctx.author) <= max((v for k, v in (('ceo', 3), ('semi', 2), ('dope', 1), ('low', 1)) if has(ctx.guild.id, uid, k)), default=0):
        raise commands.BadArgument('У пользователя равные или большие полномочия.')


def set_access(gid, uid, kind, enabled):
    if enabled:
        db.execute('INSERT OR IGNORE INTO access VALUES (?,?,?)', (gid, uid, kind))
    else:
        db.execute('DELETE FROM access WHERE guild=? AND user=? AND kind=?', (gid, uid, kind))
    db.commit()


def names(rows):
    return ', '.join(f'<@{row[0]}> (`{row[0]}`)' for row in rows) or 'пусто'


async def send_chunks(ctx, text):
    while text:
        await ctx.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())
        text = text[1900:]


@bot.command(name='blacklist')
@require(2)
async def blacklist(ctx, target: str):
    if target.lower() == 'list':
        return await send_chunks(ctx, 'Чёрный список: ' + names(db.execute('SELECT user FROM access WHERE guild=? AND kind="blacklist"', (ctx.guild.id,)).fetchall()))
    uid = user_id(target)
    target_ok(ctx, uid)
    set_access(ctx.guild.id, uid, 'blacklist', True)
    await ctx.send(f'`{uid}` добавлен в чёрный список. При входе пользователь будет забанен.')


@bot.command(name='unblacklist')
@require(2)
async def unblacklist(ctx, target: str):
    uid = user_id(target)
    set_access(ctx.guild.id, uid, 'blacklist', False)
    await ctx.send(f'`{uid}` удалён из чёрного списка. Текущий бан снимается отдельно.')


@bot.command(name='wl')
@require(3)
async def whitelist(ctx, action: str, target: str = None):
    if action == 'list':
        return await send_chunks(ctx, 'Вайтлист: ' + names(db.execute('SELECT user FROM access WHERE guild=? AND kind="wl"', (ctx.guild.id,)).fetchall()))
    if action not in ('add', 'rem') or not target:
        raise commands.BadArgument('Используйте `.wl list`, `.wl add @user/id` или `.wl rem @user/id`.')
    uid = user_id(target)
    if action == 'add' and (has(ctx.guild.id, uid, 'blacklist') or db.execute('SELECT 1 FROM bans WHERE guild=? AND user=?', (ctx.guild.id, uid)).fetchone()):
        raise commands.BadArgument('Сначала удалите пользователя из чёрного списка и снимите активные баны бота.')
    set_access(ctx.guild.id, uid, 'wl', action == 'add')
    await ctx.send(f'Вайтлист обновлён для `{uid}`.')


@bot.command(name='ceo')
@require(4)
async def ceo(ctx, action: str, target: str):
    if action not in ('add', 'rem'):
        raise commands.BadArgument('Используйте `.ceo add/rem @user/id`.')
    uid = user_id(target)
    set_access(ctx.guild.id, uid, 'ceo', action == 'add')
    await ctx.send(f'CEO обновлён для `{uid}`.')


@bot.command(name='mod')
@require(3)
async def mod(ctx, action: str, target: str, level: str = None):
    uid = user_id(target)
    if action not in ('add', 'rem') or (action == 'add' and level not in ('semi', 'low', 'dope')):
        raise commands.BadArgument('Используйте `.mod add @user/id semi/low/dope` или `.mod rem @user/id`.')
    if action == 'add' and level == 'dope' and db.execute('SELECT 1 FROM bans WHERE guild=? AND user=? AND kind="rape"', (ctx.guild.id, uid)).fetchone():
        raise commands.BadArgument('Сначала снимите существующий rape-бан командой .unrape.')
    db.execute('DELETE FROM access WHERE guild=? AND user=? AND kind IN ("semi", "low", "dope")', (ctx.guild.id, uid))
    if action == 'add':
        db.execute('INSERT INTO access VALUES (?,?,?)', (ctx.guild.id, uid, level))
    db.commit()
    await ctx.send(f'Модератор обновлён для `{uid}`.')


@bot.command(name='list')
@require(1)
async def list_all(ctx):
    gid = ctx.guild.id
    lines = []
    for kind in ('wl', 'ceo', 'semi', 'dope', 'low'):
        lines.append(f'{kind}: ' + names(db.execute('SELECT user FROM access WHERE guild=? AND kind=?', (gid, kind)).fetchall()))
    rows = db.execute('SELECT user,kind,expires FROM bans WHERE guild=? ORDER BY expires', (gid,)).fetchall()
    lines.append('Временные баны: ' + (', '.join(f'`{u}` ({k}, до <t:{e}:F>)' for u, k, e in rows) or 'пусто'))
    await send_chunks(ctx, '\n'.join(lines))


async def make_ban(ctx, target, days, reason, kind):
    uid = user_id(target)
    if kind == 'rape' and has(ctx.guild.id, uid, 'dope'):
        raise commands.BadArgument('Модератор dope защищён от rape-бана.')
    target_ok(ctx, uid)
    if not 1 <= days <= 3650:
        raise commands.BadArgument('Срок должен быть от 1 до 3650 дней.')
    if db.execute('SELECT 1 FROM bans WHERE guild=? AND user=?', (ctx.guild.id, uid)).fetchone():
        raise commands.BadArgument('У пользователя уже есть бан бота; сначала снимите его.')
    expires = int(time.time()) + days * 86400
    await ctx.guild.ban(discord.Object(id=uid), reason=f'{kind}: {reason} (модератор {ctx.author.id})', delete_message_seconds=0)
    db.execute('INSERT INTO bans VALUES (?,?,?,?,?)', (ctx.guild.id, uid, kind, expires, reason))
    db.commit()
    await ctx.send(f'`{uid}` забанен до <t:{expires}:F>. Причина: {discord.utils.escape_mentions(reason)}')


@bot.command(name='aban')
@require(2)
async def aban(ctx, target: str, days: int, *, reason: str):
    await make_ban(ctx, target, days, reason, 'aban')


@bot.command(name='rape')
@require(2)
async def rape(ctx, target: str, days: int, *, reason: str):
    await make_ban(ctx, target, days, reason, 'rape')


async def remove_ban(ctx, target, kind):
    uid = user_id(target)
    row = db.execute('SELECT 1 FROM bans WHERE guild=? AND user=? AND kind=?', (ctx.guild.id, uid, kind)).fetchone()
    if not row:
        raise commands.BadArgument('Такой активный бан бота не найден.')
    try:
        await ctx.guild.unban(discord.Object(id=uid), reason=f'{kind} снят модератором {ctx.author.id}')
    except discord.NotFound:
        pass
    db.execute('DELETE FROM bans WHERE guild=? AND user=? AND kind=?', (ctx.guild.id, uid, kind))
    db.commit()
    await ctx.send(f'Бан {kind} для `{uid}` снят.')


@bot.command(name='aunban')
@require(2)
async def aunban(ctx, target: str):
    await remove_ban(ctx, target, 'aban')


@bot.command(name='unrape')
@require(2)
async def unrape(ctx, target: str):
    await remove_ban(ctx, target, 'rape')


@bot.command(name='unrole')
@require(1)
async def unrole(ctx, target: str, role_id: int):
    uid = user_id(target)
    target_ok(ctx, uid)
    member = ctx.guild.get_member(uid)
    role = ctx.guild.get_role(role_id)
    if not member or not role or role not in member.roles:
        raise commands.BadArgument('Участник или его роль не найдены на сервере.')
    if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
        raise commands.BadArgument('Нельзя снять роль выше вашей или равную ей.')
    # An authorized removal updates the protection snapshot first.
    removed = db.execute('DELETE FROM protected_roles WHERE guild=? AND user=? AND role=?', (ctx.guild.id, uid, role_id)).rowcount
    db.commit()
    try:
        await member.remove_roles(role, reason=f'Команда unrole: {ctx.author.id}')
    except Exception:
        if removed:
            db.execute('INSERT OR IGNORE INTO protected_roles VALUES (?,?,?)', (ctx.guild.id, uid, role_id))
            db.commit()
        raise
    await ctx.send(f'Роль `{role_id}` снята у `{uid}`.')


@bot.command(name='прота')
@require(2)
async def protect(ctx, target: str):
    if target.lower() == 'list':
        return await send_chunks(ctx, 'Защита ролей: ' + names(db.execute('SELECT user FROM access WHERE guild=? AND kind="protect"', (ctx.guild.id,)).fetchall()))
    uid = user_id(target)
    member = ctx.guild.get_member(uid)
    if not member:
        raise commands.BadArgument('Пользователь должен находиться на сервере.')
    set_access(ctx.guild.id, uid, 'protect', True)
    db.execute('DELETE FROM protected_roles WHERE guild=? AND user=?', (ctx.guild.id, uid))
    db.executemany('INSERT OR IGNORE INTO protected_roles VALUES (?,?,?)', [(ctx.guild.id, uid, r.id) for r in member.roles if not r.is_default() and not r.managed])
    db.commit()
    await ctx.send(f'Защита ролей включена для `{uid}`.')


@bot.command(name='анпрота')
@require(2)
async def unprotect(ctx, target: str):
    uid = user_id(target)
    set_access(ctx.guild.id, uid, 'protect', False)
    db.execute('DELETE FROM protected_roles WHERE guild=? AND user=?', (ctx.guild.id, uid))
    db.commit()
    await ctx.send(f'Защита ролей выключена для `{uid}`.')


@bot.event
async def on_member_update(before, after):
    if before.roles == after.roles or not has(after.guild.id, after.id, 'protect'):
        return
    gid, uid = after.guild.id, after.id
    old_ids = {r.id for r in before.roles}
    new_ids = {r.id for r in after.roles}
    added = [r.id for r in after.roles if r.id not in old_ids and not r.is_default() and not r.managed]
    db.executemany('INSERT OR IGNORE INTO protected_roles VALUES (?,?,?)', [(gid, uid, rid) for rid in added])
    db.commit()
    stored = {row[0] for row in db.execute('SELECT role FROM protected_roles WHERE guild=? AND user=?', (gid, uid))}
    missing = [after.guild.get_role(rid) for rid in stored - new_ids]
    missing = [r for r in missing if r and not r.managed and r < after.guild.me.top_role]
    if missing:
        try:
            await after.add_roles(*missing, reason='Защита ролей')
        except discord.HTTPException:
            logging.exception('Failed to restore roles for %s in %s', uid, gid)


@bot.event
async def on_member_join(member):
    gid, uid = member.guild.id, member.id
    if has(gid, uid, 'wl') or uid == member.guild.owner_id:
        return
    active = db.execute('SELECT reason FROM bans WHERE guild=? AND user=? AND kind="rape" AND expires>?', (gid, uid, int(time.time()))).fetchone()
    if active or has(gid, uid, 'blacklist'):
        try:
            await member.ban(reason='Активный rape-бан или чёрный список', delete_message_seconds=0)
        except discord.HTTPException:
            logging.exception('Failed to ban joining user %s in %s', uid, gid)


@tasks.loop(seconds=60)
async def expire_bans():
    rows = db.execute('SELECT guild,user,kind FROM bans WHERE expires<=?', (int(time.time()),)).fetchall()
    for gid, uid, kind in rows:
        guild = bot.get_guild(gid)
        if not guild:
            continue
        # Blacklist and a separately recorded active ban continue to apply.
        other = db.execute('SELECT 1 FROM bans WHERE guild=? AND user=? AND expires>?', (gid, uid, int(time.time()))).fetchone()
        if not other and not has(gid, uid, 'blacklist'):
            try:
                await guild.unban(discord.Object(id=uid), reason=f'Истёк срок {kind}')
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logging.exception('Failed to expire ban for %s in %s', uid, gid)
                continue
        db.execute('DELETE FROM bans WHERE guild=? AND user=? AND kind=?', (gid, uid, kind))
        db.commit()


@expire_bans.before_loop
async def before_expire():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    if not expire_bans.is_running():
        expire_bans.start()
    logging.info('Logged in as %s', bot.user)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        message = f'Не хватает аргумента: `{error.param.name}`.'
    elif isinstance(error, (commands.BadArgument, commands.CheckFailure)):
        message = str(error) or 'Недостаточно прав или команда недоступна в личных сообщениях.'
    elif isinstance(getattr(error, 'original', None), discord.Forbidden):
        message = 'Discord отказал в действии. Проверьте права и положение роли бота.'
    elif isinstance(getattr(error, 'original', None), discord.HTTPException):
        message = 'Discord не смог выполнить действие. Проверьте данные и права бота.'
        logging.error('Discord error: %s', error.original)
    else:
        logging.error('Command error', exc_info=(type(error), error, error.__traceback__))
        message = 'Не удалось выполнить команду. Подробности в журнале бота.'
    await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())


if __name__ == '__main__':
    if not TOKEN:
        raise SystemExit('Укажите DISCORD_TOKEN в переменных окружения или файле .env')
    bot.run(TOKEN, log_handler=None)
