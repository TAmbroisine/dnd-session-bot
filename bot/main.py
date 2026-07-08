"""Point d'entrée : câblage bot + DB + boucle de planification."""
from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from . import config
from .commands import setup_commands
from .core import CycleManager
from .db import Database
from .events import setup_events
from .views import ConsultView

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dnd-bot")

intents = discord.Intents.default()
intents.members = True  # intent privilégié : rôles @Player + DM (à activer sur le portail)


class DndBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(config.DB_PATH)
        self.manager = CycleManager(self, self.db)
        self._synced = False

    async def setup_hook(self) -> None:
        await self.db.init()
        # Vue persistante : les boutons de consultation survivent aux redémarrages
        self.add_view(ConsultView(self.manager))
        await setup_commands(self, self.manager)
        await setup_events(self, self.manager)
        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (guild %s)", self.user, config.GUILD_ID)
        if not self._synced:
            self._synced = True
            # Rattrape les votes émis pendant un éventuel downtime
            await self.manager.resync_votes()
            self.scheduler.start()

    @tasks.loop(seconds=60)
    async def scheduler(self) -> None:
        try:
            await self.manager.tick()
        except Exception:
            log.exception("Erreur dans la boucle de planification")

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    DndBot().run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
