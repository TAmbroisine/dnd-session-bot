"""Commandes slash d'administration — réservées au MJ (GM_USER_ID)."""
from __future__ import annotations

from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from . import config, timeutil as tu
from .core import CycleManager
from .db import absents_from_json


def gm_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id != config.GM_USER_ID:
            await interaction.response.send_message(
                "⛔ Commande réservée au MJ.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def parse_date(raw: str) -> datetime | None:
    """Parse `JJ/MM/AAAA` → samedi 20 h Europe/Paris."""
    try:
        d = datetime.strptime(raw.strip(), "%d/%m/%Y")
    except ValueError:
        return None
    return d.replace(hour=config.SESSION_HOUR, tzinfo=tu.TZ)


class AdminCommands(commands.Cog):
    def __init__(self, bot: commands.Bot, manager: CycleManager):
        self.bot = bot
        self.manager = manager

    # ------------------------------------------------------------- /init

    @app_commands.command(name="init", description="Initialiser le cycle avec la date de la première séance (JJ/MM/AAAA)")
    @app_commands.describe(date="Date de la première séance, format JJ/MM/AAAA")
    @gm_only()
    async def init_cmd(self, interaction: discord.Interaction, date: str):
        dt = parse_date(date)
        if not dt:
            await interaction.response.send_message(
                "Format invalide — attendu : `JJ/MM/AAAA` (ex. `18/07/2026`).",
                ephemeral=True,
            )
            return
        warn = ""
        if dt.weekday() != 5:  # point n° 9 : non-samedi accepté avec avertissement
            warn = "\n⚠️ Attention : cette date n'est pas un samedi."
        if dt < tu.now():
            await interaction.response.send_message(
                "Cette date est déjà passée.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.manager.start_cycle(dt)
        await interaction.followup.send(
            f"Cycle initialisé : première séance le {tu.ts(dt)}.{warn}", ephemeral=True
        )

    # ----------------------------------------------------------- /status

    @app_commands.command(name="status", description="État du cycle en cours (date, votes, reports)")
    @gm_only()
    async def status_cmd(self, interaction: discord.Interaction):
        s = await self.manager.db.get_active_session()
        if not s:
            await interaction.response.send_message(
                "Aucun cycle en cours — lance `/init`.", ephemeral=True
            )
            return
        dt = tu.parse(s["scheduled_at"])
        votes = await self.manager.db.get_votes(s["id"], "cur")
        yes = [u for u, v in votes.items() if v == "yes"]
        no = [u for u, v in votes.items() if v == "no"]
        pending = await self.manager.db.get_pending_consultation(s["id"])
        handled = await self.manager.db.get_handled(s["id"])
        lazy = await self.manager.non_voters(s)

        e = discord.Embed(title="État du cycle D&D", color=discord.Color.dark_purple())
        e.add_field(name="Prochaine séance", value=f"{tu.ts(dt)} ({tu.ts(dt, 'R')})", inline=False)
        e.add_field(name=f"{config.EMOJI_YES} Présents", value=self.manager.names(yes))
        e.add_field(name=f"{config.EMOJI_NO} Absents", value=self.manager.names(no))
        e.add_field(name="🕐 N'ont pas voté", value=", ".join(m.display_name for m in lazy) or "—")
        e.add_field(
            name="Votes",
            value="🔒 clos" if s["locked"] else f"ouverts jusqu'au {tu.ts(tu.friday_deadline(dt), 'f')}",
            inline=False,
        )
        if pending:
            e.add_field(
                name="⚠️ Consultation en attente",
                value=f"Réponse attendue avant {tu.ts(tu.parse(pending['deadline']))}",
                inline=False,
            )
        if handled:
            e.add_field(
                name="Séance maintenue malgré",
                value=self.manager.names(sorted(handled)),
                inline=False,
            )
        if s["old_message_id"]:
            old_dt = tu.parse(s["old_scheduled_at"])
            snapshot = absents_from_json(s["old_absents"])
            e.add_field(
                name="📆 Report actif",
                value=(
                    f"Date d'origine : {tu.ts(old_dt)}. Annulation possible si "
                    f"{self.manager.names(snapshot) or 'personne (report imposé)'} "
                    f"revote(nt) {config.EMOJI_YES} avant le {tu.ts(tu.friday_deadline(old_dt), 'f')}."
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ---------------------------------------------------------- /setdate

    @app_commands.command(name="setdate", description="Modifier manuellement la date de la prochaine séance (JJ/MM/AAAA)")
    @app_commands.describe(date="Nouvelle date, format JJ/MM/AAAA")
    @gm_only()
    async def setdate_cmd(self, interaction: discord.Interaction, date: str):
        s = await self.manager.db.get_active_session()
        if not s:
            await interaction.response.send_message("Aucun cycle en cours.", ephemeral=True)
            return
        dt = parse_date(date)
        if not dt or dt < tu.now():
            await interaction.response.send_message(
                "Date invalide ou passée (format `JJ/MM/AAAA`).", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        # Point n° 9 : /setdate réinitialise tout état en cours (report, consultation)
        pending = await self.manager.db.get_pending_consultation(s["id"])
        if pending:
            await self.manager.db.update_consultation(pending["id"], status="superseded")
            await self.manager._finish_consult_dm(pending, "↪️ Consultation annulée : date modifiée manuellement.")
        await self.manager.db.update_session(
            s["id"], scheduled_at=tu.iso(dt),
            old_scheduled_at=None, old_message_id=None,
            old_announced_at=None, old_absents=None,
        )
        await self.manager.post_announcement(
            s["id"], note="🛠️ Le MJ a modifié la date de la séance — nouveau sondage :"
        )
        warn = "\n⚠️ Ce n'est pas un samedi." if dt.weekday() != 5 else ""
        await interaction.followup.send(f"Date modifiée : {tu.ts(dt)}.{warn}", ephemeral=True)

    # ----------------------------------------------------------- /cancel

    @app_commands.command(name="cancel", description="Annuler la séance en cours et passer à la suivante (+2 semaines)")
    @gm_only()
    async def cancel_cmd(self, interaction: discord.Interaction):
        s = await self.manager.db.get_active_session()
        if not s:
            await interaction.response.send_message("Aucun cycle en cours.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        dt = tu.parse(s["scheduled_at"])
        pending = await self.manager.db.get_pending_consultation(s["id"])
        if pending:
            await self.manager.db.update_consultation(pending["id"], status="superseded")
            await self.manager._finish_consult_dm(pending, "↪️ Consultation annulée : séance annulée par le MJ.")
        await self.manager.db.update_session(s["id"], status="cancelled")
        await self.manager.channel().send(
            f"🚫 La séance du {tu.ts(dt)} est **annulée** par le MJ."
        )
        # Point n° 13 : la séance suivante est annoncée immédiatement
        from datetime import timedelta
        sid = await self.manager.db.create_session(tu.iso(dt + timedelta(days=14)))
        await self.manager.post_announcement(sid)
        await interaction.followup.send("Séance annulée, suivante annoncée.", ephemeral=True)

    # --------------------------------------------------------- /postpone

    @app_commands.command(name="postpone", description="Imposer un report de +2 semaines sans sondage")
    @gm_only()
    async def postpone_cmd(self, interaction: discord.Interaction):
        s = await self.manager.db.get_active_session()
        if not s:
            await interaction.response.send_message("Aucun cycle en cours.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.manager.apply_postpone(s, forced=True)
        s = await self.manager.db.get_active_session()
        await interaction.followup.send(
            f"Report imposé : séance déplacée au {tu.ts(tu.parse(s['scheduled_at']))}.",
            ephemeral=True,
        )


async def setup_commands(bot: commands.Bot, manager: CycleManager) -> None:
    await bot.add_cog(AdminCommands(bot, manager))
