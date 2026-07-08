"""Graagmakx parle goblin quand on le mentionne.

Aucun intent supplémentaire requis : la détection passe par message.mentions
(métadonnée toujours fournie), pas par le contenu du message.
"""
from __future__ import annotations

import random

import discord
from discord.ext import commands

from . import timeutil as tu
from .core import CycleManager

PHRASES = [
    "Grrk ?! Quoi toi vouloir, grand-pieds ?!",
    "Graagmakx pas déranger ! Graagmakx compter champignons !!",
    "Toi parler à Graagmakx ?! Toi apporter viande d'abord !",
    "Skrii skrii ! Chef pas là ! Chef parti mordre choses !",
    "Hnk hnk hnk... toi sentir comme paladin. Graagmakx pas aimer paladins.",
    "QUOI ?! Graagmakx dormait dans le sac du magicien !",
    "Toi vouloir trésor ?? PAS DE TRÉSOR ! Juste cailloux brillants. À MOI les cailloux !",
    "Graagmakx écouter... mais Graagmakx comprendre RIEN. Comme d'habitude !",
    "Chhht ! Toi réveiller le dragon, toi expliquer au dragon !",
    "Gnark ! Toi lancer dé, toi faire 1, Graagmakx rigoler ! HNK HNK HNK !",
    "Graagmakx pas bot ! Graagmakx VRAI goblin dans machine ! Machine confortable.",
    "Toi encore là ?! Graagmakx facturer en rations maintenant !",
]

# Variantes qui glissent la date de la prochaine séance ({date})
SESSION_PHRASES = [
    "Grrk ! Toi préparer épée ! Grande baston prévue {date} ! Graagmakx regarder de loin.",
    "Skrii ! Chef-qui-raconte-histoires dit : prochain massacre {date} ! Toi voter, sinon Graagmakx mordre !",
    "Hnk hnk ! {date}, grand feu de camp et dés qui roulent ! Graagmakx voler rations pendant ce temps.",
    "Graagmakx savoir secret... prochaine séance {date} ! Toi cliquer petite coche verte, oui oui !",
]


class GoblinTalk(commands.Cog):
    def __init__(self, bot: commands.Bot, manager: CycleManager):
        self.bot = bot
        self.manager = manager

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Uniquement en serveur, jamais sur les bots, jamais via @everyone/@here
        if message.author.bot or message.guild is None:
            return
        if message.mention_everyone or self.bot.user not in message.mentions:
            return

        s = await self.manager.db.get_active_session()
        if s and random.random() < 0.35:
            # Une fois sur trois environ, le goblin lâche la date de la séance
            dt = tu.parse(s["scheduled_at"])
            txt = random.choice(SESSION_PHRASES).format(date=tu.ts(dt, "D"))
        else:
            txt = random.choice(PHRASES)
        await message.reply(txt, mention_author=False)


async def setup_goblin(bot: commands.Bot, manager: CycleManager) -> None:
    await bot.add_cog(GoblinTalk(bot, manager))
