# RSSLocal — Notes de session (juillet 2026)

## Le projet en une phrase
Lecteur/agrégateur de flux RSS **100 % local**, en un seul fichier Python (stdlib
uniquement), **sans installation ni droits admin**, avec analyse IA optionnelle.
Dépôt : https://github.com/Stefsg73/rss-local

## Contexte et contraintes d'origine
- Utilisateur : journaliste (site d'info généraliste), veille quotidienne 20-50 flux
- Poste de bureau **Windows 11 verrouillé** : pas de droits admin, donc ni Docker,
  ni FreshRSS → solution : Python embeddable (ZIP portable) + script unique
- Poste perso : **Mac** (développement et tests réalisés dessus)
- Besoin d'exports vers n8n envisagé, mais n8n non installé à ce stade
  → l'export JSON remplit le même office en attendant

## Architecture retenue
- `rss_local.py` : script unique ~800 lignes — collecte (RSS 2.0 + Atom,
  8 fetchs parallèles), base SQLite (`rss_local.db`), serveur HTTP local
  (127.0.0.1:8765), interface web embarquée (variable PAGE), analyse IA
- **Analyse IA « en entonnoir »** pour économiser les tokens :
  - Étape 1 : tri thématique par lots de 20 articles → `claude-haiku-4-5-20251001`
  - Étape 2 : synthèse éditoriale des tris condensés → `claude-sonnet-4-6`
  - Prompts par défaut orientés journalisme (faits marquants, panorama, angles)
- Clé API lue via variable d'environnement `ANTHROPIC_API_KEY` (ou en dur)
- Tables SQLite : `feeds`, `articles`, `syntheses`, `reglages` (v2)

## Versions

### V1 (livrée, remplacée)
Import OPML, rafraîchissement manuel, lecture par date, exports JSON/CSV/OPML,
webhook n8n, analyse IA basique.

### V2 (en production, poussée sur GitHub)
1. **Gestion des flux** dans l'interface : ajout unitaire, suppression avec
   double confirmation (articles archivés **conservés par défaut**, purge optionnelle)
2. **Tri/regroupement** : récents d'abord / anciens d'abord / groupé par média
   (sections dépliantes `<details>` avec compteurs)
3. **Partage d'articles** : ✉️ mailto, 💬 wa.me, 📋 copie du lien
4. **Synthèse repliable/masquable** (clic sur bandeau, ✕, mémorisation par jour)
5. **Prompts d'analyse éditables** dans l'interface (table `reglages`,
   bouton de restauration des défauts)
6. Correctif SSL macOS intégré de série (`make_ssl_context()`)

## Problèmes rencontrés et résolus
- **SSL macOS** `CERTIFICATE_VERIFY_FAILED` → `Install Certificates.command`
  + patch multi-chemins intégré au code (contexte `SSL_CTX` passé aux 3 urlopen)
- **Lancement** : erreurs de dossier courant et de copier-coller du prompt `%`
  → lancement recommandé : `cd <dossier>` puis `python3 rss_local.py`
- **Clé API** : nécessite un redémarrage du script après modification ;
  ⚠️ une clé a été exposée en cours de session → **révoquée et remplacée**
- **HTTP 400 « credit balance too low »** : compte API sans crédits
  → analyse IA en attente d'achat de crédits (Plans & Billing) ; tout le reste
  fonctionne sans. Budget estimé : 5-10 $/mois pour un usage quotidien
- **Git** : auth par token PAT (mot de passe refusé par GitHub) ;
  divergence de branches résolue par `git pull --rebase origin main`

## Commits GitHub (branche main)
1. Version initiale (V1) : lecteur RSS local + analyse IA
2. `v2:` gestion flux, tri/regroupement, partage, prompts éditables,
   correctif SSL, clé API par variable d'environnement
3. `docs:` mise à jour README v2 + instructions Windows 11
- Tag suggéré (à vérifier s'il a été poussé) : `v2.0`

## Fichiers du dépôt
`rss_local.py` · `lancer.bat` (Windows, détecte python/ portable) ·
`lancer.sh` (Mac/Linux) · `exemple_flux.opml` · `.gitignore`
(exclut rss_local.db, synthese_*.txt, python/) · `LICENSE` (MIT) · `README.md`

## Pistes pour la suite

### Écartées volontairement de la V2 (à réévaluer)
- Rafraîchissement automatique planifié (`INTERVALLE_MINUTES`)
- Filtrage/alertes par mots-clés

### Candidates V3
- Marquage lu/non-lu, recherche plein texte
- Sélection de flux spécifiques pour l'export (au lieu de tout/rien)
- Analyse IA ciblée par catégorie de flux
- Message pédagogique dans l'interface sur l'activation des crédits API

### Chantier n8n (moyen terme)
- n8n portable via Node.js embeddable (même logique sans-admin)
- Webhook déjà codé côté RSSLocal (variable `WEBHOOK_N8N`)

### À faire côté utilisateur
- Tester la V2 en usage quotidien sur le Windows 11 du bureau
  (point de vigilance : proxy d'entreprise → HTTP_PROXY/HTTPS_PROXY dans lancer.bat)
- Acheter des crédits API pour activer l'analyse (console.anthropic.com)
- Ajuster les prompts d'analyse à la ligne éditoriale