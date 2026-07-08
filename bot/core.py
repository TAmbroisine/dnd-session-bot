"""Cœur du bot : implémentation de la machine à états validée en phase 1.

États (portés par la table `sessions` + `consultations`) :
- Inactif           : aucune session `active` en base.
- Sondage ouvert    : session active, locked=0, pas de consultation pending.
- Consultation GM   : une ligne `consultations` au statut pending.
- Séance maintenue  : sondage ouvert + `handled_absents` non vide (les ❌ déjà
                      refusés ne redéclenchent rien ; un NOUVEAU ❌ si).
- Séance reportée   : champs old_* renseignés → veille d'annulation active
                      sur l'ancien sondage jusqu'au vendredi précédant la
                      date d'origine.
- Votes clos        : locked=1 (vendredi 23 h 59 passé).
- Séance jouée      : status='played' ; le dimanche 10 h, la séance suivante
                      (+2 semaines) est créée et annoncée → boucle.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import discord

from . import config, timeutil as tu
from .db import Database, absents_from_json, absents_to_json

log = logging.getLogger("dnd-bot")


class CycleManager:
    def __init__(self, bot: discord.Client, db: Database):
        self.bot = bot
        self.db = db

    # ------------------------------------------------------------------ utils

    def guild(self) -> discord.Guild:
        return self.bot.get_guild(config.GUILD_ID)

    def channel(self) -> discord.TextChannel:
        return self.bot.get_channel(config.CHANNEL_ID)

    def player_role(self) -> discord.Role | None:
        return self.guild().get_role(config.PLAYER_ROLE_ID)

    def players(self) -> list[discord.Member]:
        role = self.player_role()
        return list(role.members) if role else []

    def is_player(self, member: discord.Member) -> bool:
        return any(r.id == config.PLAYER_ROLE_ID for r in member.roles)

    async def gm(self) -> discord.User:
        return await self.bot.fetch_user(config.GM_USER_ID)

    def names(self, user_ids: list[int]) -> str:
        out = []
        for uid in user_ids:
            m = self.guild().get_member(uid)
            out.append(m.display_name if m else f"<@{uid}>")
        return ", ".join(out) if out else "—"

    def msg_link(self, message_id: int) -> str:
        return (
            f"https://discord.com/channels/{config.GUILD_ID}/"
            f"{config.CHANNEL_ID}/{message_id}"
        )

    async def non_voters(self, session: dict) -> list[discord.Member]:
        votes = await self.db.get_votes(session["id"], "cur")
        return [p for p in self.players() if p.id not in votes]

    # ------------------------------------------------------------ annonces

    async def post_announcement(self, session_id: int, note: str | None = None) -> None:
        """Poste (ou reposte) l'annonce + sondage dans #général et repart de zéro."""
        cur = await self.db.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        )
        s = dict(await cur.fetchone())
        dt = tu.parse(s["scheduled_at"])
        role = self.player_role()
        deadline = tu.friday_deadline(dt)

        lines = []
        if note:
            lines.append(note)
        lines += [
            f"{role.mention} 🎲 **Prochaine séance D&D** : {tu.ts(dt)} ({tu.ts(dt, 'R')}), de 20 h à minuit.",
            f"Votez avec {config.EMOJI_YES} présent / {config.EMOJI_NO} absent — "
            f"modifiable jusqu'au {tu.ts(deadline)}.",
        ]
        msg = await self.channel().send(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await msg.add_reaction(config.EMOJI_YES)
        await msg.add_reaction(config.EMOJI_NO)

        # Sondage vierge : reset complet des compteurs liés au sondage courant
        await self.db.clear_votes(session_id, "cur")
        await self.db.clear_handled(session_id)
        await self.db.update_session(
            session_id,
            message_id=msg.id,
            announced_at=tu.iso(tu.now()),
            locked=0,
            dm_reminder_sent=0,
            monday_reminder_sent=0,
        )
        log.info("Annonce postée pour la séance #%s (%s)", session_id, dt)

    async def start_cycle(self, dt) -> None:
        """/init : (ré)initialise le cycle et annonce immédiatement."""
        await self.db.close_all_active()
        sid = await self.db.create_session(tu.iso(dt))
        await self.post_announcement(sid, note="🚀 Cycle initialisé par le MJ.")

    # ------------------------------------------------------ votes & réactions

    async def after_vote_change(self, poll: str) -> None:
        """Appelé après toute modification de vote — fait avancer la machine à états."""
        s = await self.db.get_active_session()
        if not s:
            return

        if poll == "cur":
            votes = await self.db.get_votes(s["id"], "cur")
            absents = sorted(u for u, v in votes.items() if v == "no")
            pending = await self.db.get_pending_consultation(s["id"])

            if pending:
                if not absents:
                    # Point n° 6 : plus aucun ❌ pendant la consultation → annulée
                    await self.cancel_consultation(pending, s)
                else:
                    await self.update_consultation_dm(pending, s, absents)
            elif not s["locked"]:
                handled = await self.db.get_handled(s["id"])
                new_absents = [u for u in absents if u not in handled]
                if new_absents:
                    # 1er ❌ (ou nouveau ❌ après un refus) → consultation GM
                    await self.start_consultation(s, absents)

        elif poll == "old" and s["old_message_id"]:
            # Veille d'annulation de report (point n° 4)
            snapshot = absents_from_json(s["old_absents"])
            if not snapshot:
                return  # report imposé via /postpone : pas d'annulation auto
            votes = await self.db.get_votes(s["id"], "old")
            still_no = [u for u, v in votes.items() if v == "no"]
            all_flipped = all(votes.get(u) == "yes" for u in snapshot)
            if not still_no and all_flipped:
                await self.cancel_report(s)

    # ------------------------------------------------------ consultation GM

    async def start_consultation(self, s: dict, absents: list[int]) -> None:
        from .views import ConsultView  # import tardif (dépendance circulaire)

        dt = tu.parse(s["scheduled_at"])
        deadline = tu.consult_deadline(dt)
        cid = await self.db.create_consultation(
            s["id"], tu.iso(tu.now()), tu.iso(deadline)
        )
        new_dt = dt + timedelta(days=14)
        content = (
            f"⚠️ **Absence signalée** pour la séance du {tu.ts(dt)}.\n"
            f"Absent(s) : **{self.names(absents)}**\n\n"
            f"Valider le report au **{tu.ts(new_dt)}** ?\n"
            f"Sans réponse avant {tu.ts(deadline)} ({tu.ts(deadline, 'R')}), "
            f"le report sera **refusé automatiquement** et la séance maintenue."
        )
        try:
            dm = await (await self.gm()).send(content, view=ConsultView(self))
            await self.db.update_consultation(cid, dm_message_id=dm.id)
        except discord.Forbidden:
            # Fallback point n° 10 : DM du GM fermés → ping discret dans le canal
            await self.channel().send(
                f"<@{config.GM_USER_ID}> je n'arrive pas à t'envoyer de DM — "
                f"absence signalée, réponds via `/status` puis `/postpone` si besoin."
            )
        log.info("Consultation #%s ouverte (absents : %s)", cid, absents)

    async def update_consultation_dm(
        self, consult: dict, s: dict, absents: list[int]
    ) -> None:
        """❌ supplémentaire pendant une consultation : on met à jour le DM (point n° 5)."""
        if not consult["dm_message_id"]:
            return
        try:
            gm = await self.gm()
            dm_channel = gm.dm_channel or await gm.create_dm()
            msg = await dm_channel.fetch_message(consult["dm_message_id"])
            dt = tu.parse(s["scheduled_at"])
            new_dt = dt + timedelta(days=14)
            deadline = tu.parse(consult["deadline"])
            await msg.edit(
                content=(
                    f"⚠️ **Absence(s) signalée(s)** pour la séance du {tu.ts(dt)}.\n"
                    f"Absent(s) : **{self.names(absents)}**\n\n"
                    f"Valider le report au **{tu.ts(new_dt)}** ?\n"
                    f"Sans réponse avant {tu.ts(deadline)} ({tu.ts(deadline, 'R')}), "
                    f"le report sera **refusé automatiquement**."
                )
            )
        except discord.HTTPException:
            log.warning("Impossible de mettre à jour le DM de consultation")

    async def cancel_consultation(self, consult: dict, s: dict) -> None:
        await self.db.update_consultation(consult["id"], status="cancelled")
        await self._finish_consult_dm(consult, "✅ Plus aucun absent : consultation annulée, la séance est confirmée.")
        log.info("Consultation #%s annulée (plus d'absent)", consult["id"])

    async def resolve_consultation(
        self, consult: dict, approved: bool, via_timeout: bool = False
    ) -> None:
        s = await self.db.get_active_session()
        if not s or consult["session_id"] != s["id"]:
            return
        if approved:
            await self.db.update_consultation(consult["id"], status="approved")
            await self._finish_consult_dm(consult, "✅ Report **validé**.")
            await self.apply_postpone(s, forced=False)
        else:
            status = "timeout" if via_timeout else "refused"
            await self.db.update_consultation(consult["id"], status=status)
            # Les ❌ actuels ne redéclencheront plus de consultation
            votes = await self.db.get_votes(s["id"], "cur")
            absents = [u for u, v in votes.items() if v == "no"]
            await self.db.add_handled(s["id"], absents)
            reason = "délai de 48 h dépassé" if via_timeout else "refus du MJ"
            await self._finish_consult_dm(
                consult, f"❌ Report **refusé** ({reason}) : la séance est maintenue."
            )
            dt = tu.parse(s["scheduled_at"])
            await self.channel().send(
                f"ℹ️ La séance du {tu.ts(dt)} est **maintenue** malgré les absences signalées."
            )
        log.info("Consultation #%s résolue : approved=%s timeout=%s",
                 consult["id"], approved, via_timeout)

    async def _finish_consult_dm(self, consult: dict, result: str) -> None:
        """Retire les boutons du DM et affiche le résultat."""
        if not consult["dm_message_id"]:
            return
        try:
            gm = await self.gm()
            dm_channel = gm.dm_channel or await gm.create_dm()
            msg = await dm_channel.fetch_message(consult["dm_message_id"])
            await msg.edit(content=f"{msg.content}\n\n{result}", view=None)
        except discord.HTTPException:
            pass

    # ----------------------------------------------------------- report

    async def apply_postpone(self, s: dict, forced: bool) -> None:
        """Report de +2 semaines. `forced=True` = /postpone (pas d'annulation auto)."""
        old_dt = tu.parse(s["scheduled_at"])
        new_dt = old_dt + timedelta(days=14)

        # Snapshot des absents (base de la règle d'annulation, point n° 4)
        votes = await self.db.get_votes(s["id"], "cur")
        absents = [] if forced else sorted(u for u, v in votes.items() if v == "no")

        # Une consultation encore en attente devient obsolète
        pending = await self.db.get_pending_consultation(s["id"])
        if pending:
            await self.db.update_consultation(pending["id"], status="superseded")
            await self._finish_consult_dm(pending, "↪️ Consultation remplacée par un report imposé.")

        # L'ancien sondage passe sous veille d'annulation (si non forcé)
        await self.db.move_votes_cur_to_old(s["id"])
        await self.db.update_session(
            s["id"],
            scheduled_at=tu.iso(new_dt),
            old_scheduled_at=s["scheduled_at"],
            old_message_id=s["message_id"],
            old_announced_at=s["announced_at"],
            old_absents=absents_to_json(absents),
        )
        who = "à la demande du MJ" if forced else "suite au sondage"
        note = (
            f"📆 **Séance reportée** {who} : le {tu.ts(old_dt)} → le {tu.ts(new_dt)}.\n"
            f"Nouveau sondage ci-dessous, **tout le monde revote** ⤵️"
        )
        if absents:
            note += (
                f"\n(Si {self.names(absents)} repasse(nt) à {config.EMOJI_YES} sur "
                f"[l'ancien sondage]({self.msg_link(s['message_id'])}) avant le "
                f"{tu.ts(tu.friday_deadline(old_dt))}, le report sera annulé.)"
            )
        await self.post_announcement(s["id"], note=note)

    async def cancel_report(self, s: dict) -> None:
        """Annulation de report : retour à la date d'origine (point n° 4)."""
        # Supprime l'annonce de la nouvelle date (ses votes sont perdus)
        try:
            msg = await self.channel().fetch_message(s["message_id"])
            await msg.delete()
        except discord.HTTPException:
            pass

        old_dt = tu.parse(s["old_scheduled_at"])
        announced = tu.parse(s["old_announced_at"]) or tu.now()
        n = tu.now()
        await self.db.move_votes_old_to_cur(s["id"])
        await self.db.clear_handled(s["id"])
        await self.db.update_session(
            s["id"],
            scheduled_at=s["old_scheduled_at"],
            message_id=s["old_message_id"],
            announced_at=s["old_announced_at"],
            locked=0,
            # Évite de re-spammer des rappels dont l'heure est déjà passée
            dm_reminder_sent=1 if n >= announced + timedelta(days=config.DM_REMINDER_DAYS) else 0,
            monday_reminder_sent=1 if n >= tu.monday_reminder(old_dt) else 0,
            old_scheduled_at=None,
            old_message_id=None,
            old_announced_at=None,
            old_absents=None,
        )
        role = self.player_role()
        await self.channel().send(
            f"{role.mention} 🔄 **Report annulé** : plus aucun absent ! "
            f"La séance revient à sa date d'origine, le {tu.ts(old_dt)}. "
            f"Le sondage d'origine ({self.msg_link(s['old_message_id'])}) reste valable.",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        try:
            await (await self.gm()).send(
                f"🔄 Report annulé automatiquement : tous les absents ont revoté "
                f"{config.EMOJI_YES}. Séance rétablie au {tu.ts(old_dt)}."
            )
        except discord.Forbidden:
            pass
        log.info("Report annulé, retour au %s", old_dt)

    # ----------------------------------------------------------- rappels

    async def send_dm_reminders(self, s: dict) -> None:
        dt = tu.parse(s["scheduled_at"])
        link = self.msg_link(s["message_id"])
        lazy = await self.non_voters(s)
        for m in lazy:
            try:
                await m.send(
                    f"👋 Petit rappel : tu n'as pas encore voté pour la séance D&D "
                    f"du {tu.ts(dt)}. Réagis {config.EMOJI_YES} ou {config.EMOJI_NO} "
                    f"ici : {link}"
                )
            except discord.Forbidden:
                # Point n° 10 : DM fermés → mention discrète dans le canal
                await self.channel().send(
                    f"{m.mention} pense à voter pour la séance du {tu.ts(dt, 'D')} ⤴️",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
        log.info("Relances DM envoyées à %s joueur(s)", len(lazy))

    async def send_monday_reminder(self, s: dict) -> None:
        dt = tu.parse(s["scheduled_at"])
        role = self.player_role()
        lazy = await self.non_voters(s)
        votes = await self.db.get_votes(s["id"], "cur")
        yes = sum(1 for v in votes.values() if v == "yes")
        no = sum(1 for v in votes.values() if v == "no")
        txt = (
            f"{role.mention} ⏰ **Séance samedi** {tu.ts(dt)} !\n"
            f"Sondage : {config.EMOJI_YES} {yes} · {config.EMOJI_NO} {no}."
        )
        if lazy:
            txt += (
                f"\nEn attente de vote : {', '.join(m.mention for m in lazy)} "
                f"— clôture vendredi 23 h 59."
            )
        await self.channel().send(
            txt, allowed_mentions=discord.AllowedMentions(roles=True, users=True)
        )

    # ----------------------------------------------------------- boucle

    async def tick(self) -> None:
        """Exécutée chaque minute : fait avancer tous les délais depuis l'état en base.
        Idempotente et recalculée depuis la DB → robuste aux redémarrages."""
        s = await self.db.get_active_session()
        if not s:
            return
        n = tu.now()
        dt = tu.parse(s["scheduled_at"])

        # 1. Fin de la fenêtre d'annulation de report (vendredi avant la date d'origine)
        if s["old_message_id"] and n > tu.friday_deadline(tu.parse(s["old_scheduled_at"])):
            await self.db.update_session(
                s["id"], old_scheduled_at=None, old_message_id=None,
                old_announced_at=None, old_absents=None,
            )
            log.info("Fenêtre d'annulation de report expirée — report définitif")
            s = await self.db.get_active_session()

        # 2. Clôture des votes (vendredi 23 h 59)
        if not s["locked"] and n >= tu.friday_deadline(dt):
            await self.db.update_session(s["id"], locked=1)
            log.info("Votes clos pour la séance du %s", dt)
            s = await self.db.get_active_session()

        # 3. Timeout de consultation (48 h ou samedi 19 h)
        pending = await self.db.get_pending_consultation(s["id"])
        if pending and n >= tu.parse(pending["deadline"]):
            await self.resolve_consultation(pending, approved=False, via_timeout=True)

        # 4. Relance DM individuelle (annonce + 7 jours)
        if (
            not s["dm_reminder_sent"] and not s["locked"] and s["announced_at"]
            and n >= tu.parse(s["announced_at"]) + timedelta(days=config.DM_REMINDER_DAYS)
        ):
            await self.db.update_session(s["id"], dm_reminder_sent=1)
            await self.send_dm_reminders(s)

        # 5. Rappel du lundi 18 h de la semaine de séance
        if not s["monday_reminder_sent"] and n >= tu.monday_reminder(dt) and n < dt:
            await self.db.update_session(s["id"], monday_reminder_sent=1)
            # Ne rien poster si l'annonce est postérieure au lundi (cas /setdate tardif)
            if not s["announced_at"] or tu.parse(s["announced_at"]) <= tu.monday_reminder(dt):
                await self.send_monday_reminder(s)

        # 6. Lendemain de séance (dimanche 10 h) : boucle → séance suivante
        if n >= tu.sunday_announce(dt):
            await self.db.update_session(s["id"], status="played")
            next_dt = dt + timedelta(days=14)
            sid = await self.db.create_session(tu.iso(next_dt))
            await self.post_announcement(sid, note="🗡️ Bien joué hier soir !")
            log.info("Séance jouée, suivante planifiée au %s", next_dt)

    # ---------------------------------------------------- resync au démarrage

    async def resync_votes(self) -> None:
        """Relit les réactions des messages de sondage pour rattraper les votes
        émis pendant un éventuel downtime du bot."""
        s = await self.db.get_active_session()
        if not s:
            return
        for poll, mid in (("cur", s["message_id"]), ("old", s["old_message_id"])):
            if not mid:
                continue
            try:
                msg = await self.channel().fetch_message(mid)
            except discord.HTTPException:
                continue
            seen: dict[int, set[str]] = {}
            for reaction in msg.reactions:
                emoji = str(reaction.emoji)
                if emoji not in (config.EMOJI_YES, config.EMOJI_NO):
                    continue
                value = "yes" if emoji == config.EMOJI_YES else "no"
                async for user in reaction.users():
                    if user.bot:
                        continue
                    member = self.guild().get_member(user.id)
                    if not member or not self.is_player(member):
                        continue
                    seen.setdefault(user.id, set()).add(value)
            db_votes = await self.db.get_votes(s["id"], poll)
            for uid, values in seen.items():
                if len(values) == 1:
                    await self.db.set_vote(s["id"], poll, uid, values.pop())
                elif uid not in db_votes:
                    # Double réaction sans historique : ❌ prioritaire par prudence
                    await self.db.set_vote(s["id"], poll, uid, "no")
            for uid in list(db_votes):
                if uid not in seen:  # réaction retirée pendant le downtime
                    await self.db.delete_vote(s["id"], poll, uid)
            await self.after_vote_change(poll)
        log.info("Resynchronisation des votes terminée")
