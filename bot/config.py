"""Configuration du bot — tout provient des variables d'environnement.

On référence le canal et le rôle par ID (et non par nom) : c'est immunisé
contre les renommages et les problèmes d'accents (#général).
"""
import os


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Variable d'environnement manquante : {name}")
    return val


DISCORD_TOKEN: str = _req("DISCORD_TOKEN")
GUILD_ID: int = int(_req("GUILD_ID"))
CHANNEL_ID: int = int(_req("CHANNEL_ID"))          # canal #général
GM_USER_ID: int = int(_req("GM_USER_ID"))          # toi, le MJ
PLAYER_ROLE_ID: int = int(_req("PLAYER_ROLE_ID"))  # rôle @Player
DB_PATH: str = os.environ.get("DB_PATH", "/app/data/bot.db")

# Émojis du sondage
EMOJI_YES = "✅"
EMOJI_NO = "❌"

# Horaires du cycle (heure de Paris) — cf. points validés en phase 1
SESSION_HOUR = 20            # séance samedi 20 h
ANNOUNCE_HOUR = 10           # annonce le dimanche à 10 h
DM_REMINDER_DAYS = 7         # relance DM 7 jours après l'annonce
MONDAY_REMINDER_HOUR = 18    # rappel du lundi à 18 h
CONSULT_TIMEOUT_H = 48       # timeout de consultation GM
CONSULT_CAP_HOUR = 19        # ... plafonné au samedi 19 h (point n° 3)
