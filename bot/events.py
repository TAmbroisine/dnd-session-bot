"""Gestion des réactions = gestion des votes.

Règles appliquées (points validés en phase 1) :
- n° 1  : double vote → dernier vote gagne, l'autre réaction est retirée ;
- n° 8  : votes après clôture → réaction retirée, sans effet ;
- n° 11 : seules les réactions des membres @Player comptent, les autres
          sont retirées (le MJ inclus — il dispose de /postpone).
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from . import config, timeutil as tu
from .core import CycleManager

log = logging.getLogger("dnd-bot")

PAIR = {config.EMOJI_YES: "yes", config.EMOJI_NO: "no"}
OTHER = {config.EMOJI_YES: config.EMOJI_NO, config.EMOJI_NO: config.EMOJI_YES}


class ReactionEvents(commands.Cog):
    def __init__(self, bot: commands.Bot, manager: CycleManager):
        self.bot = bot
        self.manager = manager

    async def _poll_for(self, s: dict, message_id: int) -> str | None:
        if message_id == s["message_id"]:
            return "cur"
        if s["old_message_id"] and message_id == s["old_message_id"]:
            return "old"
        return None

    async def _remove(self, message_id: int, emoji: str, member: discord.abc.User):
        try:
            msg = await self.manager.channel().fetch_message(message_id)
            await msg.remove_reaction(emoji, member)
        except discord.HTTPException:
            pass

    def _poll_locked(self, s: dict, poll: str) -> bool:
        if poll == "cur":
            return bool(s["locked"])
        # L'ancien sondage se fige au vendredi précédant la date d'origine
        return tu.now() > tu.friday_deadline(tu.parse(s["old_scheduled_at"]))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        emoji = str(payload.emoji)
        if emoji not in PAIR:
            return
        s = await self.manager.db.get_active_session()
        if not s:
            return
        poll = await self._poll_for(s, payload.message_id)
        if poll is None:
            return

        member = payload.member
        if member is None or member.bot:
            return
        if not self.manager.is_player(member):
            await self._remove(payload.message_id, emoji, member)  # point n° 11
            return
        if self._poll_locked(s, poll):
            await self._remove(payload.message_id, emoji, member)  # point n° 8
            return

        value = PAIR[emoji]
        prev = await self.manager.db.get_vote(s["id"], poll, member.id)
        await self.manager.db.set_vote(s["id"], poll, member.id, value)
        if prev and prev != value:
            # Point n° 1 : retire l'ancienne réaction (dernier vote gagne)
            await self._remove(payload.message_id, OTHER[emoji], member)
        log.info("Vote %s : %s → %s (sondage %s)", s["id"], member.display_name, value, poll)
        await self.manager.after_vote_change(poll)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        emoji = str(payload.emoji)
        if emoji not in PAIR:
            return
        s = await self.manager.db.get_active_session()
        if not s:
            return
        poll = await self._poll_for(s, payload.message_id)
        if poll is None or self._poll_locked(s, poll):
            return
        # On n'efface le vote que si la réaction retirée correspond au vote
        # enregistré (sinon c'est le bot qui vient de retirer l'ancienne
        # réaction lors d'un changement de vote).
        current = await self.manager.db.get_vote(s["id"], poll, payload.user_id)
        if current == PAIR[emoji]:
            await self.manager.db.delete_vote(s["id"], poll, payload.user_id)
            await self.manager.after_vote_change(poll)


async def setup_events(bot: commands.Bot, manager: CycleManager) -> None:
    await bot.add_cog(ReactionEvents(bot, manager))
