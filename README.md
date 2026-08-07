# RSSLocal

Lecteur/agrégateur de flux RSS **100 % local**, en un seul fichier Python
(stdlib uniquement), **sans installation ni droits admin**, avec analyse
éditoriale IA optionnelle.

## En une phrase

Un script unique (`rss_local.py`) qui collecte vos flux RSS/Atom, les
organise en dossiers, les stocke dans une base SQLite locale, et sert une
interface web en 3 colonnes sur `http://localhost:8765` — sans Docker, sans
serveur externe, sans droits administrateur.

## Fonctionnalités (v5)

### Interface
- **3 colonnes** : flux organisés en dossiers (gauche) · liste condensée des
  articles (centre) · panneau de lecture (droite)
- **Dossiers pliables/dépliables**, avec glisser-déposer pour organiser les
  flux (entre dossiers, ou pour réordonner ses flux préférés en haut de liste)
- **Panneau de lecture** avec tentative d'extraction du **texte intégral**
  de l'article (heuristique interne, sans dépendance externe), repli
  automatique sur le résumé RSS si l'extraction échoue (paywall, site en
  JavaScript…), mise en cache et bouton de rechargement manuel
- **Sélection multiple d'articles** (cases à cocher) avec barre d'export
  flottante : export groupé en `.md`, indépendamment de la période ou du flux
- **Raccourcis clavier** : `J`/`K` pour naviguer, `E` pour sélectionner
  l'article ouvert, `O` pour l'ouvrir dans un nouvel onglet
- **Favicons des médias** dans la liste des flux
- Tri (récents/anciens/par média), marquage lu/non-lu, recherche plein
  texte, mots-clés à surveiller (surlignage dans la liste)

### Flux et collecte
- RSS 2.0 et Atom, 8 flux rafraîchis en parallèle
- Gestion des flux : ajout unitaire, édition en place (titre, dossier),
  suppression unitaire ou par lots, purge des articles au choix
- **Générateur de flux Google News** : crée un flux RSS à partir d'un
  mot-clé et d'une langue/région, pour une veille ponctuelle sans avoir à
  chercher le flux natif d'un média
- **Import/export OPML** avec aperçu avant action : ajout simple des
  nouveaux flux, ou synchronisation complète avec écran de confirmation
  listant les flux absents du fichier avant toute suppression ; les
  dossiers sont reconstruits automatiquement à l'import et conservés à
  l'export
- Badge d'alerte ⚠️ sur les flux en échec de rafraîchissement récurrent

### Analyse et partage
- Analyse éditoriale IA (Claude Sonnet, prompt éditable) : synthèse par
  période, repliable, exportable en `.md`, envoyable par mail
- Partage d'articles : mail, WhatsApp, copie du lien
- Exports JSON, CSV, OPML, filtrables par période et par flux
- Purges automatique (par ancienneté, réglable) ou manuelle (totale, par
  flux, par période)
- Webhook n8n optionnel (variable `WEBHOOK_N8N`)

## Prérequis

- Python 3.9+ (aucune dépendance externe : uniquement la bibliothèque
  standard)
- Aucun droit administrateur nécessaire

## Installation

### macOS / Linux

```bash
cd rss-local
./lancer.sh
```

### Windows 11 (poste verrouillé, sans droits admin)

`lancer.bat` détecte automatiquement un Python installé ou un Python
portable (dossier `python/` à côté du script) :

```bat
lancer.bat
```

Si votre poste est derrière un **proxy d'entreprise**, définissez au besoin
`HTTP_PROXY` / `HTTPS_PROXY` dans `lancer.bat` avant de lancer le script.

### Dans les deux cas

```bash
python3 rss_local.py
```

L'interface s'ouvre automatiquement dans votre navigateur à l'adresse
`http://localhost:8765`.

## Configuration de la clé API (analyse IA)

L'analyse éditoriale est **optionnelle** : sans clé API, tout le reste du
lecteur (collecte, lecture, texte intégral, exports) fonctionne normalement.

### Option 1 — Variable d'environnement (recommandée)

**Windows 11** (sans droits admin) :
1. `Windows` → « variables d'environnement » → **Modifier les variables
   d'environnement pour ce compte**
