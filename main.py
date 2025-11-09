import discord
from discord.ext import commands
from discord.utils import get
from collections import defaultdict
from datetime import datetime
import asyncio
import time
from keep_alive import keep_alive
import os

# --- CONFIGURATION GÉNÉRALE ---
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- PARAMÈTRES MUTING PAR RÉACTION 🚨 ---
MUTE_ROLE_NAME = "Muted"
LOG_CHANNEL_NAME = "modlogs"
MUTE_DURATION = 30 * 60      # 30 minutes
COOLDOWN_TIME = 3 * 60       # Cooldown signalement par réaction
cooldown = {}

# --- PARAMÈTRES SUPPRESSION PAR RÉACTION ⚔️ ---
DELETE_EMOJI = "⚔️"
DELETE_THRESHOLD = 3  # nombre de réactions nécessaires

# --- PARAMÈTRES REPORT MANUEL ---
BAN_THRESHOLD = 3             # Nombre de reports avant ban
REPORT_WINDOW = 20 * 60       # 20 min
REPORT_COOLDOWN = 60          # 1 min entre deux reports du même utilisateur sur la même cible
TEMP_BAN_DURATION = 30 * 60   # 30 min
mentions = defaultdict(list)
user_cooldowns = defaultdict(dict)

# --- RÔLES EXCLUS ---
EXCLUDED_ROLES = ["Modération", "Modérateur en test"]

# --- UTILITAIRE POUR RÔLES PROTÉGÉS ---
def is_protected(member):
    return any(role.name in EXCLUDED_ROLES for role in member.roles)

# --- ÉVÉNEMENT PRÊT ---
@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user} ({bot.user.id})")
    print("🚀 Bot prêt à détecter 🚨 et ⚔️ et à gérer les reports !")

# =======================================================
# 🔹 SYSTÈME DE RÉACTION 🚨 -> MUTE / ⚔️ -> DELETE
# =======================================================
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    message = await reaction.message.channel.fetch_message(reaction.message.id)
    emoji = str(reaction.emoji)
    now = time.time()

    # --- Cooldown commun pour toutes les réactions ---
    last = cooldown.get((user.id, emoji), 0)
    if now - last < COOLDOWN_TIME:
        try:
            await reaction.remove(user)
            print(f"⏳ {user} est encore en cooldown de report pour {emoji}.")
        except discord.Forbidden:
            pass
        return
    cooldown[(user.id, emoji)] = now

    guild = message.guild
    member = guild.get_member(message.author.id)
    log_channel = get(guild.text_channels, name=LOG_CHANNEL_NAME)

    # --- 🚨 Mute automatique ---
    if emoji == "🚨":
        updated_reaction = get(message.reactions, emoji="🚨")
        if updated_reaction and updated_reaction.count >= 3:
            if not member:
                return
            if member.guild_permissions.administrator or is_protected(member):
                await message.reply(f"⚠️ {member.mention} ne peut pas être mute (admin ou rôle protégé).")
                if log_channel:
                    await log_channel.send(f"⚠️ Tentative de mute bloquée sur {member.mention} (admin ou rôle protégé).")
                return

            # Création du rôle "Muted" si inexistant
            mute_role = get(guild.roles, name=MUTE_ROLE_NAME)
            if mute_role is None:
                print("🛠️ Création du rôle 'Muted'...")
                mute_role = await guild.create_role(name=MUTE_ROLE_NAME, color=discord.Color.greyple())
                for channel in guild.channels:
                    await channel.set_permissions(mute_role, send_messages=False, speak=False, add_reactions=False)

            await member.add_roles(mute_role, reason="3 signalements 🚨")
            try:
                await member.send(
                    f"🚫 Tu as été **mute** pendant 30 minutes sur **{guild.name}** suite à plusieurs signalements 🚨.\n"
                    f"👉 Message signalé : {message.jump_url}"
                )
            except Exception:
                pass

            await message.reply(f"🚫 {member.mention} a été mute 30 min suite à plusieurs signalements.")
            print(f"🔇 {member} a été mute 30 min (3 🚨)")

            if log_channel:
                await log_channel.send(
                    f"🔇 **Mute automatique** : {member.mention}\n"
                    f"📩 Message : {message.jump_url}\n"
                    f"🕒 Durée : 30 minutes"
                )

            await asyncio.sleep(MUTE_DURATION)
            if mute_role in member.roles:
                await member.remove_roles(mute_role, reason="Fin du mute automatique")
                print(f"🔊 {member} est démute automatiquement.")
                if log_channel:
                    await log_channel.send(f"🔊 **Démute automatique** : {member.mention} après 30 min.")

    # --- ⚔️ Suppression de message ---
    elif emoji == DELETE_EMOJI:
        updated_reaction = get(message.reactions, emoji=DELETE_EMOJI)
        if updated_reaction and updated_reaction.count >= DELETE_THRESHOLD:
            try:
                await message.delete()
                print(f"🗡️ Message de {member} supprimé après {DELETE_THRESHOLD} ⚔️")
                if log_channel:
                    await log_channel.send(f"🗡️ **Message supprimé** : {member.mention} ({message.jump_url})")
            except discord.Forbidden:
                if log_channel:
                    await log_channel.send(f"❌ Impossible de supprimer le message de {member.mention}.")
            except discord.NotFound:
                pass

