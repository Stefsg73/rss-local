# 📰 RSSLocal

Lecteur et agrégateur de flux RSS **100 % local**, en un seul fichier Python,
**sans installation, sans droits administrateur, sans aucune dépendance externe**.

Conçu pour les environnements verrouillés (poste professionnel sans droits admin,
impossibilité d'installer Docker, FreshRSS ou toute application classique).

## ✨ Fonctionnalités

- Agrégation de flux **RSS 2.0 et Atom** (collecte parallèle, dédoublonnage automatique)
- **Import / export OPML** (compatible Feedly, Inoreader, FreshRSS…)
- Interface web locale dans votre navigateur (`http://localhost:8765`)
- Navigation par **date**, résumés d'articles, liens directs vers les sources
- **Export JSON et CSV** des articles d'une journée (CSV compatible Excel FR, séparateur `;`)
- **Envoi vers un webhook n8n** (optionnel) pour automatisation
- **Analyse IA en entonnoir** (optionnel) : tri thématique par Claude Haiku,
  synthèse éditoriale par Claude Sonnet — architecture économe en tokens
- Stockage dans une base **SQLite** locale (un simple fichier `rss_local.db`)

## 🔒 Confidentialité

- La collecte, le stockage et la lecture sont **entièrement locaux**.
  Le serveur web n'écoute que sur `127.0.0.1` : rien n'est accessible depuis le réseau.
- Seule exception, **si vous l'activez** : la fonction d'analyse IA envoie les titres
  et résumés d'articles à l'API Anthropic. Sans clé API, aucune donnée ne sort.

## 🚀 Installation (5 minutes, sans droits admin)

### Prérequis
Aucun, si ce n'est un navigateur web et la possibilité de télécharger des fichiers.

### Étape 1 — Python portable (si Python n'est pas déjà installé)

1. Rendez-vous sur https://www.python.org/downloads/windows/
2. Téléchargez le **« Windows embeddable package (64-bit) »**
   (fichier `python-3.12.x-embed-amd64.zip`) — c'est une simple archive,
   **pas un installateur** : aucun droit admin requis.
3. Décompressez-la dans un sous-dossier `python/` de ce projet :

```
rss-local/
├── python/
│   ├── python.exe
│   └── ...
├── rss_local.py
└── lancer.bat
```

> **Linux / macOS** : Python est généralement préinstallé. Vérifiez avec
> `python3 --version` dans un terminal, puis utilisez `lancer.sh`.

### Étape 2 — Lancement

- **Windows** : double-cliquez sur `lancer.bat`
- **Linux/macOS** : `chmod +x lancer.sh && ./lancer.sh`

Votre navigateur s'ouvre automatiquement sur http://localhost:8765.

> Si Python est installé de façon classique sur votre machine (commande `python`
> disponible), vous pouvez aussi lancer directement : `python rss_local.py`

### Étape 3 — Premiers pas

1. Cliquez sur **📁 Importer OPML** et sélectionnez votre fichier de flux
   (export depuis Feedly, Inoreader, etc. — ou `exemple_flux.opml` pour tester)
2. Cliquez sur **🔄 Rafraîchir les flux**
3. Les articles du jour s'affichent. Utilisez le sélecteur de date pour l'historique.

## ⚙️ Configuration

Toutes les options sont regroupées **en tête du fichier `rss_local.py`** :

| Variable | Rôle | Défaut |
|---|---|---|
| `PORT` | Port du serveur local | `8765` |
| `DB_PATH` | Fichier de base de données | `rss_local.db` |
| `WEBHOOK_N8N` | URL du webhook n8n (vide = désactivé) | `""` |
| `API_KEY` | Clé API Anthropic (vide = analyse désactivée) | `""` |
| `MODELE_TRI` | Modèle pour le tri par lots | Claude Haiku |
| `MODELE_SYNTHESE` | Modèle pour la synthèse finale | Claude Sonnet |
| `TAILLE_LOT` | Articles par lot envoyé au tri | `20` |

## 🧠 Analyse IA (optionnel)

1. Créez une clé sur https://console.anthropic.com (menu *API Keys*)
2. Collez-la dans la variable `API_KEY` de `rss_local.py`
3. Cliquez sur **🧠 Analyser la journée**

L'architecture « en entonnoir » minimise les coûts : les articles partent par
lots vers **Haiku** (modèle économique) pour un tri thématique ; seuls les tris
condensés remontent vers **Sonnet** pour la synthèse éditoriale (faits marquants,
panorama par thème, angles d'articles). La synthèse s'affiche dans l'interface,
est conservée en base et sauvegardée dans un fichier `synthese_AAAA-MM-JJ.txt`.

⚠️ L'analyse IA est **payante** (facturation à l'usage par Anthropic) et nécessite
une connexion internet. Consultez les tarifs en vigueur sur le site d'Anthropic.
Ordre de grandeur : quelques centimes par analyse quotidienne de ~1 000 articles.

## 📤 Exports

| Bouton | Format | Contenu |
|---|---|---|
| ⬇ JSON | `.json` | Articles du jour sélectionné (structure complète) |
| ⬇ CSV | `.csv` | Idem, séparateur `;`, encodage UTF-8 BOM (Excel FR) |
| ⬇ OPML | `.opml` | La liste de tous vos flux, réimportable partout |
| 📤 n8n | webhook | POST JSON des articles du jour vers `WEBHOOK_N8N` |

## 🗃️ Données

Tout est stocké dans `rss_local.db` (SQLite) à côté du script :
- `feeds` : vos flux (URL, titre, catégorie)
- `articles` : les articles collectés (dédupliqués par identifiant unique)
- `syntheses` : les analyses IA générées

**Sauvegarde** : copiez simplement ce fichier. **Réinitialisation** : supprimez-le.

## 🛠️ Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| La page ne s'ouvre pas | Port 8765 occupé | Changez `PORT` dans le script |
| `⚠ Erreur réseau` sur un flux | Site bloquant les robots, proxy d'entreprise | Vérifiez l'URL dans un navigateur ; certains sites exigent des en-têtes spécifiques |
| `⚠ XML invalide` | Le flux n'est pas du RSS/Atom valide | Testez l'URL sur validator.w3.org/feed |
| Import OPML : 0 flux ajouté | Flux déjà présents ou OPML sans attribut `xmlUrl` | Ouvrez l'OPML dans un éditeur pour vérifier |
| Analyse : « Aucune clé API » | `API_KEY` vide | Renseignez la clé en tête de script |
| Le poste passe par un proxy d'entreprise | Requêtes sortantes bloquées | Définissez les variables d'environnement `HTTP_PROXY`/`HTTPS_PROXY` avant lancement |

## 📁 Structure du projet

```
rss_local.py       Script unique : collecte, base, serveur web, interface, analyse IA
lancer.bat         Lanceur Windows (utilise python/ portable ou le python système)
lancer.sh          Lanceur Linux/macOS
exemple_flux.opml  Fichier OPML de démonstration
rss_local.db       (créé au premier lancement) base SQLite — non versionné
synthese_*.txt     (créés par l'analyse IA) — non versionnés
```

## 📜 Licence

MIT — voir le fichier `LICENSE`.