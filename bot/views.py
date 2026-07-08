"""Boutons du DM de consultation GM.

Vue persistante (timeout=None + custom_id fixes) : les boutons restent
fonctionnels après un redémarrage du bot. La deadline de 48 h est la nôtre,
appliquée côté base par la boucle `tick()` — pas par le timeout Discord.
"""
from __future__ import annotations

import discord

from . import config


class ConsultView(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=None)
        self.manager = manager

    async def _handle(self, interaction: discord.Interaction, approved: bool) -> None:
        if interaction.user.id != config.GM_USER_ID:
            await interaction.response.send_message(
                "Seul le MJ peut répondre à cette consultation.", ephemeral=True
            )
            return
        s = await self.manager.db.get_active_session()
        pending = (
            await self.manager.db.get_pending_consultation(s["id"]) if s else None
        )
        # Point n° 2 : clic après timeout/résolution, ou sur un ancien DM → refusé
        if not pending or pending["dm_message_id"] != interaction.message.id:
            await interaction.response.edit_message(
                content=f"{interaction.message.content}\n\n"
                        "⌛ Trop tard : cette consultation n'est plus active.",
                view=None,
            )
            return
        await interaction.response.defer()
        await self.manager.resolve_consultation(pending, approved=approved)

    @discord.ui.button(
        label="Valider le report",
        style=discord.ButtonStyle.success,
        custom_id="dnd:consult:approve",
    )
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle(interaction, approved=True)

    @discord.ui.button(
        label="Refuser (séance maintenue)",
        style=discord.ButtonStyle.danger,
        custom_id="dnd:consult:refuse",
    )
    async def refuse(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._handle(interaction, approved=False)
