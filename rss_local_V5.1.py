# -*- coding: utf-8 -*-
"""
RSSLocal v5.1 — Lecteur/agrégateur RSS 100 % local avec analyse IA.
Aucun droit admin, aucune dépendance externe (stdlib Python uniquement).
Usage : python3 rss_local.py   puis ouvrir http://localhost:8765

Nouveautés v5.1 (ajustements suite retours d'usage sur la v5) :
- Colonne centrale redevenue la vue principale (cartes complètes avec
  résumé et partage, comme en v4) et la plus large par défaut ; le
  panneau de lecture à droite reste dédié au texte intégral. Un
  séparateur entre les deux colonnes permet d'ajuster leur largeur.
- Synthèse IA : modèle par défaut allégé (Haiku, plus sobre, largement
  suffisant pour ce type de synthèse), déplacée dans la colonne centrale,
  et strictement pliable/dépliable (elle ne peut plus être fermée
  définitivement — juste repliée, toujours accessible)
- Purge par période désormais scoping-aware : si un flux ou un dossier est
  sélectionné dans la colonne de gauche, la purge ne porte que sur cette
  sélection (au lieu de toute la période, tous flux confondus)

Nouveautés v5 (refonte de l'interface, phases 1 et 2) :
- Interface en 3 colonnes : flux organisés en dossiers (gauche) / liste des
  articles (centre) / panneau de lecture (droite)
- Dossiers de flux pliables/dépliables, avec glisser-déposer pour organiser
  les flux (entre dossiers, ou pour réordonner) — remplace l'ancien champ
  « catégorie » texte
- Panneau de lecture avec tentative d'extraction du texte intégral de
  l'article (heuristique, sans dépendance externe) ; repli automatique sur
  le résumé RSS si l'extraction échoue (paywall, site en JavaScript, etc.),
  avec mise en cache et bouton de rechargement manuel
- Sélection multiple d'articles avec export groupé en .md, raccourcis
  clavier (J/K naviguer, E sélectionner, O ouvrir), favicons des médias,
  générateur de flux Google News (mot-clé + langue/région)

⚠️ IMPORTANT — changement de schéma de base de données (depuis la v5) :
Cette version restructure les tables `feeds` et `articles` (dossiers,
cache de texte intégral). Supprimez (ou renommez) votre ancien
rss_local.db si vous venez d'une version antérieure à la v5 : il sera
recréé automatiquement avec le nouveau schéma. Vos flux devront être
réimportés (OPML) ou réajoutés. Aucun changement de schéma entre la v5 et
la v5.1 : pas besoin de reset si vous êtes déjà en v5.

Rappel des nouveautés v4 : suppression de flux par lots, import OPML avec
aperçu et choix ajout/synchronisation, marquage lu/non-lu, recherche plein
texte, filtre d'affichage/export par flux, mots-clés à surveiller, édition
en place d'un flux, badge d'alerte sur échec de rafraîchissement récurrent.
"""
import sqlite3, json, csv, io, re, html, threading, webbrowser, ssl, os
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

# ---------- CONFIGURATION ----------
PORT = 8765
DB_PATH = "rss_local.db"
WEBHOOK_N8N = ""     # ex: "http://localhost:5678/webhook/rss" — vide si inutilisé
USER_AGENT = "Mozilla/5.0 (RSSLocal/5.0; lecteur personnel)"
TIMEOUT = 15
CONSERVER_JOURS = 14  # purge auto : articles et synthèses plus vieux supprimés
SEUIL_ECHECS_ALERTE = 3  # nombre d'échecs consécutifs avant badge ⚠️ sur un flux
LONGUEUR_MIN_TEXTE_INTEGRAL = 250  # en-dessous, on considère l'extraction ratée
LONGUEUR_MAX_TEXTE_INTEGRAL = 15000  # troncature de sécurité

# --- Analyse IA (laisser API_KEY vide pour désactiver) ---
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # ou collez la clé ici : "sk-ant-..."
MODELE_ANALYSE = "claude-haiku-4-5-20251001"  # modèle sobre et rapide, largement suffisant pour ce type de synthèse
# -----------------------------------

# ---------- CONTEXTE SSL (compatibilité macOS / OpenSSL strict) ----------
def make_ssl_context():
    """Contexte SSL : certificats système si trouvés, et retrait du contrôle
    VERIFY_X509_STRICT (OpenSSL 3.2+ rejette à tort certains certificats
    légitimes comme ISRG Root X1). La vérification SSL reste active."""
    ctx = None
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if ctx is None:
        for path in ("/etc/ssl/cert.pem",
                     "/etc/ssl/certs/ca-certificates.crt",
                     "/private/etc/ssl/cert.pem"):
            if os.path.exists(path):
                ctx = ssl.create_default_context(cafile=path)
                break
    if ctx is None:
        ctx = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx

SSL_CTX = make_ssl_context()
# --------------------------------------------------------------------------

# ---------- PROMPT PAR DÉFAUT (unique) ----------
PROMPT_ANALYSE_DEFAUT = (
    "Tu es l'assistant de veille d'un journaliste généraliste. "
    "Voici la liste des articles collectés sur la période indiquée "
    "(titre, média, court résumé). Rédige une synthèse éditoriale structurée : "
    "1) les 5 faits marquants de la période, 2) un panorama par thème "
    "(politique, économie, santé, culture, sport, local, autre), "
    "3) trois angles d'articles possibles. Style factuel et neutre, concis.")
# ------------------------------------------------

# ==================== BASE DE DONNÉES ====================