# =======================================================
# 🔹 COMMANDE !report -> BAN TEMPORAIRE
# =======================================================
@bot.command()
async def report(ctx, member: discord.Member):
    """Signale un membre. Si BAN_THRESHOLD reports dans REPORT_WINDOW => ban temporaire."""
    reporter = ctx.author
    now = datetime.utcnow()

    # --- Vérifications de base ---
    if member.id == reporter.id:
        return await ctx.send(f"❌ Tu ne peux pas te signaler toi-même, {reporter.mention}.")
    if member.bot:
        return await ctx.send(f"❌ Tu ne peux pas signaler un bot, {reporter.mention}.")
    if is_protected(member):
        return await ctx.send(f"⚠️ {member.mention} a un rôle protégé, tu ne peux pas le signaler.")

    last = user_cooldowns[reporter.id].get(member.id)
    if last and (now - last).total_seconds() < REPORT_COOLDOWN:
        remaining = int(REPORT_COOLDOWN - (now - last).total_seconds())
        return await ctx.send(f"⏳ Tu as déjà signalé {member.mention}. Réessaie dans {remaining}s.")

    # --- Ajout du report ---
    mentions[member.id].append(now)
    user_cooldowns[reporter.id][member.id] = now

    # Nettoyage des anciens reports
    mentions[member.id] = [t for t in mentions[member.id] if (now - t).total_seconds() < REPORT_WINDOW]
    count = len(mentions[member.id])
    left = max(0, BAN_THRESHOLD - count)

    await ctx.send(f"✅ {member.mention} signalé par {reporter.mention} — {count}/{BAN_THRESHOLD} reports (valables {REPORT_WINDOW//60} min).")

    # --- Si le seuil est atteint ---
    if count >= BAN_THRESHOLD:
        log_channel = get(ctx.guild.text_channels, name=LOG_CHANNEL_NAME)
        try:
            try:
                await member.send(f"⚠️ Tu as été **banni temporairement** de **{ctx.guild.name}** suite à plusieurs signalements. Durée : {TEMP_BAN_DURATION//60} minutes.")
            except Exception:
                pass

            await ctx.guild.ban(member, reason=f"Auto-ban temporaire : {count} reports en {REPORT_WINDOW//60} min")
            await ctx.send(f"🚫 {member.mention} a été banni temporairement 30 min suite à {count} reports.")
            mentions[member.id] = []

            if log_channel:
                await log_channel.send(
                    f"🚫 **Ban automatique** : {member.mention}\n"
                    f"📆 Durée : 30 minutes\n"
                    f"👮‍♂️ Déclenché par : {reporter.mention}"
                )

            async def unban_later():
                await asyncio.sleep(TEMP_BAN_DURATION)
                try:
                    await ctx.guild.unban(discord.Object(id=member.id))
                    if log_channel:
                        await log_channel.send(f"✅ **Unban automatique** : `{member}` après 30 minutes.")
                except Exception:
                    pass

            bot.loop.create_task(unban_later())

        except discord.Forbidden:
            await ctx.send(f"❌ Je n'ai pas la permission de bannir {member.mention}.")
        except discord.HTTPException as e:
            await ctx.send(f"❌ Erreur lors du ban : {e}")
    else:
        await ctx.send(f"ℹ️ Encore {left} report(s) nécessaires avant un ban temporaire.")

# --- Démarrage du bot ---
token = os.getenv("DISCORD_BOT_TOKEN")
if not token:
    raise ValueError("DISCORD_BOT_TOKEN environment variable is not set!")

keep_alive()  # lance le serveur Flask en arrière-plan
bot.run(token)



