# Bot D&D — gestion des séances bimensuelles

Bot Discord gérant le cycle de séances D&D (un samedi sur deux, 20 h–minuit, fuseau Europe/Paris) : annonces, sondage de présence par réactions ✅/❌, relances, et reports avec validation du MJ par DM.

## Structure du projet

```
dnd-session-bot/
├── bot/
│   ├── main.py        # point d'entrée, câblage, boucle de planification (60 s)
│   ├── config.py      # variables d'environnement, constantes horaires
│   ├── timeutil.py    # helpers de dates Europe/Paris (deadlines, rappels)
│   ├── db.py          # persistance SQLite (aiosqlite)
│   ├── core.py        # machine à états : annonces, consultations, reports
│   ├── events.py      # réactions = votes
│   ├── commands.py    # commandes slash /init /status /setdate /cancel /postpone
│   └── views.py       # boutons persistants du DM de consultation
├── data/              # base SQLite (volume Docker)
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

## Choix techniques

- **discord.py** : slash commands, Views persistantes (les boutons du DM survivent aux redémarrages), réactions brutes (`on_raw_reaction_*`, fiables même sur des messages non mis en cache), boucle `tasks.loop`.
- **SQLite (aiosqlite)** plutôt que JSON : écritures atomiques (pas de fichier corrompu si le conteneur est tué en pleine écriture), état relationnel (session, votes, consultations avec deadline).
- **Robustesse aux redémarrages** : la boucle de planification ne stocke aucun timer en mémoire — chaque minute, elle recalcule tous les délais depuis la base. Au démarrage, le bot **relit les réactions** des messages de sondage pour rattraper les votes émis pendant un downtime.
- **Canal et rôle par ID** (pas par nom) : immunisé contre l'accent de `#général` et les renommages.

## 1. Création du bot (Discord Developer Portal)

1. https://discord.com/developers/applications → **New Application** → nomme-la.
2. Onglet **Bot** :
   - **Reset Token** → copie le token dans `.env` (`DISCORD_TOKEN`). Ne le partage jamais.
   - **Privileged Gateway Intents** : active **SERVER MEMBERS INTENT** (obligatoire : liste des membres @Player, relances DM). *Message Content* et *Presence* ne sont **pas** nécessaires.
3. Onglet **OAuth2 → URL Generator** :
   - Scopes : `bot` + `applications.commands`.
   - Permissions : *View Channels*, *Send Messages*, *Embed Links*, *Add Reactions*, *Manage Messages* (pour retirer les réactions invalides), *Mention Everyone* (pour mentionner @Player même si le rôle n'est pas « mentionnable par tous »).
   - Ouvre l'URL générée et invite le bot sur ton serveur.
4. Active le **mode développeur** dans Discord (Paramètres → Avancés) puis, par clic droit, **copie les identifiants** du serveur, du canal `#général`, de ton compte et du rôle `@Player` dans `.env`.
5. Réglage utile : Paramètres de confidentialité du serveur → autoriser les MP des membres du serveur (sinon le bot ne pourra pas t'envoyer la consultation — il a un fallback dans `#général`, mais c'est moins discret).

## 2. Déploiement sur Proxmox

**Recommandation : conteneur LXC** plutôt qu'une VM — un bot Discord consomme quelques dizaines de Mo de RAM ; un LXC Debian 12 avec 512 Mo / 1 vCPU / 4 Go de disque est très largement suffisant, démarre en secondes et se sauvegarde trivialement avec `vzdump`. Une VM n'apporterait rien ici sauf si tu veux une isolation forte du kernel.

Docker *dans* un LXC nécessite deux options sur le conteneur (Proxmox → LXC → Options → Features) : **nesting=1** et **keyctl=1** (LXC non privilégié recommandé).

```bash
# Dans le LXC Debian 12 :
apt update && apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh

# Déployer le bot :
git clone <ton-repo> dnd-session-bot   # ou scp du dossier
cd dnd-session-bot
cp .env.example .env && nano .env      # token + IDs
docker compose up -d --build

# Suivre les logs :
docker compose logs -f
```

La base vit dans `./data/bot.db` (monté dans le conteneur). `restart: unless-stopped` relance le bot au boot du LXC. Sauvegarde = snapshot vzdump du LXC, ou simplement copier `data/`.

Mise à jour : `git pull && docker compose up -d --build`.

## 3. Utilisation

| Commande | Effet |
|---|---|
| `/init JJ/MM/AAAA` | Initialise le cycle, poste immédiatement la première annonce + sondage. |
| `/status` | État complet (date, votes, non-votants, consultation en attente, report actif). Réponse éphémère. |
| `/setdate JJ/MM/AAAA` | Change la date de la séance courante ; réinitialise sondage, report et consultation. |
| `/cancel` | Annule la séance courante et annonce immédiatement la suivante (+2 semaines). |
| `/postpone` | Impose un report de +2 semaines sans sondage (pas d'annulation automatique possible). |

Toutes les commandes sont réservées à ton compte (`GM_USER_ID`).

## 4. Cycle et règles implémentées

- **Dimanche 10 h** après chaque séance : annonce de la suivante (+2 sem.) avec sondage ✅/❌ et mention @Player.
- **Annonce + 7 jours** : relance DM des joueurs n'ayant pas voté (fallback : mention dans le canal si DM fermés).
- **Lundi 18 h** de la semaine de séance : rappel dans le canal avec l'état du sondage et la liste des retardataires.
- **Vendredi 23 h 59** : clôture des votes — toute réaction ultérieure est retirée sans effet.
- **Premier ❌** : consultation du MJ par DM (boutons Valider/Refuser). Timeout : min(48 h, samedi 19 h) → refus automatique. Un ❌ supplémentaire pendant la consultation met à jour le DM ; si plus aucun ❌, la consultation s'annule.
- **Refus/timeout** : séance maintenue ; ces absents ne redéclenchent plus rien, seul un ❌ d'un *nouveau* joueur rouvre une consultation.
- **Report validé** : séance +2 sem., nouveau sondage à zéro, rythme bimensuel réancré sur la nouvelle date. L'**ancien sondage reste actif** : si tous les absents d'origine repassent à ✅ (et plus aucun ❌) avant le vendredi précédant la date d'origine, le report est annulé — le message de la nouvelle annonce est supprimé (ses votes sont perdus) et la séance revient à sa date d'origine.
- **Reports en chaîne** : possibles indéfiniment ; la veille d'annulation ne porte que sur la date immédiatement précédente.
- **Double vote ✅+❌** : dernier vote gagne, le bot retire l'autre réaction. Réactions des non-@Player retirées. Clic du MJ après le timeout : « trop tard », le refus automatique tient.

## 5. Test rapide

Pour vérifier l'installation sans attendre deux semaines : `/init` avec le samedi qui vient, vote ❌ avec un compte joueur → tu dois recevoir le DM de consultation dans la foulée. `/status` montre chaque étape.