def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS dossiers(
        id INTEGER PRIMARY KEY, nom TEXT UNIQUE, ordre INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS feeds(
        id INTEGER PRIMARY KEY, url TEXT UNIQUE, title TEXT,
        dossier_id INTEGER, ordre INTEGER DEFAULT 0, echecs INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY, feed_id INTEGER, guid TEXT UNIQUE,
        title TEXT, link TEXT, summary TEXT, published TEXT, fetched TEXT,
        lu INTEGER DEFAULT 0, texte_integral TEXT, texte_recupere_le TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS syntheses(
        id INTEGER PRIMARY KEY, jour TEXT UNIQUE, texte TEXT, cree TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reglages(
        cle TEXT PRIMARY KEY, valeur TEXT)""")
    return c

def get_reglage(cle, defaut=""):
    c = db()
    row = c.execute("SELECT valeur FROM reglages WHERE cle=?", (cle,)).fetchone()
    c.close()
    return row[0] if row and row[0].strip() else defaut

def set_reglage(cle, valeur):
    c = db()
    c.execute("INSERT OR REPLACE INTO reglages(cle, valeur) VALUES(?,?)", (cle, valeur))
    c.commit(); c.close()

# ==================== PURGES ====================

def purge_auto():
    """Purge silencieuse à chaque rafraîchissement : articles et synthèses
    plus vieux que CONSERVER_JOURS."""
    cutoff = (datetime.now() - timedelta(days=CONSERVER_JOURS)).strftime("%Y-%m-%d")
    c = db()
    c.execute("DELETE FROM articles WHERE substr(published,1,10) < ?", (cutoff,))
    n_art = c.execute("SELECT changes()").fetchone()[0]
    c.execute("DELETE FROM syntheses WHERE substr(cree,1,10) < ?", (cutoff,))
    c.commit(); c.close()
    return n_art

def purge_totale():
    c = db()
    c.execute("DELETE FROM articles")
    n = c.execute("SELECT changes()").fetchone()[0]
    c.execute("DELETE FROM syntheses")
    c.commit(); c.close()
    return True, f"Base vidée : {n} articles et toutes les synthèses supprimés. Les flux sont conservés."

def purge_flux(feed_id):
    c = db()
    row = c.execute("SELECT title FROM feeds WHERE id=?", (feed_id,)).fetchone()
    if not row:
        c.close(); return False, "Flux introuvable."
    c.execute("DELETE FROM articles WHERE feed_id=?", (feed_id,))
    n = c.execute("SELECT changes()").fetchone()[0]
    c.commit(); c.close()
    return True, f"{n} articles de « {row[0]} » supprimés (flux conservé)."

def purge_periode(start, end, feed_ids=None):
    c = db()
    q = """DELETE FROM articles WHERE substr(published,1,10) >= ?
           AND substr(published,1,10) <= ?"""
    params = [start, end]
    if feed_ids:
        q += f" AND feed_id IN ({','.join('?' * len(feed_ids))})"
        params += list(feed_ids)
    c.execute(q, params)
    n = c.execute("SELECT changes()").fetchone()[0]
    c.commit(); c.close()
    portee = " (flux/dossier sélectionné uniquement)" if feed_ids else ""
    return True, f"{n} articles supprimés sur la période {start} → {end}{portee}."

# ==================== COLLECTE DES FLUX ====================

def parse_date(s):
    if not s: return None
    s = s.strip()
    try: return parsedate_to_datetime(s)
    except Exception: pass
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

def strip_html(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()[:600]

def fetch_feed(feed_id, url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            data = r.read()
    except Exception as e:
        return [], f"Erreur réseau: {e}"
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return [], f"XML invalide: {e}"
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []
    for it in root.iter("item"):  # RSS 2.0
        title = (it.findtext("title") or "(sans titre)").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        desc = strip_html(it.findtext("description"))
        pub = parse_date(it.findtext("pubDate"))
        items.append((guid, title, link, desc, pub))
    if not items:  # Atom
        for it in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = (it.findtext("atom:title", namespaces=ns) or "(sans titre)").strip()
            link_el = it.find("atom:link[@rel='alternate']", ns) or it.find("atom:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            guid = (it.findtext("atom:id", namespaces=ns) or link or title).strip()
            desc = strip_html(it.findtext("atom:summary", namespaces=ns)
                              or it.findtext("atom:content", namespaces=ns))
            pub = parse_date(it.findtext("atom:published", namespaces=ns)
                             or it.findtext("atom:updated", namespaces=ns))
            items.append((guid, title, link, desc, pub))
    return items, None

def refresh_all():
    purge_auto()  # nettoyage silencieux des articles/synthèses trop vieux
    c = db()
    feeds = c.execute("SELECT id, url, title FROM feeds").fetchall()
    def work(f):
        fid, url, ftitle = f
        items, err = fetch_feed(fid, url)
        return fid, ftitle or url, items, err
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, feeds))
    now = datetime.now(timezone.utc).isoformat()
    new_count, report, erreurs = 0, [], 0
    for fid, ftitle, items, err in results:
        if err:
            erreurs += 1
            c.execute("UPDATE feeds SET echecs = echecs + 1 WHERE id=?", (fid,))
            report.append(f"⚠ {ftitle} : {err}"); continue
        c.execute("UPDATE feeds SET echecs = 0 WHERE id=?", (fid,))
        for guid, title, link, desc, pub in items:
            try:
                c.execute("""INSERT OR IGNORE INTO articles
                    (feed_id, guid, title, link, summary, published, fetched)
                    VALUES (?,?,?,?,?,?,?)""",
                    (fid, guid, title, link, desc,
                     pub.isoformat() if pub else now, now))
                new_count += c.execute("SELECT changes()").fetchone()[0]
            except Exception: pass
        report.append(f"✓ {ftitle} : {len(items)} articles")
    c.commit(); c.close()
    return new_count, erreurs, report

# ==================== DOSSIERS ====================

def list_dossiers():
    c = db()
    rows = c.execute("SELECT id, nom, ordre FROM dossiers ORDER BY ordre, nom").fetchall()
    c.close()
    return [{"id": r[0], "nom": r[1], "ordre": r[2]} for r in rows]

def get_or_create_dossier(nom, c=None):
    """Renvoie l'id du dossier portant ce nom, en le créant si besoin.
    Accepte une connexion existante pour éviter tout verrou SQLite lors
    d'un import OPML (plusieurs écritures dans la même transaction)."""
    nom = (nom or "").strip()
    if not nom:
        return None
    ferme = c is None
    if c is None:
        c = db()
    row = c.execute("SELECT id FROM dossiers WHERE nom=?", (nom,)).fetchone()
    if row:
        did = row[0]
    else:
        maxo = c.execute("SELECT COALESCE(MAX(ordre),-1) FROM dossiers").fetchone()[0]
        c.execute("INSERT INTO dossiers(nom, ordre) VALUES(?,?)", (nom, maxo + 1))
        did = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    if ferme:
        c.commit(); c.close()
    return did

def ajouter_dossier(nom):
    did = get_or_create_dossier(nom)
    return (True, "Dossier prêt.", did) if did else (False, "Nom de dossier requis.", None)

def renommer_dossier(dossier_id, nom):
    nom = (nom or "").strip()
    if not nom:
        return False, "Nom de dossier requis."
    c = db()
    row = c.execute("SELECT id FROM dossiers WHERE id=?", (dossier_id,)).fetchone()
    if not row:
        c.close(); return False, "Dossier introuvable."
    try:
        c.execute("UPDATE dossiers SET nom=? WHERE id=?", (nom, dossier_id))
        c.commit()
    except sqlite3.IntegrityError:
        c.close(); return False, "Un dossier porte déjà ce nom."
    c.close()
    return True, "Dossier renommé."

def supprimer_dossier(dossier_id):
    c = db()
    row = c.execute("SELECT nom FROM dossiers WHERE id=?", (dossier_id,)).fetchone()
    if not row:
        c.close(); return False, "Dossier introuvable."
    c.execute("UPDATE feeds SET dossier_id=NULL WHERE dossier_id=?", (dossier_id,))
    c.execute("DELETE FROM dossiers WHERE id=?", (dossier_id,))
    c.commit(); c.close()
    return True, f"Dossier « {row[0]} » supprimé (ses flux sont déplacés hors dossier, pas supprimés)."

def reordonner_dossiers(ids):
    c = db()
    for i, did in enumerate(ids or []):
        c.execute("UPDATE dossiers SET ordre=? WHERE id=?", (i, int(did)))
    c.commit(); c.close()
    return True

# ==================== GESTION DES FLUX ====================

def list_feeds():
    c = db()
    rows = c.execute("""SELECT f.id, f.title, f.url, f.dossier_id, f.ordre, f.echecs,
                        COUNT(a.id) FROM feeds f
                        LEFT JOIN articles a ON a.feed_id = f.id
                        GROUP BY f.id ORDER BY f.dossier_id, f.ordre, f.title""").fetchall()
    c.close()
    return [{"id": r[0], "titre": r[1], "url": r[2], "dossier_id": r[3], "ordre": r[4],
             "echecs": r[5], "alerte": r[5] >= SEUIL_ECHECS_ALERTE,
             "articles": r[6]} for r in rows]

def add_feed(url, title="", dossier_id=None):
    if not url or not url.startswith(("http://", "https://")):
        return False, "URL invalide (elle doit commencer par http:// ou https://)."
    c = db()
    maxo = c.execute("SELECT COALESCE(MAX(ordre),-1) FROM feeds WHERE dossier_id IS ?",
                     (dossier_id,)).fetchone()[0]
    c.execute("INSERT OR IGNORE INTO feeds(url, title, dossier_id, ordre) VALUES(?,?,?,?)",
              (url.strip(), (title or url).strip(), dossier_id, maxo + 1))
    n = c.execute("SELECT changes()").fetchone()[0]
    c.commit(); c.close()
    return (True, "Flux ajouté.") if n else (False, "Ce flux existe déjà.")

def edit_feed(feed_id, titre, dossier_id):
    c = db()
    row = c.execute("SELECT title FROM feeds WHERE id=?", (feed_id,)).fetchone()
    if not row:
        c.close(); return False, "Flux introuvable."
    nouveau_titre = (titre or "").strip() or row[0]
    c.execute("UPDATE feeds SET title=?, dossier_id=? WHERE id=?",
              (nouveau_titre, dossier_id, feed_id))
    c.commit(); c.close()
    return True, f"Flux « {nouveau_titre} » mis à jour."

def delete_feed(feed_id, purge=False):
    c = db()
    row = c.execute("SELECT title FROM feeds WHERE id=?", (feed_id,)).fetchone()
    if not row:
        c.close(); return False, "Flux introuvable."
    if purge:
        c.execute("DELETE FROM articles WHERE feed_id=?", (feed_id,))
    c.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
    c.commit(); c.close()
    return True, f"Flux « {row[0]} » supprimé" + (" (articles effacés)." if purge
                                                  else " (articles conservés).")

def delete_feeds_bulk(ids, purge=False):
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return False, "Aucun flux sélectionné."
    c = db()
    q = ",".join("?" * len(ids))
    rows = c.execute(f"SELECT id, title FROM feeds WHERE id IN ({q})", ids).fetchall()
    if not rows:
        c.close(); return False, "Aucun flux correspondant trouvé."
    if purge:
        c.execute(f"DELETE FROM articles WHERE feed_id IN ({q})", ids)
    c.execute(f"DELETE FROM feeds WHERE id IN ({q})", ids)
    c.commit(); c.close()
    noms = ", ".join(f"« {r[1]} »" for r in rows)
    return True, (f"{len(rows)} flux supprimés ({noms})"
                  + (" — articles effacés." if purge else " — articles conservés."))

def reordonner_feeds(dossier_id, ids):
    """Réordonne (et réaffecte au besoin) les flux d'un dossier — ou de la
    racine si dossier_id est None — d'après la liste ordonnée reçue du
    glisser-déposer côté interface."""
    c = db()
    for i, fid in enumerate(ids or []):
        c.execute("UPDATE feeds SET dossier_id=?, ordre=? WHERE id=?", (dossier_id, i, int(fid)))
    c.commit(); c.close()
    return True

# ==================== OPML ====================

def normalize_url(u):
    """Normalise une URL pour comparaison (schéma, casse, barre finale)
    afin d'éviter les faux positifs lors d'une synchronisation OPML."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    return u.rstrip("/")

def opml_outlines(xml_text):
    """Retourne une liste de (url, titre, nom_dossier) à partir d'un OPML.
    nom_dossier correspond au groupement (<outline> sans xmlUrl) le plus
    proche du flux, vide si le flux est à la racine du fichier."""
    root = ET.fromstring(xml_text)
    out = []
    def walk(node, dossier=""):
        for o in node.findall("outline"):
            url = o.get("xmlUrl")
            if url:
                out.append((url, o.get("title") or o.get("text") or url, dossier))
            else:
                walk(o, o.get("title") or o.get("text") or dossier)
    body = root.find("body")
    if body is not None: walk(body)
    return out

def import_opml(xml_text):
    outlines = opml_outlines(xml_text)
    c = db(); n = 0
    cache_dossiers = {}
    for url, titre, nom_dossier in outlines:
        did = None
        if nom_dossier:
            if nom_dossier not in cache_dossiers:
                cache_dossiers[nom_dossier] = get_or_create_dossier(nom_dossier, c)
            did = cache_dossiers[nom_dossier]
        maxo = c.execute("SELECT COALESCE(MAX(ordre),-1) FROM feeds WHERE dossier_id IS ?",
                         (did,)).fetchone()[0]
        c.execute("INSERT OR IGNORE INTO feeds(url,title,dossier_id,ordre) VALUES(?,?,?,?)",
                  (url, titre, did, maxo + 1))
        n += c.execute("SELECT changes()").fetchone()[0]
    c.commit(); c.close()
    return n

def opml_preview(xml_text):
    """Prévisualise l'effet d'un import OPML sans toucher à la base :
    renvoie les nouveaux flux à ajouter et les flux existants absents
    du fichier (candidats à la suppression en mode synchronisation)."""
    outlines = opml_outlines(xml_text)
    urls_fichier = {normalize_url(u) for u, t, d in outlines}
    c = db()
    existants = c.execute("""SELECT f.id, f.title, f.url, COALESCE(d.nom,'')
                             FROM feeds f LEFT JOIN dossiers d ON d.id=f.dossier_id""").fetchall()
    c.close()
    urls_existantes = {normalize_url(u) for _, _, u, _ in existants}
    nouveaux = [{"url": u, "titre": t, "dossier": d} for u, t, d in outlines
                if normalize_url(u) not in urls_existantes]
    a_supprimer = [{"id": r[0], "titre": r[1], "url": r[2], "dossier": r[3]}
                   for r in existants if normalize_url(r[2]) not in urls_fichier]
    return nouveaux, a_supprimer

def opml_appliquer(xml_text, supprimer_ids=None, purge=False):
    """Applique un import OPML : ajoute toujours les nouveaux flux (en
    recréant les dossiers nommés dans le fichier si besoin), et supprime en
    plus les flux listés dans supprimer_ids (mode synchronisation)."""
    n = import_opml(xml_text)
    msg_sup = ""
    if supprimer_ids:
        ok, msg = delete_feeds_bulk(supprimer_ids, purge)
        if ok: msg_sup = " — " + msg
    return n, msg_sup

def export_opml():
    c = db()
    rows = c.execute("""SELECT f.title, f.url, COALESCE(d.nom,'') FROM feeds f
                        LEFT JOIN dossiers d ON d.id=f.dossier_id
                        ORDER BY d.ordre, f.ordre, f.title""").fetchall()
    c.close()
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<opml version="2.0"><head><title>RSSLocal export</title></head><body>']
    for t, u, dossier in rows:
        t = (t or "").replace('"', "'"); dossier = (dossier or "").replace('"', "'")
        lines.append(f'<outline text="{t}" title="{t}" type="rss" xmlUrl="{u}" category="{dossier}"/>')
    lines.append("</body></opml>")
    return "\n".join(lines)

# ==================== TEXTE INTÉGRAL (extraction heuristique) ====================

def _retirer_balises(page, tags):
    for t in tags:
        page = re.sub(rf"<{t}\b[^>]*>.*?</{t}>", " ", page, flags=re.I | re.S)
    return page

def _blocs(page, tag):
    return re.findall(rf"<{tag}\b[^>]*>.*?</{tag}>", page, flags=re.I | re.S)

def _texte_brut(bloc):
    t = re.sub(r"<[^>]+>", " ", bloc)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()

def _texte_paragraphes(bloc):
    """Reconstruit le texte en conservant les coupures de paragraphes
    (un <p> par ligne), plus lisible qu'un bloc de texte compact."""
    paragraphes = re.findall(r"<p\b[^>]*>(.*?)</p>", bloc, flags=re.I | re.S)
    if not paragraphes:
        return _texte_brut(bloc)
    textes = []
    for p in paragraphes:
        t = html.unescape(re.sub(r"<[^>]+>", " ", p))
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            textes.append(t)
    return "\n\n".join(textes)

def extraire_texte_de_html(page):
    """Heuristique d'extraction du corps d'un article à partir du HTML brut
    d'une page. Renvoie None si rien d'exploitable n'a été trouvé (ce qui
    déclenche le repli sur le résumé RSS côté appelant)."""
    page = _retirer_balises(page, ["script", "style", "nav", "header", "footer",
                                   "aside", "form", "iframe", "noscript", "svg"])
    candidats = _blocs(page, "article")
    bloc = max(candidats, key=lambda b: len(_texte_brut(b))) if candidats else None
    if not bloc:
        divs = _blocs(page, "div") + _blocs(page, "section")
        notes = []
        for b in divs:
            nb_p = len(re.findall(r"<p\b", b, re.I))
            if nb_p >= 2:
                notes.append((len(_texte_brut(b)), b))
        if notes:
            notes.sort(key=lambda x: x[0], reverse=True)
            bloc = notes[0][1]
    if not bloc:
        return None
    texte = _texte_paragraphes(bloc)
    if len(texte) < LONGUEUR_MIN_TEXTE_INTEGRAL:
        return None
    return texte[:LONGUEUR_MAX_TEXTE_INTEGRAL]

def extraire_texte_integral(url):
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            data = r.read()
            charset = r.headers.get_content_charset() or "utf-8"
    except Exception:
        return None
    try:
        page = data.decode(charset, errors="replace")
    except Exception:
        page = data.decode("utf-8", errors="replace")
    return extraire_texte_de_html(page)

def obtenir_texte_article(article_id, forcer=False):
    c = db()
    row = c.execute("""SELECT link, texte_integral, texte_recupere_le, summary
                       FROM articles WHERE id=?""", (article_id,)).fetchone()
    if not row:
        c.close(); return {"ok": False, "message": "Article introuvable."}
    link, cache, recupere_le, resume = row
    if cache and not forcer:
        c.close()
        return {"ok": True, "texte": cache, "source": "integral", "recupere_le": recupere_le}
    texte = extraire_texte_integral(link)
    now = datetime.now(timezone.utc).isoformat()
    if texte:
        c.execute("UPDATE articles SET texte_integral=?, texte_recupere_le=? WHERE id=?",
                  (texte, now, article_id))
        c.commit(); c.close()
        return {"ok": True, "texte": texte, "source": "integral", "recupere_le": now}
    c.execute("UPDATE articles SET texte_recupere_le=? WHERE id=?", (now, article_id))
    c.commit(); c.close()
    return {"ok": True, "texte": resume, "source": "resume", "recupere_le": now,
            "message": "Texte intégral non disponible sur ce média — affichage du résumé."}

# ==================== ARTICLES : PÉRIODES / LECTURE / EXPORTS / WEBHOOK ====================

def borne_periode(start, end):
    """Normalise les bornes ; par défaut : aujourd'hui."""
    today = datetime.now().strftime("%Y-%m-%d")
    start = start or today
    end = end or start
    if start > end: start, end = end, start
    return start, end

def articles_periode(start=None, end=None, feed_ids=None):
    start, end = borne_periode(start, end)
    c = db()
    q = """SELECT a.id, a.title, a.link, a.summary, a.published, f.title, f.dossier_id, a.lu
           FROM articles a JOIN feeds f ON f.id=a.feed_id
           WHERE substr(a.published,1,10) >= ? AND substr(a.published,1,10) <= ?"""
    params = [start, end]
    if feed_ids:
        q += f" AND a.feed_id IN ({','.join('?' * len(feed_ids))})"
        params += list(feed_ids)
    q += " ORDER BY a.published DESC"
    rows = c.execute(q, params).fetchall()
    c.close()
    return [{"id": r[0], "titre": r[1], "lien": r[2], "resume": r[3], "date": r[4],
             "flux": r[5], "dossier_id": r[6], "lu": bool(r[7])} for r in rows]

def marquer_lu(article_id, lu):
    c = db()
    c.execute("UPDATE articles SET lu=? WHERE id=?", (1 if lu else 0, article_id))
    c.commit(); c.close()
    return True

def send_webhook(start=None, end=None):
    if not WEBHOOK_N8N:
        return False, "Aucune URL de webhook configurée (variable WEBHOOK_N8N)."
    start, end = borne_periode(start, end)
    payload = json.dumps({"periode": f"{start}_{end}",
                          "articles": articles_periode(start, end)},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_N8N, data=payload,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            return True, f"Webhook OK (HTTP {r.status})"
    except Exception as e:
        return False, f"Échec webhook: {e}"

# ==================== ANALYSE IA (Sonnet seul, prompt unique) ====================

def appel_claude(modele, prompt, max_tokens=2500):
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({
            "model": modele,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]
        }).encode("utf-8"),
        headers={"x-api-key": API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180, context=SSL_CTX) as r:
        rep = json.loads(r.read())
    return rep["content"][0]["text"]

def analyser_periode(start=None, end=None):
    if not API_KEY:
        return False, ("Aucune clé API configurée. Renseignez API_KEY dans rss_local.py "
                       "ou la variable d'environnement ANTHROPIC_API_KEY "
                       "(clé + crédits sur console.anthropic.com, Plans & Billing).")
    start, end = borne_periode(start, end)
    cle_periode = start if start == end else f"{start}_{end}"
    arts = articles_periode(start, end)
    if not arts:
        return False, f"Aucun article sur la période {start} → {end}. Rafraîchissez d'abord."

    prompt = get_reglage("prompt_analyse", PROMPT_ANALYSE_DEFAUT)
    libelle = start if start == end else f"du {start} au {end}"
    liste = "\n".join(f"- [{a['flux']}] {a['titre']} — {a['resume'][:120]}" for a in arts)

    try:
        synthese = appel_claude(MODELE_ANALYSE,
            prompt + f"\n\n(Période : {libelle} — {len(arts)} articles.)\n\n" + liste)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return False, f"Erreur API (HTTP {e.code}) : {detail}"
    except Exception as e:
        return False, f"Erreur d'analyse : {e}"

    c = db()
    c.execute("INSERT OR REPLACE INTO syntheses(jour, texte, cree) VALUES(?,?,?)",
              (cle_periode, synthese, datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close()
    return True, synthese

def synthese_existante(start, end):
    start, end = borne_periode(start, end)
    cle = start if start == end else f"{start}_{end}"
    c = db()
    row = c.execute("SELECT texte FROM syntheses WHERE jour=?", (cle,)).fetchone()
    c.close()
    return row[0] if row else None

def synthese_markdown(start, end):
    start, end = borne_periode(start, end)
    texte = synthese_existante(start, end)
    libelle = start if start == end else f"{start} → {end}"
    if not texte:
        return None, libelle
    md = (f"# Synthèse RSSLocal — {libelle}\n\n"
          f"*Générée le {datetime.now().strftime('%d/%m/%Y à %H:%M')} — "
          f"modèle : {MODELE_ANALYSE}*\n\n---\n\n{texte}\n")
    return md, libelle

# ==================== INTERFACE WEB ====================

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>RSSLocal</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;background:#f7f7f5;color:#222;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.topbar{padding:10px 16px 0}
h1{font-size:1.25rem;margin:6px 0}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;align-items:center}
button,a.btn,label.btn{padding:7px 12px;border:1px solid #bbb;border-radius:8px;background:#fff;cursor:pointer;text-decoration:none;color:#222;font-size:.85rem}
button:hover,a.btn:hover,label.btn:hover{background:#eee}
button.ia{background:#4a3b8f;color:#fff;border-color:#4a3b8f}button.ia:hover{background:#5c4bb0}
button.danger{border-color:#c0392b;color:#c0392b}button.danger:hover{background:#fdf0ee}
select,input[type=date],input[type=text],input[type=email]{padding:5px;border-radius:8px;border:1px solid #bbb;font-size:.85rem}
#status{font-size:.82rem;color:#333;background:#eee;padding:6px 12px;border-radius:8px;display:none;margin:6px 16px}
#status details{margin-top:4px}#status summary{cursor:pointer;color:#666;font-size:.78rem}
#status pre{white-space:pre-wrap;font-size:.76rem;color:#555;margin:4px 0 0}
.panel{background:#fff;border:1px solid #ccc;border-radius:10px;padding:14px 16px;margin:6px 16px;display:none}
.panel h2{font-size:1rem;margin:0 0 8px}
.feedrow{display:flex;justify-content:space-between;align-items:flex-start;padding:5px 0;border-bottom:1px solid #eee;font-size:.85rem;gap:8px}
.feedrow .finfo{flex:1;min-width:0}.feedrow .furl{color:#999;font-size:.72rem;word-break:break-all}
.feedrow button{padding:3px 8px;font-size:.75rem;white-space:nowrap}
.editbox{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}.editbox input,.editbox select{flex:1;min-width:100px}
.badge{cursor:help}
textarea{width:100%;box-sizing:border-box;min-height:100px;border:1px solid #bbb;border-radius:8px;padding:8px;font-size:.82rem;font-family:inherit}
.addrow{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.addrow input,.addrow select{flex:1;min-width:110px}
#customdates{display:none;gap:6px;align-items:center}
.reglrow{display:flex;gap:8px;align-items:center;margin:6px 0;flex-wrap:wrap}

/* ─── 3 COLONNES ─── */
#main{flex:1;display:flex;overflow:hidden;min-height:0}

#sidebar{width:230px;min-width:190px;flex-shrink:0;background:#20242c;color:#e4e6eb;display:flex;flex-direction:column;overflow:hidden}
#sidebar-scroll{flex:1;overflow-y:auto;padding:6px 0}
#sidebar-scroll::-webkit-scrollbar{width:5px}
#sidebar-scroll::-webkit-scrollbar-thumb{background:#4a4f59;border-radius:3px}
#sidebar-bottom{padding:8px;border-top:1px solid #3a3f4a;display:flex;flex-direction:column;gap:6px}
#sidebar-bottom button{background:#2c313c;color:#e4e6eb;border:1px solid #3a3f4a}
#sidebar-bottom button:hover{background:#3a3f4a}
.sb-item{display:block;width:calc(100% - 12px);margin:2px 6px;text-align:left;padding:7px 10px;border:none;border-radius:8px;background:none;color:#e4e6eb;font-size:.85rem;cursor:pointer;font-family:inherit}
.sb-item:hover{background:#2c313c}
.sb-item.active{background:#3a4a6b;color:#8ab4f8}
.sb-dossier-head{display:flex;align-items:center;gap:6px;margin:2px 6px;padding:6px 8px;border-radius:8px;cursor:pointer;font-size:.85rem;font-weight:600}
.sb-dossier-head:hover{background:#2c313c}
.sb-dossier-head.active{background:#3a4a6b;color:#8ab4f8}
.sb-dossier-head.drag-over{box-shadow:inset 0 0 0 2px #8ab4f8;background:#2c313c}
.sb-toggle{width:12px;flex-shrink:0;text-align:center;color:#9aa0a6;font-size:.7rem}
.sb-nom{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-actions{display:none;gap:2px;flex-shrink:0}
.sb-dossier-head:hover .sb-actions{display:flex}
.sb-actions button{background:none;border:none;color:#9aa0a6;cursor:pointer;font-size:.78rem;padding:1px 3px}
.sb-actions button:hover{color:#8ab4f8}
.sb-feeds{padding-left:16px}
.sb-flux{display:flex;align-items:center;gap:6px;margin:1px 6px;padding:6px 8px;border-radius:8px;cursor:grab;font-size:.82rem;color:#c7cad1}
.sb-flux:hover{background:#2c313c}
.sb-flux.active{background:#3a4a6b;color:#8ab4f8}
.sb-flux.dragging{opacity:.35}
.sb-flux.drag-over{box-shadow:inset 0 0 0 2px #8ab4f8}
.sb-fnom{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-vide{padding:4px 12px;font-size:.75rem;color:#666}
.sb-sans{cursor:default}

#col-articles{flex:1 1 620px;min-width:340px;background:#fff;border-right:1px solid #ddd;display:flex;flex-direction:column;overflow:hidden}
#col-articles-tools{padding:8px 10px;border-bottom:1px solid #eee;display:flex;gap:6px;flex-wrap:wrap;flex-shrink:0}
#article-list{flex:1;overflow-y:auto}
#article-list::-webkit-scrollbar{width:5px}
#article-list::-webkit-scrollbar-thumb{background:#dadce0;border-radius:3px}
.a-item{padding:12px 14px;border-bottom:1px solid #eee;cursor:pointer;border-left:3px solid transparent}
.a-item:hover{background:#f8f9fa}
.a-item.active{background:#e8f0fe;border-left-color:#1a73e8}
.a-item.lu{opacity:.5}
.a-item.surligne{background:#fffdf2;border-left-color:#e6b800}
.a-top{display:flex;align-items:center;gap:6px;margin-bottom:1px}
.a-check{flex-shrink:0;cursor:pointer}
.a-feed{font-size:.7rem;color:#777;text-transform:uppercase;letter-spacing:.3px;font-weight:600}
.a-title{font-size:.95rem;font-weight:600;margin:2px 0 4px;line-height:1.32}
.a-resume{font-size:.85rem;color:#555;line-height:1.4;margin-bottom:6px}
.a-meta{font-size:.74rem;color:#999;display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.luBtn{padding:1px 7px;font-size:.7rem;border:1px solid #ccc;border-radius:6px;background:#fafafa;cursor:pointer}
.a-share{display:flex;gap:6px;flex-wrap:wrap}
.a-share a,.a-share button{padding:3px 8px;font-size:.76rem;border:1px solid #ddd;border-radius:6px;background:#fafafa;cursor:pointer;text-decoration:none;color:#333}
details.media{margin:4px 8px}details.media summary{cursor:pointer;font-weight:600;font-size:.82rem;padding:6px 4px;color:#555}
.sb-favicon{width:14px;height:14px;flex-shrink:0;border-radius:2px;background:#3a3f4a}

.splitter{width:6px;flex-shrink:0;cursor:col-resize;background:#e2e2e0}
.splitter:hover,.splitter.dragging{background:#b9c6e0}

/* ─── BARRE D'EXPORT FLOTTANTE ─── */
#export-bar{position:fixed;left:0;right:0;bottom:0;background:#1b2430;color:#fff;transform:translateY(100%);transition:transform .25s ease;z-index:200;box-shadow:0 -4px 16px rgba(0,0,0,.2)}
#export-bar.open{transform:translateY(0)}
#export-bar-inner{display:flex;align-items:center;gap:12px;padding:10px 16px}
#export-count{font-size:.82rem;font-weight:600;white-space:nowrap}
#export-num{background:#e37400;border-radius:10px;padding:1px 8px;margin-right:6px}
#export-chips{flex:1;display:flex;gap:6px;overflow-x:auto;padding:2px 0}
.export-chip{display:flex;align-items:center;gap:5px;background:#2d3a47;padding:4px 9px;border-radius:14px;font-size:.75rem;white-space:nowrap;flex-shrink:0}
.export-chip button{background:none;border:none;color:#9aa0a6;cursor:pointer;font-size:.75rem;padding:0}
.export-chip button:hover{color:#fff}
#export-actions{display:flex;gap:8px;flex-shrink:0}
#export-actions button{background:#2d3a47;color:#fff;border:1px solid #3d4a57}
#export-actions button:hover{background:#3d4a57}

#col-lecture{flex:1 1 420px;min-width:300px;overflow-y:auto;background:#fff;padding:24px 32px}
#col-lecture::-webkit-scrollbar{width:6px}
#col-lecture::-webkit-scrollbar-thumb{background:#dadce0;border-radius:3px}
.lect-placeholder{color:#999;font-size:.9rem;padding:40px 0;text-align:center}
.lect-head h2{font-size:1.3rem;margin:0 0 6px;line-height:1.3}
.lect-meta{color:#888;font-size:.82rem;margin-bottom:12px}
.lect-actions{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px}
.lect-actions a,.lect-actions button{padding:5px 11px;font-size:.8rem;border:1px solid #ccc;border-radius:8px;background:#fafafa;cursor:pointer;text-decoration:none;color:#333}
.lect-corps{font-size:.95rem;line-height:1.65;color:#2a2a2a;max-width:70ch}
.lect-corps p{margin:0 0 14px}
.lect-fallback{background:#fef7e0;border:1px solid #e6d98a;border-radius:8px;padding:8px 12px;font-size:.82rem;color:#7a6a1a;margin-bottom:14px}
.lect-loading{color:#999;font-style:italic}

#synthbox{background:#fffbe8;border-bottom:1px solid #e6d98a;flex-shrink:0;display:none}
#synthhead{display:flex;justify-content:space-between;align-items:center;padding:9px 14px;cursor:pointer;font-weight:600;font-size:.85rem;gap:8px}
#synthtitle{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#synthtoggle{color:#8a7b2e;flex-shrink:0;font-size:.75rem}
#synthclose{border:none;background:none;font-size:1rem;cursor:pointer;color:#8a7b2e;flex-shrink:0;padding:0 2px}
#synthclose:hover{color:#5a4e12}
#synthbody{white-space:pre-wrap;padding:0 14px 10px;font-size:.86rem;max-height:280px;overflow-y:auto}
#synthactions{padding:0 14px 12px;display:flex;gap:8px}
#synthactions a,#synthactions button{padding:4px 10px;font-size:.76rem;border:1px solid #d8c96a;border-radius:6px;background:#fff;cursor:pointer;text-decoration:none;color:#333}
#opmlpreview{display:none}
</style></head><body>
<div class="topbar">
<h1>📰 RSSLocal v5</h1>
<div class="bar">
<button onclick="refresh()">🔄 Rafraîchir</button>
<select id="periode" onchange="periodeChange()">
<option value="jour">Aujourd'hui</option>
<option value="7">7 derniers jours</option>
<option value="30">30 derniers jours</option>
<option value="perso">Période personnalisée</option>
</select>
<span id="customdates">du <input type="date" id="dstart"> au <input type="date" id="dend">
<button onclick="load()">OK</button></span>
<select id="tri" onchange="render()">
<option value="desc">Plus récents d'abord</option>
<option value="asc">Plus anciens d'abord</option>
<option value="media">Grouper par média</option>
</select>
<select id="lufiltre" onchange="render()">
<option value="tous">Tous (lus et non lus)</option>
<option value="nonlu">Non lus seulement</option>
<option value="lu">Lus seulement</option>
</select>
<input type="text" id="recherche" placeholder="🔎 Rechercher…" oninput="render()" style="min-width:130px">
<button class="ia" onclick="analyse()">🧠 Analyser la période</button>
</div>
<div class="bar">
<button onclick="toggle('pfeeds');loadFeeds()">⚙️ Flux</button>
<button onclick="toggle('preglages');loadReglages()">🛠 Réglages</button>
<a class="btn" id="ejson" href="#">⬇ JSON</a>
<a class="btn" id="ecsv" href="#">⬇ CSV</a>
<a class="btn" href="/export/opml">⬇ OPML</a>
<button onclick="hook()">📤 n8n</button>
<label class="btn">📁 Importer OPML<input type="file" id="opml" style="display:none" onchange="upload(this)"></label>
</div>
</div>
<div id="status"></div>
<div class="panel" id="opmlpreview"></div>

<div class="panel" id="pfeeds">
<h2>⚙️ Gérer les flux</h2>
<div id="feedlist"></div>
<div class="bar">
<button class="danger" onclick="delFeedsBulk()">🗑 Supprimer la sélection</button>
</div>
<div class="addrow">
<input type="text" id="furl" placeholder="URL du flux (https://…)">
<input type="text" id="ftitre" placeholder="Titre (optionnel)">
<select id="fdossier"></select>
<button onclick="addFeed()">➕ Ajouter</button>
</div>
<h2 style="margin-top:16px">🗞️ Générer un flux Google News</h2>
<div class="addrow">
<input type="text" id="gnquery" placeholder="Mot-clé de recherche (ex: intelligence artificielle)">
<select id="gnlang">
<option value="fr|FR">Français (France)</option>
<option value="fr|BE">Français (Belgique)</option>
<option value="en-US|US">Anglais (États-Unis)</option>
<option value="en-GB|GB">Anglais (Royaume-Uni)</option>
<option value="es|ES">Espagnol (Espagne)</option>
<option value="de|DE">Allemand (Allemagne)</option>
</select>
<select id="gndossier"></select>
<button onclick="ajouterFluxGoogleNews()">➕ Créer ce flux</button>
</div>
<p style="font-size:.78rem;color:#888">Crée un flux RSS Google News basé sur une recherche par mot-clé —
utile pour une veille ponctuelle sans devoir trouver le flux RSS natif d'un média.</p>
</div>

<div class="panel" id="preglages">
<h2>🛠 Réglages</h2>
<div class="reglrow"><label>✉️ Destinataire mail par défaut :</label>
<input type="email" id="rmail" placeholder="prenom.nom@exemple.fr" style="flex:1;min-width:200px">
<button onclick="saveMail()">💾</button></div>
<div class="reglrow"><label>🔎 Mots-clés à surveiller (séparés par des virgules) :</label>
<input type="text" id="rmots" placeholder="ex: budget, grève, élection" style="flex:1;min-width:200px">
<button onclick="saveMots()">💾</button></div>
<p style="font-size:.78rem;color:#888;margin-top:-4px">Les articles correspondants sont surlignés dans la liste (aucune notification).</p>
<p style="font-size:.82rem;color:#666;margin-bottom:4px">🧠 Prompt d'analyse (envoyé au modèle avec la liste des articles) :</p>
<textarea id="pana"></textarea>
<div class="bar">
<button onclick="savePrompt()">💾 Enregistrer le prompt</button>
<button onclick="resetPrompt()">↩️ Prompt par défaut</button>
</div>
<h2 style="margin-top:16px">🗑 Purges</h2>
<div class="bar">
<button class="danger" onclick="purgePeriode()">Effacer les articles de la période affichée</button>
<button class="danger" onclick="purgeTotale()">Tout effacer (articles + synthèses)</button>
</div>
<p style="font-size:.78rem;color:#888">Purge automatique : les articles et synthèses de plus de
<b>CONSERVER_JOURS</b> jours (réglé dans le script) sont supprimés à chaque rafraîchissement.</p>
</div>

<div id="main">
<div id="sidebar">
<div id="sidebar-scroll"></div>
<div id="sidebar-bottom">
<button onclick="nouveauDossierUI()">➕ Nouveau dossier</button>
</div>
</div>
<div id="col-articles">
<div id="synthbox">
<div id="synthhead" onclick="foldSynth()"><span id="synthtitle">🧠 Synthèse</span>
<span id="synthtoggle">▼</span>
<button id="synthclose" onclick="event.stopPropagation();closeSynth()" title="Fermer">✕</button></div>
<div id="synthbody"></div>
<div id="synthactions">
<a id="smd" href="#">⬇ Export .md</a>
<a id="smail" href="#">✉️ Envoyer par mail</a>
</div>
</div>
<div id="col-articles-tools"><span style="font-size:.76rem;color:#888">Raccourcis : <b>J</b>/<b>K</b> naviguer · <b>E</b> sélectionner · <b>O</b> ouvrir l'original</span></div>
<div id="article-list"></div>
</div>
<div id="splitter"></div>
<div id="col-lecture"><div class="lect-placeholder">Sélectionnez un article dans la liste pour l'afficher ici.</div></div>
</div>

<div id="export-bar">
<div id="export-bar-inner">
<div id="export-count"><span id="export-num">0</span>article(s) sélectionné(s)</div>
<div id="export-chips"></div>
<div id="export-actions">
<button onclick="exporterSelectionMd()">⬇ Export .md</button>
<button onclick="viderSelection()">Vider</button>
</div>
</div>
</div>

<script>
let syntheseCourante=null;let syntheseRepliee={};let syntheseFermee={};let mailDefaut='';let motsCles=[];let articlesCache=[];
let feedsCache=[];let dossiersCache=[];let dossiersReplies={};let vue={type:'tous'};
let articleSelectionne=null;let xmlEnAttente='';
let selectionExport=new Map();let ordreAffiche=[];

function favicon(url){try{const h=new URL(url).hostname;
return `https://www.google.com/s2/favicons?domain=${h}&sz=32`;}catch{return'';}}

function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function fmt(d){return d.toISOString().slice(0,10);}
function bornes(){
const p=document.getElementById('periode').value;const now=new Date();
if(p==='jour')return[fmt(now),fmt(now)];
if(p==='7'){const s=new Date(now);s.setDate(s.getDate()-6);return[fmt(s),fmt(now)];}
if(p==='30'){const s=new Date(now);s.setDate(s.getDate()-29);return[fmt(s),fmt(now)];}
return[document.getElementById('dstart').value||fmt(now),
document.getElementById('dend').value||fmt(now)];}
function periodeChange(){
document.getElementById('customdates').style.display=
document.getElementById('periode').value==='perso'?'inline-flex':'none';
if(document.getElementById('periode').value!=='perso')load();}

function feedsDe(dossierId){return feedsCache.filter(f=>f.dossier_id===dossierId).map(f=>f.id);}
function feedIdsPourVue(){
if(vue.type==='flux')return[vue.id];
if(vue.type==='dossier')return feedsDe(vue.id);
return null;}

function qs(){const[s,e]=bornes();let q='start='+s+'&end='+e;
const fids=feedIdsPourVue();
if(fids&&fids.length)q+='&feeds='+fids.join(',');
return q;}
function links(){document.getElementById('ejson').href='/export/json?'+qs();
document.getElementById('ecsv').href='/export/csv?'+qs();
document.getElementById('smd').href='/export/md?'+qs();}
function toggle(id){const p=document.getElementById(id);
p.style.display=p.style.display==='block'?'none':'block';}
function show(t,detail){const s=document.getElementById('status');s.style.display='block';
s.innerHTML=esc(t)+(detail?'<details><summary>voir le détail</summary><pre>'+esc(detail)+'</pre></details>':'');}

/* ─── BARRE LATÉRALE : DOSSIERS & FLUX ─── */

async function chargerBarreLaterale(){
const[rf,rd]=await Promise.all([fetch('/api/feeds'),fetch('/api/dossiers')]);
feedsCache=await rf.json();dossiersCache=await rd.json();
renderSidebar();}

function ligneFlux(f){
return `<div class="sb-flux ${vue.type==='flux'&&vue.id===f.id?'active':''}" draggable="true"
onclick="definirVue({type:'flux',id:${f.id}})"
ondragstart="event.dataTransfer.setData('text/plain','${f.id}');this.classList.add('dragging')"
ondragend="this.classList.remove('dragging')"
ondragover="event.preventDefault();event.stopPropagation();this.classList.add('drag-over')"
ondragleave="this.classList.remove('drag-over')"
ondrop="deposerSurFlux(event,${f.id})">
<img class="sb-favicon" src="${favicon(f.url)}" onerror="this.style.display='none'" alt="">
<span class="sb-fnom">${esc(f.titre)}</span>${f.alerte?' <span class="badge" title="'+f.echecs+' échecs consécutifs">⚠️</span>':''}
</div>`;}

function renderSidebar(){
const sansDossier=feedsCache.filter(f=>f.dossier_id===null);
let html=`<button class="sb-item ${vue.type==='tous'?'active':''}" onclick="definirVue({type:'tous'})">📋 Tous les flux</button>`;
html+=dossiersCache.map(d=>{
const feeds=feedsCache.filter(f=>f.dossier_id===d.id);
const ouvert=!dossiersReplies[d.id];
const nomEsc=esc(d.nom).replace(/'/g,"\\\\'");
return `<div class="sb-dossier">
<div class="sb-dossier-head ${vue.type==='dossier'&&vue.id===d.id?'active':''}"
onclick="definirVue({type:'dossier',id:${d.id}})"
ondragover="event.preventDefault();this.classList.add('drag-over')"
ondragleave="this.classList.remove('drag-over')"
ondrop="deposerSurDossier(event,${d.id})">
<span class="sb-toggle" onclick="event.stopPropagation();basculerDossier(${d.id})">${ouvert?'▼':'▶'}</span>
<span class="sb-nom">${esc(d.nom)}</span>
<span class="sb-actions">
<button onclick="event.stopPropagation();renommerDossierUI(${d.id},'${nomEsc}')" title="Renommer">✏️</button>
<button onclick="event.stopPropagation();supprimerDossierUI(${d.id},'${nomEsc}')" title="Supprimer">🗑</button>
</span></div>
${ouvert?`<div class="sb-feeds">${feeds.map(ligneFlux).join('')||'<div class="sb-vide">Aucun flux</div>'}</div>`:''}
</div>`;}).join('');
const ouvertSans=!dossiersReplies['sans'];
html+=`<div class="sb-dossier">
<div class="sb-dossier-head sb-sans"
ondragover="event.preventDefault();this.classList.add('drag-over')"
ondragleave="this.classList.remove('drag-over')"
ondrop="deposerSurDossier(event,null)">
<span class="sb-toggle" onclick="basculerDossier('sans')">${ouvertSans?'▼':'▶'}</span>
<span class="sb-nom">📂 Sans dossier</span></div>
${ouvertSans?`<div class="sb-feeds">${sansDossier.map(ligneFlux).join('')||'<div class="sb-vide">Aucun flux</div>'}</div>`:''}
</div>`;
document.getElementById('sidebar-scroll').innerHTML=html;}

function basculerDossier(id){dossiersReplies[id]=!dossiersReplies[id];renderSidebar();}
function definirVue(v){vue=v;renderSidebar();load();}

async function deposerSurDossier(e,dossierId){
e.preventDefault();e.currentTarget.classList.remove('drag-over');
const fid=+e.dataTransfer.getData('text/plain');
let ids=feedsDe(dossierId).filter(id=>id!==fid);ids.push(fid);
await fetch('/api/feeds/reorder',{method:'POST',body:JSON.stringify({dossier_id:dossierId,ids:ids})});
chargerBarreLaterale();}

async function deposerSurFlux(e,cibleId){
e.preventDefault();e.stopPropagation();e.currentTarget.classList.remove('drag-over');
const fid=+e.dataTransfer.getData('text/plain');
if(fid===cibleId)return;
const cible=feedsCache.find(f=>f.id===cibleId);
const dossierId=cible?cible.dossier_id:null;
let ids=feedsDe(dossierId).filter(id=>id!==fid);
const idx=ids.indexOf(cibleId);
ids.splice(idx,0,fid);
await fetch('/api/feeds/reorder',{method:'POST',body:JSON.stringify({dossier_id:dossierId,ids:ids})});
chargerBarreLaterale();}

async function nouveauDossierUI(){
const nom=prompt('Nom du nouveau dossier :');
if(!nom)return;
await fetch('/api/dossiers/add',{method:'POST',body:JSON.stringify({nom:nom})});
chargerBarreLaterale();}

async function renommerDossierUI(id,nomActuel){
const nom=prompt('Renommer le dossier :',nomActuel);
if(!nom||nom===nomActuel)return;
const r=await fetch('/api/dossiers/rename',{method:'POST',body:JSON.stringify({id:id,nom:nom})});
const j=await r.json();if(!j.ok)show(j.message);
chargerBarreLaterale();}

async function supprimerDossierUI(id,nom){
if(!confirm('Supprimer le dossier « '+nom+' » ?\\n(Les flux qu\\'il contient seront déplacés hors dossier, pas supprimés.)'))return;
await fetch('/api/dossiers/delete',{method:'POST',body:JSON.stringify({id:id})});
if(vue.type==='dossier'&&vue.id===id)definirVue({type:'tous'});else chargerBarreLaterale();}

/* ─── LISTE CENTRALE DES ARTICLES ─── */

function artCarte(x){
const cible=(x.titre+' '+x.resume).toLowerCase();
const surligne=motsCles.length&&motsCles.some(k=>cible.includes(k));
const txt=encodeURIComponent(x.titre+' — '+x.lien);
const mail='mailto:'+encodeURIComponent(mailDefaut)+'?subject='+encodeURIComponent(x.titre)+'&body='+txt;
const wa='https://wa.me/?text='+txt;
return `<div class="a-item ${x.lu?'lu':''} ${surligne?'surligne':''} ${articleSelectionne===x.id?'active':''}"
onclick="ouvrirArticle(${x.id})">
<div class="a-top"><input type="checkbox" class="a-check" onclick="toggleSelection(event,${x.id})"
${selectionExport.has(x.id)?'checked':''}><span class="a-feed">${esc(x.flux)}</span></div>
<div class="a-title">${esc(x.titre)}</div>
<div class="a-resume">${esc(x.resume)}</div>
<div class="a-meta"><span>${x.date?x.date.slice(0,16).replace('T',' '):''}</span>
<button class="luBtn" onclick="event.stopPropagation();toggleLu(${x.id},${!x.lu})">${x.lu?'✓ Lu':'○ Non lu'}</button></div>
<div class="a-share" onclick="event.stopPropagation()">
<a href="${mail}">✉️ Mail</a>
<a href="${wa}" target="_blank">💬 WhatsApp</a>
<button onclick="navigator.clipboard.writeText('${esc(x.lien)}');this.textContent='✓ Copié'">📋 Copier</button>
<a href="${esc(x.lien)}" target="_blank">🔗 Original</a>
</div></div>`;}

function render(){
let a=articlesCache.slice();
const mode=document.getElementById('tri').value;
const q=(document.getElementById('recherche').value||'').toLowerCase().trim();
const luF=document.getElementById('lufiltre').value;
if(q)a=a.filter(x=>(x.titre+' '+x.resume).toLowerCase().includes(q));
if(luF==='nonlu')a=a.filter(x=>!x.lu);
if(luF==='lu')a=a.filter(x=>x.lu);
if(mode==='asc')a=a.slice().reverse();
ordreAffiche=a.map(x=>x.id);
const list=document.getElementById('article-list');
if(!a.length){list.innerHTML='<p style="padding:16px;color:#888;font-size:.85rem">Aucun article ne correspond à ces critères.</p>';return;}
if(mode==='media'){
const g={};a.forEach(x=>{(g[x.flux]=g[x.flux]||[]).push(x);});
list.innerHTML=Object.keys(g).sort().map(m=>
`<details class="media" open><summary>${esc(m)} (${g[m].length})</summary>
${g[m].map(artCarte).join('')}</details>`).join('');}
else{list.innerHTML=a.map(artCarte).join('');}}

/* ─── SÉLECTION MULTIPLE & EXPORT GROUPÉ ─── */

function toggleSelection(e,id){e.stopPropagation();
if(selectionExport.has(id)){selectionExport.delete(id);}
else{const a=articlesCache.find(x=>x.id===id);if(a)selectionExport.set(id,a);}
renderExportBar();render();}

function retirerSelection(id){selectionExport.delete(id);renderExportBar();render();}
function viderSelection(){selectionExport.clear();renderExportBar();render();}

function renderExportBar(){
const bar=document.getElementById('export-bar');
const n=selectionExport.size;
document.getElementById('export-num').textContent=n+' ';
document.getElementById('export-chips').innerHTML=[...selectionExport.values()].map(a=>{
const t=a.titre.length>30?a.titre.slice(0,30)+'…':a.titre;
return `<span class="export-chip">${esc(t)}<button onclick="retirerSelection(${a.id})">✕</button></span>`;
}).join('');
bar.classList.toggle('open',n>0);}

function exporterSelectionMd(){
const arts=[...selectionExport.values()];
if(!arts.length)return;
const maintenant=new Date().toLocaleString('fr-FR');
let md=`# Sélection RSSLocal — ${maintenant}\n\n> ${arts.length} article${arts.length>1?'s':''} sélectionné${arts.length>1?'s':''}\n\n`;
arts.forEach(a=>{md+=`## ${a.titre}\n\n*${a.flux} — ${(a.date||'').slice(0,16).replace('T',' ')}*\n\n`
+`${a.resume||''}\n\n[Lire l'article](${a.lien})\n\n---\n\n`;});
const blob=new Blob([md],{type:'text/markdown;charset=utf-8'});
const url=URL.createObjectURL(blob);
const lien=document.createElement('a');lien.href=url;lien.download='selection_'+fmt(new Date())+'.md';
document.body.appendChild(lien);lien.click();lien.remove();
URL.revokeObjectURL(url);
show('✓ '+arts.length+' article(s) exporté(s) en Markdown.');}

/* ─── RACCOURCIS CLAVIER ─── */

function naviguerArticle(direction){
if(!ordreAffiche.length)return;
let idx=ordreAffiche.indexOf(articleSelectionne);
idx=idx===-1?(direction>0?0:ordreAffiche.length-1):idx+direction;
if(idx<0)idx=0;if(idx>=ordreAffiche.length)idx=ordreAffiche.length-1;
ouvrirArticle(ordreAffiche[idx]);}

document.addEventListener('keydown',e=>{
const tag=(document.activeElement||{}).tagName;
if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT')return;
if(e.key==='j'||e.key==='J'){naviguerArticle(1);}
else if(e.key==='k'||e.key==='K'){naviguerArticle(-1);}
else if(e.key==='e'||e.key==='E'){if(articleSelectionne)toggleSelection({stopPropagation(){}},articleSelectionne);}
else if(e.key==='o'||e.key==='O'){const a=articlesCache.find(x=>x.id===articleSelectionne);if(a)window.open(a.lien,'_blank');}
});

async function toggleLu(id,lu){
await fetch('/api/articles/lu',{method:'POST',body:JSON.stringify({id:id,lu:lu})});
const idx=articlesCache.findIndex(x=>x.id===id);if(idx>=0)articlesCache[idx].lu=lu;
render();}

async function load(){links();
const r=await fetch('/api/articles?'+qs());articlesCache=await r.json();
render();
const sr=await fetch('/api/synthese?'+qs());const js=await sr.json();
syntheseCourante=js.texte||null;
renderSynthBox();}

function renderSynthBox(){
const box=document.getElementById('synthbox');
const[s,e]=bornes();const cle=s+'_'+e;
if(!syntheseCourante||syntheseFermee[cle]){box.style.display='none';return;}
box.style.display='block';
const pliee=!!syntheseRepliee[cle];
document.getElementById('synthtitle').textContent='🧠 Synthèse '+(s===e?('du '+s):('du '+s+' au '+e));
document.getElementById('synthtoggle').textContent=pliee?'▶ déplier':'▼ replier';
document.getElementById('synthbody').style.display=pliee?'none':'block';
document.getElementById('synthactions').style.display=pliee?'none':'flex';
document.getElementById('synthbody').textContent=syntheseCourante;
const body=encodeURIComponent(syntheseCourante.slice(0,1800));
document.getElementById('smail').href='mailto:'+encodeURIComponent(mailDefaut)
+'?subject='+encodeURIComponent('Synthèse RSSLocal — '+(s===e?s:s+' au '+e))
+'&body='+body;}

function foldSynth(){const[s,e]=bornes();const cle=s+'_'+e;
syntheseRepliee[cle]=!syntheseRepliee[cle];renderSynthBox();}

function closeSynth(){const[s,e]=bornes();const cle=s+'_'+e;
syntheseFermee[cle]=true;renderSynthBox();}

/* ─── PANNEAU DE LECTURE ─── */

async function ouvrirArticle(id){
articleSelectionne=id;render();
const a=articlesCache.find(x=>x.id===id);
if(!a)return;
const pane=document.getElementById('col-lecture');
pane.innerHTML=`<div class="lect-head"><h2>${esc(a.titre)}</h2>
<div class="lect-meta">${esc(a.flux)} — ${a.date?a.date.slice(0,16).replace('T',' '):''}</div>
<div class="lect-actions">
<a href="${esc(a.lien)}" target="_blank">🔗 Ouvrir l'original</a>
<button onclick="toggleLuLecture(${a.id})">${a.lu?'✓ Lu':'○ Non lu'}</button>
<button onclick="rechargerTexte(${a.id})">🔄 Recharger le texte intégral</button>
<a href="mailto:${encodeURIComponent(mailDefaut)}?subject=${encodeURIComponent(a.titre)}&body=${encodeURIComponent(a.titre+' — '+a.lien)}">✉️ Mail</a>
<a href="https://wa.me/?text=${encodeURIComponent(a.titre+' — '+a.lien)}" target="_blank">💬 WhatsApp</a>
<button onclick="navigator.clipboard.writeText('${esc(a.lien)}');this.textContent='✓ Copié'">📋 Copier le lien</button>
</div></div>
<div class="lect-corps" id="lect-corps"><p class="lect-loading">Récupération du texte intégral…</p></div>`;
const r=await fetch('/api/article/texte?id='+id);
const j=await r.json();
if(articleSelectionne!==id)return;
afficherTexte(j);}

function afficherTexte(j){
const corps=document.getElementById('lect-corps');
if(!j.ok){corps.innerHTML='<p>Erreur de récupération du texte.</p>';return;}
let out='';
if(j.source==='resume')out+='<p class="lect-fallback">'+esc(j.message)+'</p>';
const paras=(j.texte||'').split('\\n\\n').filter(Boolean);
out+=paras.length?paras.map(p=>'<p>'+esc(p)+'</p>').join(''):'<p>'+esc(j.texte||'')+'</p>';
corps.innerHTML=out;}

async function rechargerTexte(id){
const corps=document.getElementById('lect-corps');
corps.innerHTML='<p class="lect-loading">Nouvelle tentative de récupération…</p>';
const r=await fetch('/api/article/texte/recharger',{method:'POST',body:JSON.stringify({id:id})});
const j=await r.json();afficherTexte(j);}

async function toggleLuLecture(id){
const a=articlesCache.find(x=>x.id===id);if(!a)return;
await toggleLu(id,!a.lu);
ouvrirArticle(id);}

/* ─── RAFRAÎCHISSEMENT / ANALYSE ─── */

async function refresh(){show('Rafraîchissement en cours…');
const r=await fetch('/api/refresh',{method:'POST'});const j=await r.json();
const ligne=(j.erreurs?'⚠ ':'✓ ')+j.nouveaux+' nouveaux articles'
+(j.erreurs?' — '+j.erreurs+' flux en erreur':'');
show(ligne,j.rapport.join('\\n'));load();chargerBarreLaterale();}

async function analyse(){
show('🧠 Analyse en cours… quelques secondes à 1 minute selon le volume.');
const r=await fetch('/api/analyse?'+qs(),{method:'POST'});const j=await r.json();
if(j.ok){const[s,e]=bornes();const cle=s+'_'+e;delete syntheseRepliee[cle];delete syntheseFermee[cle];
show('✓ Synthèse générée.');load();}
else{show('⚠ '+j.message);}}

/* ─── OPML ─── */

async function upload(inp){
xmlEnAttente=await inp.files[0].text();
const r=await fetch('/api/opml/preview',{method:'POST',body:xmlEnAttente});
const j=await r.json();inp.value='';
afficherPreviewOpml(j);}

function afficherPreviewOpml(j){
const box=document.getElementById('opmlpreview');
let html=`<h3>📁 Import OPML — aperçu avant action</h3>`;
html+=`<p>${j.nouveaux.length} nouveau(x) flux seront ajoutés`
+(j.nouveaux.length?' : '+j.nouveaux.map(x=>esc(x.titre)).join(', '):'')+`.</p>`;
if(j.a_supprimer&&j.a_supprimer.length){
html+=`<p>${j.a_supprimer.length} flux déjà abonnés sont absents de ce fichier :</p>`;
html+=j.a_supprimer.map(x=>`<label style="display:block"><input type="checkbox" class="opmlsupchk" value="${x.id}" checked> ${esc(x.titre)} <span style="color:#999;font-size:.75rem">${esc(x.url)}</span></label>`).join('');
html+=`<div class="bar" style="margin-top:8px">
<button onclick="appliquerOpml('ajout')">➕ Ajouter seulement les nouveaux</button>
<button class="danger" onclick="appliquerOpml('sync')">🔄 Synchroniser (ajout + suppression cochée)</button>
<button onclick="annulerOpml()">Annuler</button></div>`;
}else{
html+=`<div class="bar" style="margin-top:8px">
<button onclick="appliquerOpml('ajout')">➕ Ajouter les nouveaux flux</button>
<button onclick="annulerOpml()">Annuler</button></div>`;
}
box.innerHTML=html;box.style.display='block';}

function annulerOpml(){document.getElementById('opmlpreview').style.display='none';xmlEnAttente='';}

async function appliquerOpml(mode){
let supprimerIds=[];let purge=false;
if(mode==='sync'){
supprimerIds=[...document.querySelectorAll('.opmlsupchk:checked')].map(c=>+c.value);
if(supprimerIds.length){
purge=confirm('Effacer AUSSI les articles archivés des flux supprimés ?\\nOK = effacer, Annuler = conserver');}}
const r=await fetch('/api/opml/appliquer',{method:'POST',
body:JSON.stringify({xml:xmlEnAttente,mode:mode,supprimer_ids:supprimerIds,purge:purge})});
const j=await r.json();
show(j.message);
document.getElementById('opmlpreview').style.display='none';
xmlEnAttente='';refreshFeeds();}

async function hook(){const r=await fetch('/api/webhook?'+qs(),{method:'POST'});
const j=await r.json();show(j.message);}

/* ─── PANNEAU « GÉRER LES FLUX » ─── */

function optionsDossiers(selectionne){
return '<option value="">— Sans dossier —</option>'+dossiersCache.map(d=>
`<option value="${d.id}" ${selectionne===d.id?'selected':''}>${esc(d.nom)}</option>`).join('');}

async function loadFeeds(){
const[rf,rd]=await Promise.all([fetch('/api/feeds'),fetch('/api/dossiers')]);
const f=await rf.json();dossiersCache=await rd.json();
document.getElementById('feedlist').innerHTML=f.length?f.map(x=>
`<div class="feedrow"><input type="checkbox" class="fdelchk" value="${x.id}">
<div class="finfo"><b>${esc(x.titre)}</b>${x.alerte?' <span class="badge" title="'+x.echecs+' échecs consécutifs">⚠️</span>':''}
· ${x.articles} art.
<div class="furl">${esc(x.url)}</div>
<div class="editbox" id="editbox${x.id}" style="display:none">
<input type="text" id="etitre${x.id}" value="${esc(x.titre)}" placeholder="Titre">
<select id="edossier${x.id}">${optionsDossiers(x.dossier_id)}</select>
<button onclick="saveEditFeed(${x.id})">💾</button>
<button onclick="toggleEdit(${x.id})">Annuler</button>
</div></div>
<button onclick="toggleEdit(${x.id})">✏️</button>
<button onclick="purgeFlux(${x.id},'${esc(x.titre).replace(/'/g,"\\\\'")}')">🧹 Vider</button>
<button onclick="delFeed(${x.id},'${esc(x.titre).replace(/'/g,"\\\\'")}')">🗑 Supprimer</button></div>`).join('')
:'<p>Aucun flux. Ajoutez-en un ci-dessous ou importez un OPML.</p>';
document.getElementById('fdossier').innerHTML=optionsDossiers(null);
document.getElementById('gndossier').innerHTML=optionsDossiers(null);}

async function refreshFeeds(){await loadFeeds();await chargerBarreLaterale();}

/* ─── GÉNÉRATEUR DE FLUX GOOGLE NEWS ─── */

function urlGoogleNews(query,langRegion){
const[hl,gl]=langRegion.split('|');
const ceid=gl+':'+hl.split('-')[0];
return `https://news.google.com/rss/search?q=${encodeURIComponent(query)}&hl=${hl}&gl=${gl}&ceid=${ceid}`;}

async function ajouterFluxGoogleNews(){
const q=document.getElementById('gnquery').value.trim();
if(!q){show('Entrez un mot-clé de recherche.');return;}
const url=urlGoogleNews(q,document.getElementById('gnlang').value);
const dv=document.getElementById('gndossier').value;
const r=await fetch('/api/feeds/add',{method:'POST',
body:JSON.stringify({url:url,titre:'Google News : '+q,dossier_id:dv||null})});
const j=await r.json();show(j.message);
if(j.ok){document.getElementById('gnquery').value='';refreshFeeds();}}

function toggleEdit(id){const b=document.getElementById('editbox'+id);
b.style.display=b.style.display==='none'?'flex':'none';}

async function saveEditFeed(id){
const titre=document.getElementById('etitre'+id).value;
const dv=document.getElementById('edossier'+id).value;
const r=await fetch('/api/feeds/edit',{method:'POST',
body:JSON.stringify({id:id,titre:titre,dossier_id:dv||null})});
const j=await r.json();show(j.message);refreshFeeds();}

async function delFeedsBulk(){
const ids=[...document.querySelectorAll('.fdelchk:checked')].map(c=>+c.value);
if(!ids.length){show('Aucun flux sélectionné.');return;}
if(!confirm('Supprimer les '+ids.length+' flux sélectionnés ?'))return;
const wipe=confirm('Effacer AUSSI leurs articles archivés ?\\nOK = effacer, Annuler = conserver');
const r=await fetch('/api/feeds/delete-bulk',{method:'POST',
body:JSON.stringify({ids:ids,purge:wipe})});const j=await r.json();
show(j.message);refreshFeeds();load();}

async function addFeed(){
const dv=document.getElementById('fdossier').value;
const body=JSON.stringify({url:document.getElementById('furl').value,
titre:document.getElementById('ftitre').value,dossier_id:dv||null});
const r=await fetch('/api/feeds/add',{method:'POST',body:body});const j=await r.json();
show(j.message);if(j.ok){document.getElementById('furl').value='';
document.getElementById('ftitre').value='';refreshFeeds();}}

async function delFeed(id,titre){
if(!confirm('Supprimer le flux « '+titre+' » ?'))return;
const wipe=confirm('Effacer AUSSI ses articles archivés ?\\nOK = effacer, Annuler = conserver');
const r=await fetch('/api/feeds/delete',{method:'POST',
body:JSON.stringify({id:id,purge:wipe})});const j=await r.json();
show(j.message);refreshFeeds();load();}

async function purgeFlux(id,titre){
if(!confirm('Vider tous les articles de « '+titre+' » ?\\n(Le flux reste abonné.)'))return;
const r=await fetch('/api/purge/flux',{method:'POST',
body:JSON.stringify({id:id})});const j=await r.json();
show(j.message);refreshFeeds();load();}

/* ─── PURGES / RÉGLAGES ─── */

async function purgePeriode(){const[s,e]=bornes();
const fids=feedIdsPourVue();
const portee=fids&&fids.length?' pour la sélection de flux/dossier en cours':'';
if(!confirm('Effacer tous les articles de la période '+s+' → '+e+portee+' ?'))return;
const r=await fetch('/api/purge/periode?'+qs(),{method:'POST'});const j=await r.json();
show(j.message);load();}

async function purgeTotale(){
if(!confirm('TOUT effacer : articles ET synthèses ?\\n(Les flux et réglages sont conservés.)'))return;
if(!confirm('Dernière confirmation : cette action est irréversible.'))return;
const r=await fetch('/api/purge/totale',{method:'POST'});const j=await r.json();
show(j.message);load();}

async function loadReglages(){const r=await fetch('/api/reglages');const j=await r.json();
document.getElementById('pana').value=j.prompt;
document.getElementById('rmail').value=j.mail;mailDefaut=j.mail;
document.getElementById('rmots').value=j.motscles;
motsCles=(j.motscles||'').split(',').map(s=>s.trim().toLowerCase()).filter(Boolean);}

async function saveMail(){
const r=await fetch('/api/reglages/mail',{method:'POST',
body:JSON.stringify({mail:document.getElementById('rmail').value})});
const j=await r.json();mailDefaut=document.getElementById('rmail').value;
show(j.message);load();}

async function saveMots(){
const r=await fetch('/api/reglages/motscles',{method:'POST',
body:JSON.stringify({motscles:document.getElementById('rmots').value})});
const j=await r.json();
motsCles=document.getElementById('rmots').value.split(',').map(s=>s.trim().toLowerCase()).filter(Boolean);
show(j.message);render();}

async function savePrompt(){
const r=await fetch('/api/reglages/prompt',{method:'POST',
body:JSON.stringify({prompt:document.getElementById('pana').value})});
const j=await r.json();show(j.message);}

async function resetPrompt(){
const r=await fetch('/api/reglages/prompt',{method:'POST',
body:JSON.stringify({prompt:''})});const j=await r.json();
show('Prompt par défaut rétabli.');loadReglages();}

/* ─── SÉPARATEUR AJUSTABLE (colonne centrale / panneau de lecture) ─── */
(function(){
const splitter=document.getElementById('splitter');
const colArticles=document.getElementById('col-articles');
const main=document.getElementById('main');
let dragging=false;
splitter.addEventListener('mousedown',e=>{dragging=true;splitter.classList.add('dragging');
document.body.style.cursor='col-resize';document.body.style.userSelect='none';e.preventDefault();});
document.addEventListener('mousemove',e=>{
if(!dragging)return;
const rect=colArticles.getBoundingClientRect();
const rectMain=main.getBoundingClientRect();
let w=e.clientX-rect.left;
w=Math.max(340,Math.min(w,rectMain.width-300));
colArticles.style.flex='0 0 '+w+'px';});
document.addEventListener('mouseup',()=>{if(!dragging)return;dragging=false;
splitter.classList.remove('dragging');document.body.style.cursor='';document.body.style.userSelect='';});
})();

(async()=>{
await chargerBarreLaterale();
const r=await fetch('/api/reglages');const j=await r.json();
mailDefaut=j.mail;
motsCles=(j.motscles||'').split(',').map(s=>s.trim().toLowerCase()).filter(Boolean);
load();
})();
</script></body></html>"""

# ==================== SERVEUR HTTP ====================

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, body, ctype="text/html; charset=utf-8", dl=None):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if dl: self.send_header("Content-Disposition", f'attachment; filename="{dl}"')
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _json(self, obj):
        self._send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")
    def _params(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        start = q.get("start", [None])[0]
        end = q.get("end", [None])[0]
        feeds_param = q.get("feeds", [None])[0]
        feed_ids = ([int(x) for x in feeds_param.split(",") if x.strip().isdigit()]
                    if feeds_param else None)
        article_id = q.get("id", [None])[0]
        return u, start, end, feed_ids, article_id

    @staticmethod
    def _dossier_id_de(d):
        v = d.get("dossier_id")
        if v in (None, "", "null"):
            return None
        try: return int(v)
        except (TypeError, ValueError): return None

    def do_GET(self):
        u, start, end, feed_ids, article_id = self._params()
        if u.path == "/": self._send(PAGE)
        elif u.path == "/api/articles":
            self._json(articles_periode(start, end, feed_ids))
        elif u.path == "/api/feeds":
            self._json(list_feeds())
        elif u.path == "/api/dossiers":
            self._json(list_dossiers())
        elif u.path == "/api/article/texte":
            self._json(obtenir_texte_article(int(article_id or 0)))
        elif u.path == "/api/reglages":
            self._json({"prompt": get_reglage("prompt_analyse", PROMPT_ANALYSE_DEFAUT),
                        "mail": get_reglage("mail_defaut", ""),
                        "motscles": get_reglage("mots_cles", "")})
        elif u.path == "/api/synthese":
            self._json({"texte": synthese_existante(start, end)})
        elif u.path == "/export/json":
            s, e = borne_periode(start, end)
            self._send(json.dumps(articles_periode(s, e, feed_ids), ensure_ascii=False, indent=2),
                       "application/json; charset=utf-8", f"articles_{s}_{e}.json")
        elif u.path == "/export/csv":
            s, e = borne_periode(start, end)
            buf = io.StringIO(); w = csv.writer(buf, delimiter=";")
            w.writerow(["titre","lien","resume","date","flux","lu"])
            for a in articles_periode(s, e, feed_ids):
                w.writerow([a["titre"],a["lien"],a["resume"],a["date"],a["flux"],
                           "oui" if a["lu"] else "non"])
            self._send("\ufeff"+buf.getvalue(), "text/csv; charset=utf-8",
                       f"articles_{s}_{e}.csv")
        elif u.path == "/export/md":
            md, libelle = synthese_markdown(start, end)
            if md is None:
                self._send("Aucune synthèse pour cette période. Lancez d'abord une analyse.",
                           "text/plain; charset=utf-8")
            else:
                s, e = borne_periode(start, end)
                self._send(md, "text/markdown; charset=utf-8", f"synthese_{s}_{e}.md")
        elif u.path == "/export/opml":
            self._send(export_opml(), "text/xml; charset=utf-8", "flux.opml")
        else: self.send_error(404)

    def do_POST(self):
        u, start, end, feed_ids, article_id = self._params()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        def jbody():
            try: return json.loads(body)
            except Exception: return {}
        if u.path == "/api/refresh":
            n, err, rep = refresh_all()
            self._json({"nouveaux": n, "erreurs": err, "rapport": rep})
        elif u.path == "/api/analyse":
            ok, msg = analyser_periode(start, end)
            self._json({"ok": ok, "message": msg if not ok else "OK"})
        elif u.path == "/api/feeds/add":
            d = jbody()
            ok, msg = add_feed(d.get("url",""), d.get("titre",""), self._dossier_id_de(d))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/feeds/edit":
            d = jbody()
            ok, msg = edit_feed(int(d.get("id", 0)), d.get("titre",""), self._dossier_id_de(d))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/feeds/delete":
            d = jbody()
            ok, msg = delete_feed(int(d.get("id", 0)), bool(d.get("purge", False)))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/feeds/delete-bulk":
            d = jbody()
            ok, msg = delete_feeds_bulk(d.get("ids", []), bool(d.get("purge", False)))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/feeds/reorder":
            d = jbody()
            reordonner_feeds(self._dossier_id_de(d), d.get("ids", []))
            self._json({"ok": True})
        elif u.path == "/api/dossiers/add":
            d = jbody()
            ok, msg, did = ajouter_dossier(d.get("nom",""))
            self._json({"ok": ok, "message": msg, "id": did})
        elif u.path == "/api/dossiers/rename":
            d = jbody()
            ok, msg = renommer_dossier(int(d.get("id", 0)), d.get("nom",""))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/dossiers/delete":
            d = jbody()
            ok, msg = supprimer_dossier(int(d.get("id", 0)))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/dossiers/reorder":
            d = jbody()
            reordonner_dossiers(d.get("ids", []))
            self._json({"ok": True})
        elif u.path == "/api/articles/lu":
            d = jbody()
            marquer_lu(int(d.get("id", 0)), bool(d.get("lu", False)))
            self._json({"ok": True})
        elif u.path == "/api/article/texte/recharger":
            d = jbody()
            self._json(obtenir_texte_article(int(d.get("id", 0)), forcer=True))
        elif u.path == "/api/opml/preview":
            try:
                nouveaux, a_supprimer = opml_preview(body)
            except Exception as e:
                self._json({"nouveaux": [], "a_supprimer": [], "erreur": str(e)}); return
            self._json({"nouveaux": nouveaux, "a_supprimer": a_supprimer})
        elif u.path == "/api/opml/appliquer":
            d = jbody()
            try:
                supp = d.get("supprimer_ids") if d.get("mode") == "sync" else None
                n, msg_sup = opml_appliquer(d.get("xml",""), supp, bool(d.get("purge", False)))
            except Exception as e:
                self._json({"ok": False, "message": f"Erreur d'import : {e}"}); return
            self._json({"ok": True, "message": f"{n} flux ajoutés{msg_sup}."})
        elif u.path == "/api/purge/flux":
            d = jbody()
            ok, msg = purge_flux(int(d.get("id", 0)))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/purge/periode":
            s, e = borne_periode(start, end)
            ok, msg = purge_periode(s, e, feed_ids)
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/purge/totale":
            ok, msg = purge_totale()
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/reglages/mail":
            set_reglage("mail_defaut", jbody().get("mail", "").strip())
            self._json({"ok": True, "message": "Destinataire par défaut enregistré."})
        elif u.path == "/api/reglages/motscles":
            set_reglage("mots_cles", jbody().get("motscles", "").strip())
            self._json({"ok": True, "message": "Mots-clés enregistrés."})
        elif u.path == "/api/reglages/prompt":
            set_reglage("prompt_analyse", jbody().get("prompt", ""))
            self._json({"ok": True, "message": "Prompt enregistré."})
        elif u.path == "/api/webhook":
            ok, msg = send_webhook(start, end)
            self._json({"ok": ok, "message": msg})
        else: self.send_error(404)

if __name__ == "__main__":
    db().close()
    print(f"RSSLocal v5 démarré → http://localhost:{PORT}  (Ctrl+C pour arrêter)")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