2. Variables utilisateur → **Nouveau** → nom `ANTHROPIC_API_KEY`, valeur
   `sk-ant-...`
3. Redémarrer le terminal avant de relancer le script

Ou en une commande PowerShell :
```powershell
setx ANTHROPIC_API_KEY "sk-ant-votre-cle"
```

**macOS/Linux** :
```bash
export ANTHROPIC_API_KEY="sk-ant-votre-cle"
```

### Option 2 — En dur dans le script (déconseillé si le dépôt est public)

Ligne dédiée dans `rss_local.py` :
```python
API_KEY = "sk-ant-votre-cle"
```
⚠️ Ne jamais committer une clé en dur. Vérifiez que `rss_local.py` reste
hors du dépôt si vous utilisez cette option, ou que le dépôt est privé.

### Crédits API

Un compte sans crédits renvoie une erreur HTTP 400 (« credit balance too
low »). Achat de crédits sur
[console.anthropic.com](https://console.anthropic.com) → Plans & Billing.
Budget indicatif pour un usage quotidien : 5-10 $/mois.

## Limites connues

- **Texte intégral** : l'extraction est une heuristique maison (recherche
  de la balise `<article>` ou du bloc le plus dense en paragraphes), sans
  bibliothèque tierce. Elle échoue sur les contenus sous paywall et sur les
  sites qui nécessitent JavaScript pour afficher le texte — dans ce cas,
  repli automatique et transparent sur le résumé RSS.
- **Favicons** : chargées depuis un service public (`google.com/s2/favicons`) ;
  peuvent ne pas s'afficher derrière certains proxys d'entreprise stricts,
  sans impact sur le reste de l'application.
- **Interface** : pensée pour un usage bureau (3 colonnes), non optimisée
  pour un petit écran mobile.

## Fichiers du dépôt

| Fichier | Rôle |
|---|---|
| `rss_local.py` | Script principal (collecte, base, serveur, interface) |
| `lancer.bat` | Lancement Windows (détection Python système/portable) |
| `lancer.sh` | Lancement macOS/Linux |
| `exemple_flux.opml` | Exemple de fichier d'import |
| `.gitignore` | Exclut `rss_local.db`, `synthese_*.txt`, `python/` |
| `LICENSE` | MIT |

## ⚠️ Migration depuis une version antérieure à la v5

La v5 restructure les tables `feeds` et `articles` (dossiers, cache de
texte intégral). **Supprimez (ou renommez) votre `rss_local.db` existant**
avant le premier lancement de cette version : il sera recréé
automatiquement avec le nouveau schéma. Vos flux devront être réimportés
(OPML — les dossiers sont reconstruits automatiquement) ou réajoutés.

## Historique des versions

### v5
Refonte de l'interface en 3 colonnes (flux/articles/lecture), dossiers de
flux pliables avec glisser-déposer (remplacent l'ancien champ
« catégorie »), panneau de lecture avec extraction de texte intégral et
repli automatique, sélection multiple d'articles avec export `.md` groupé,
raccourcis clavier, favicons des médias, générateur de flux Google News.

### v4
Suppression de flux par lots, édition en place d'un flux, synchronisation
OPML avec aperçu et confirmation, marquage lu/non-lu, recherche plein
texte, filtre d'affichage/export par flux, mots-clés à surveiller
(surlignage), badge d'alerte sur les flux en échec récurrent.

### v3
Sélection de période (jour/7j/30j/personnalisée), analyse IA simplifiée
(modèle et prompt uniques), destinataire mail par défaut, envoi de la
synthèse par mail, export `.md`, purge automatique par ancienneté, purges
manuelles (totale/flux/période), correctif SSL macOS intégré.

### v2
Gestion des flux dans l'interface, tri/regroupement par média, partage
d'articles (mail/WhatsApp/copie), synthèse repliable/masquable, prompts
d'analyse éditables, clé API par variable d'environnement.

### v1
Import OPML, rafraîchissement manuel, lecture par date, exports
JSON/CSV/OPML, webhook n8n, analyse IA basique.

## Licence

MIT — voir `LICENSE`.
