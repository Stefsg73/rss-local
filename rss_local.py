# -*- coding: utf-8 -*-
"""
RSSLocal v4 — Lecteur/agrégateur RSS 100 % local + analyse IA simplifiée.
Aucun droit admin, aucune dépendance externe (stdlib Python uniquement).
Usage : python3 rss_local.py   puis ouvrir http://localhost:8765

Nouveautés v4 :
- Suppression de flux par lots (cases à cocher + bouton, avec la même
  double confirmation purge/conservation que la suppression unitaire)
- Import OPML avec aperçu avant action : choix entre ajout seul ou
  synchronisation complète (flux absents du fichier proposés à la
  suppression, avec écran de confirmation détaillé)
- Marquage lu / non-lu des articles (filtre dédié)
- Recherche plein texte (titre + résumé) sur la période affichée
- Filtre d'affichage et d'export par flux spécifiques (au lieu de tout/rien)
- Mots-clés à surveiller : surlignage des articles correspondants
- Édition en place d'un flux existant (titre, catégorie)
- Badge d'alerte ⚠️ sur les flux en échec de rafraîchissement récurrent

⚠️ IMPORTANT — changement de schéma de base de données :
Cette version ajoute des colonnes aux tables `feeds` et `articles`.
Supprimez (ou renommez) votre ancien rss_local.db avant le premier lancement
de cette V4 : il sera recréé automatiquement avec le nouveau schéma.

Nouveautés v3 (rappel) :
- Sélection de période : aujourd'hui / 7 jours / 30 jours / personnalisée
- Analyse simplifiée : un seul modèle (Sonnet), un seul prompt éditable
- Destinataire mail par défaut (réglage dans l'interface)
- Envoi de la synthèse par mail, export de la synthèse en .md
- Statut de rafraîchissement sur une ligne (détail dépliable)
- Purge automatique par ancienneté (CONSERVER_JOURS, articles + synthèses)
- Purges manuelles : totale, par média, par période
"""
import sqlite3, json, csv, io, re, threading, webbrowser, ssl, os
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
USER_AGENT = "Mozilla/5.0 (RSSLocal/4.0; lecteur personnel)"
TIMEOUT = 15
CONSERVER_JOURS = 14  # purge auto : articles et synthèses plus vieux supprimés
SEUIL_ECHECS_ALERTE = 3  # nombre d'échecs consécutifs avant badge ⚠️ sur un flux

# --- Analyse IA (laisser API_KEY vide pour désactiver) ---
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # ou collez la clé ici : "sk-ant-..."
MODELE_ANALYSE = "claude-sonnet-4-6"
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
    c.execute("""CREATE TABLE IF NOT EXISTS feeds(
        id INTEGER PRIMARY KEY, url TEXT UNIQUE, title TEXT, category TEXT DEFAULT '',
        echecs INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY, feed_id INTEGER, guid TEXT UNIQUE,
        title TEXT, link TEXT, summary TEXT, published TEXT, fetched TEXT,
        lu INTEGER DEFAULT 0)""")
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

def purge_periode(start, end):
    c = db()
    c.execute("""DELETE FROM articles WHERE substr(published,1,10) >= ?
                 AND substr(published,1,10) <= ?""", (start, end))
    n = c.execute("SELECT changes()").fetchone()[0]
    c.commit(); c.close()
    return True, f"{n} articles supprimés sur la période {start} → {end}."

# ==================== COLLECTE DES FLUX ====================

def parse_date(s):
    if not s: return None
    s = s.strip()
    try: return parsedate_to_datetime(s)
    except Exception: pass
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

def strip_html(t):
    return re.sub(r"<[^>]+>", " ", t or "").strip()[:600]

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

# ==================== GESTION DES FLUX ====================

def list_feeds():
    c = db()
    rows = c.execute("""SELECT f.id, f.title, f.url, f.category, f.echecs,
                        COUNT(a.id) FROM feeds f
                        LEFT JOIN articles a ON a.feed_id = f.id
                        GROUP BY f.id ORDER BY f.category, f.title""").fetchall()
    c.close()
    return [{"id": r[0], "titre": r[1], "url": r[2], "categorie": r[3],
             "echecs": r[4], "alerte": r[4] >= SEUIL_ECHECS_ALERTE,
             "articles": r[5]} for r in rows]

def add_feed(url, title="", category=""):
    if not url or not url.startswith(("http://", "https://")):
        return False, "URL invalide (elle doit commencer par http:// ou https://)."
    c = db()
    c.execute("INSERT OR IGNORE INTO feeds(url, title, category) VALUES(?,?,?)",
              (url.strip(), (title or url).strip(), category.strip()))
    n = c.execute("SELECT changes()").fetchone()[0]
    c.commit(); c.close()
    return (True, "Flux ajouté.") if n else (False, "Ce flux existe déjà.")

def edit_feed(feed_id, titre, categorie):
    c = db()
    row = c.execute("SELECT title FROM feeds WHERE id=?", (feed_id,)).fetchone()
    if not row:
        c.close(); return False, "Flux introuvable."
    nouveau_titre = (titre or "").strip() or row[0]
    c.execute("UPDATE feeds SET title=?, category=? WHERE id=?",
              (nouveau_titre, (categorie or "").strip(), feed_id))
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

# ==================== OPML ====================

def normalize_url(u):
    """Normalise une URL pour comparaison (schéma, casse, barre finale)
    afin d'éviter les faux positifs lors d'une synchronisation OPML."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    return u.rstrip("/")

def opml_outlines(xml_text):
    """Retourne une liste de (url, titre, catégorie) à partir d'un texte OPML."""
    root = ET.fromstring(xml_text)
    out = []
    def walk(node, cat=""):
        for o in node.findall("outline"):
            url = o.get("xmlUrl")
            if url:
                out.append((url, o.get("title") or o.get("text") or url, cat))
            else:
                walk(o, o.get("title") or o.get("text") or cat)
    body = root.find("body")
    if body is not None: walk(body)
    return out

def import_opml(xml_text):
    outlines = opml_outlines(xml_text)
    c = db(); n = 0
    for url, titre, cat in outlines:
        c.execute("INSERT OR IGNORE INTO feeds(url,title,category) VALUES(?,?,?)",
                  (url, titre, cat))
        n += c.execute("SELECT changes()").fetchone()[0]
    c.commit(); c.close()
    return n

def opml_preview(xml_text):
    """Prévisualise l'effet d'un import OPML sans toucher à la base :
    renvoie les nouveaux flux à ajouter et les flux existants absents
    du fichier (candidats à la suppression en mode synchronisation)."""
    outlines = opml_outlines(xml_text)
    urls_fichier = {normalize_url(u) for u, t, cat in outlines}
    c = db()
    existants = c.execute("SELECT id, title, url, category FROM feeds").fetchall()
    c.close()
    urls_existantes = {normalize_url(u) for _, _, u, _ in existants}
    nouveaux = [{"url": u, "titre": t, "categorie": cat} for u, t, cat in outlines
                if normalize_url(u) not in urls_existantes]
    a_supprimer = [{"id": r[0], "titre": r[1], "url": r[2], "categorie": r[3]}
                   for r in existants if normalize_url(r[2]) not in urls_fichier]
    return nouveaux, a_supprimer

def opml_appliquer(xml_text, supprimer_ids=None, purge=False):
    """Applique un import OPML : ajoute toujours les nouveaux flux, et
    supprime en plus les flux listés dans supprimer_ids (mode synchronisation)."""
    n = import_opml(xml_text)
    msg_sup = ""
    if supprimer_ids:
        ok, msg = delete_feeds_bulk(supprimer_ids, purge)
        if ok: msg_sup = " — " + msg
    return n, msg_sup

def export_opml():
    c = db()
    feeds = c.execute("SELECT title, url, category FROM feeds ORDER BY category, title").fetchall()
    c.close()
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<opml version="2.0"><head><title>RSSLocal export</title></head><body>']
    for t, u, cat in feeds:
        t = (t or "").replace('"', "'"); cat = (cat or "").replace('"', "'")
        lines.append(f'<outline text="{t}" title="{t}" type="rss" xmlUrl="{u}" category="{cat}"/>')
    lines.append("</body></opml>")
    return "\n".join(lines)

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
    q = """SELECT a.id, a.title, a.link, a.summary, a.published, f.title, f.category, a.lu
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
             "flux": r[5], "categorie": r[6], "lu": bool(r[7])} for r in rows]

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
body{font-family:system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px;background:#f7f7f5;color:#222}
h1{font-size:1.4rem} .bar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0;align-items:center}
button,a.btn,label.btn{padding:8px 14px;border:1px solid #bbb;border-radius:8px;background:#fff;cursor:pointer;text-decoration:none;color:#222;font-size:.9rem}
button:hover,a.btn:hover,label.btn:hover{background:#eee}
button.ia{background:#4a3b8f;color:#fff;border-color:#4a3b8f}button.ia:hover{background:#5c4bb0}
button.danger{border-color:#c0392b;color:#c0392b}button.danger:hover{background:#fdf0ee}
select,input[type=date],input[type=text],input[type=email]{padding:6px;border-radius:8px;border:1px solid #bbb;font-size:.9rem}
.art{background:#fff;border:1px solid #e2e2e0;border-radius:10px;padding:12px 16px;margin:10px 0}
.art h3{margin:0 0 4px;font-size:1.02rem}.art .meta{color:#777;font-size:.8rem}
.art p{margin:6px 0 0;font-size:.9rem;color:#444}
.art.lu{opacity:.5}
.art.surligne{border-left:4px solid #e6b800;background:#fffdf2}
.share{margin-top:6px;display:flex;gap:6px}
.share a,.share button{padding:3px 8px;font-size:.8rem;border:1px solid #ddd;border-radius:6px;background:#fafafa;cursor:pointer;text-decoration:none;color:#333}
.luBtn{margin-left:8px;padding:2px 8px;font-size:.75rem;border:1px solid #ccc;border-radius:6px;background:#fafafa;cursor:pointer}
#status{font-size:.85rem;color:#333;background:#eee;padding:8px 12px;border-radius:8px;display:none;margin:8px 0}
#status details{margin-top:6px}#status summary{cursor:pointer;color:#666;font-size:.8rem}
#status pre{white-space:pre-wrap;font-size:.78rem;color:#555;margin:6px 0 0}
details.media{background:#fff;border:1px solid #e2e2e0;border-radius:10px;margin:8px 0;padding:4px 12px}
details.media summary{cursor:pointer;font-weight:600;padding:8px 0}
details.media .art{border:none;border-top:1px solid #eee;border-radius:0;margin:0}
#synthbox{background:#fffbe8;border:1px solid #e6d98a;border-radius:10px;margin:12px 0;display:none}
#synthhead{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;cursor:pointer;font-weight:600}
#synthbody{white-space:pre-wrap;padding:0 16px 12px;font-size:.92rem}
#synthactions{padding:0 16px 14px;display:flex;gap:8px}
#synthactions a,#synthactions button{padding:4px 10px;font-size:.8rem;border:1px solid #d8c96a;border-radius:6px;background:#fff;cursor:pointer;text-decoration:none;color:#333}
#synthclose{border:none;background:none;font-size:1.1rem;cursor:pointer;color:#8a7b2e}
.panel{background:#fff;border:1px solid #ccc;border-radius:10px;padding:16px;margin:12px 0;display:none}
.panel h2{font-size:1.05rem;margin:0 0 10px}
.panel h3{font-size:.98rem;margin:10px 0 6px}
.feedrow{display:flex;justify-content:space-between;align-items:flex-start;padding:6px 0;border-bottom:1px solid #eee;font-size:.9rem;gap:8px}
.feedrow .finfo{flex:1;min-width:0}.feedrow .furl{color:#999;font-size:.75rem;word-break:break-all}
.feedrow button{padding:4px 10px;font-size:.8rem;white-space:nowrap}
.feedrow input[type=checkbox]{margin-top:4px}
.editbox{display:flex;gap:6px;flex-wrap:wrap}
.editbox input{flex:1;min-width:100px}
.badge{cursor:help}
textarea{width:100%;box-sizing:border-box;min-height:110px;border:1px solid #bbb;border-radius:8px;padding:8px;font-size:.85rem;font-family:inherit}
.addrow{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.addrow input{flex:1;min-width:120px}
#customdates{display:none;gap:6px;align-items:center}
.reglrow{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}
#filtrefluxlist label{display:block;padding:2px 0;font-size:.9rem}
#opmlpreview{display:none}
</style></head><body>
<h1>📰 RSSLocal v4 — agrégateur local</h1>
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
<input type="text" id="recherche" placeholder="🔎 Rechercher…" oninput="render()" style="min-width:140px">
<button class="ia" onclick="analyse()">🧠 Analyser la période</button>
</div>
<div class="bar">
<button onclick="toggle('pfeeds');loadFeeds()">⚙️ Flux</button>
<button onclick="toggle('preglages');loadReglages()">🛠 Réglages</button>
<button onclick="toggle('pfiltreflux');loadFiltreFlux()">🔍 Filtrer par flux</button>
<a class="btn" id="ejson" href="#">⬇ JSON</a>
<a class="btn" id="ecsv" href="#">⬇ CSV</a>
<a class="btn" href="/export/opml">⬇ OPML</a>
<button onclick="hook()">📤 n8n</button>
<label class="btn">📁 Importer OPML<input type="file" id="opml" style="display:none" onchange="upload(this)"></label>
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
<input type="text" id="fcat" placeholder="Catégorie (optionnel)">
<button onclick="addFeed()">➕ Ajouter</button>
</div>
</div>

<div class="panel" id="pfiltreflux">
<h2>🔍 Filtrer l'affichage et les exports par flux</h2>
<p style="font-size:.85rem;color:#666;margin-top:0">Aucune case cochée = tous les flux affichés et exportés.</p>
<div class="bar">
<button onclick="filtreFluxTout(true)">Tout cocher</button>
<button onclick="filtreFluxTout(false)">Tout décocher</button>
</div>
<div id="filtrefluxlist"></div>
</div>

<div class="panel" id="preglages">
<h2>🛠 Réglages</h2>
<div class="reglrow"><label>✉️ Destinataire mail par défaut :</label>
<input type="email" id="rmail" placeholder="prenom.nom@exemple.fr" style="flex:1;min-width:200px">
<button onclick="saveMail()">💾</button></div>
<div class="reglrow"><label>🔎 Mots-clés à surveiller (séparés par des virgules) :</label>
<input type="text" id="rmots" placeholder="ex: budget, grève, élection" style="flex:1;min-width:200px">
<button onclick="saveMots()">💾</button></div>
<p style="font-size:.8rem;color:#888;margin-top:-4px">Les articles correspondants sont surlignés dans la liste (aucune notification).</p>
<p style="font-size:.85rem;color:#666;margin-bottom:4px">🧠 Prompt d'analyse (envoyé à Sonnet avec la liste des articles) :</p>
<textarea id="pana"></textarea>
<div class="bar">
<button onclick="savePrompt()">💾 Enregistrer le prompt</button>
<button onclick="resetPrompt()">↩️ Prompt par défaut</button>
</div>
<h2 style="margin-top:18px">🗑 Purges</h2>
<div class="bar">
<button class="danger" onclick="purgePeriode()">Effacer les articles de la période affichée</button>
<button class="danger" onclick="purgeTotale()">Tout effacer (articles + synthèses)</button>
</div>
<p style="font-size:.8rem;color:#888">Purge automatique : les articles et synthèses de plus de
<b>CONSERVER_JOURS</b> jours (réglé dans le script) sont supprimés à chaque rafraîchissement.</p>
</div>

<div id="synthbox">
<div id="synthhead" onclick="foldSynth()"><span id="synthtitle">🧠 Synthèse</span>
<button id="synthclose" onclick="event.stopPropagation();closeSynth()">✕</button></div>
<div id="synthbody"></div>
<div id="synthactions">
<a id="smd" href="#">⬇ Export .md</a>
<a id="smail" href="#">✉️ Envoyer par mail</a>
</div>
</div>
<div id="list"></div>

<script>
let synthHidden={};let mailDefaut='';let motsCles=[];let articlesCache=[];
let fluxFiltre=new Set();let xmlEnAttente='';
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
function qs(){const[s,e]=bornes();let q='start='+s+'&end='+e;
if(fluxFiltre.size)q+='&feeds='+[...fluxFiltre].join(',');
return q;}
function links(){document.getElementById('ejson').href='/export/json?'+qs();
document.getElementById('ecsv').href='/export/csv?'+qs();
document.getElementById('smd').href='/export/md?'+qs();}
function toggle(id){const p=document.getElementById(id);
p.style.display=p.style.display==='block'?'none':'block';}
function show(t,detail){const s=document.getElementById('status');s.style.display='block';
s.innerHTML=esc(t)+(detail?'<details><summary>voir le détail</summary><pre>'+esc(detail)+'</pre></details>':'');}

function artHTML(x){
const txt=encodeURIComponent(x.titre+' — '+x.lien);
const mail='mailto:'+encodeURIComponent(mailDefaut)+'?subject='+encodeURIComponent(x.titre)+'&body='+txt;
const wa='https://wa.me/?text='+txt;
const cible=(x.titre+' '+x.resume).toLowerCase();
const surligne=motsCles.length&&motsCles.some(k=>cible.includes(k));
return `<div class="art ${x.lu?'lu':''} ${surligne?'surligne':''}">
<h3><a href="${esc(x.lien)}" target="_blank">${esc(x.titre)}</a></h3>
<div class="meta">${esc(x.flux)} — ${x.date?x.date.slice(0,16).replace('T',' '):''}
<button class="luBtn" onclick="toggleLu(${x.id},${!x.lu})">${x.lu?'✓ Lu':'○ Non lu'}</button></div>
<p>${esc(x.resume)}</p>
<div class="share"><a href="${mail}">✉️ Mail</a>
<a href="${wa}" target="_blank">💬 WhatsApp</a>
<button onclick="navigator.clipboard.writeText('${esc(x.lien)}');this.textContent='✓ Copié'">📋 Copier</button></div></div>`;}

function render(){
let a=articlesCache.slice();
const mode=document.getElementById('tri').value;
const q=(document.getElementById('recherche').value||'').toLowerCase().trim();
const luF=document.getElementById('lufiltre').value;
if(q)a=a.filter(x=>(x.titre+' '+x.resume).toLowerCase().includes(q));
if(luF==='nonlu')a=a.filter(x=>!x.lu);
if(luF==='lu')a=a.filter(x=>x.lu);
const list=document.getElementById('list');
if(!a.length){list.innerHTML='<p>Aucun article ne correspond à ces critères.</p>';return;}
if(mode==='media'){
const g={};a.forEach(x=>{(g[x.flux]=g[x.flux]||[]).push(x);});
list.innerHTML=Object.keys(g).sort().map(m=>
`<details class="media" open><summary>${esc(m)} (${g[m].length})</summary>
${g[m].map(artHTML).join('')}</details>`).join('');}
else{if(mode==='asc')a=a.slice().reverse();
list.innerHTML=a.map(artHTML).join('');}}

async function toggleLu(id,lu){
await fetch('/api/articles/lu',{method:'POST',body:JSON.stringify({id:id,lu:lu})});
const idx=articlesCache.findIndex(x=>x.id===id);if(idx>=0)articlesCache[idx].lu=lu;
render();}

async function load(){links();
const r=await fetch('/api/articles?'+qs());articlesCache=await r.json();
render();
const[s,e]=bornes();
const sr=await fetch('/api/synthese?'+qs());const js=await sr.json();
const box=document.getElementById('synthbox');const cle=s+'_'+e;
if(js.texte&&!synthHidden[cle]){box.style.display='block';
document.getElementById('synthtitle').textContent='🧠 Synthèse '+(s===e?('du '+s):('du '+s+' au '+e))+' (cliquer pour plier/déplier)';
document.getElementById('synthbody').textContent=js.texte;
const body=encodeURIComponent(js.texte.slice(0,1800));
document.getElementById('smail').href='mailto:'+encodeURIComponent(mailDefaut)
+'?subject='+encodeURIComponent('Synthèse RSSLocal — '+(s===e?s:s+' au '+e))
+'&body='+body;}
else{box.style.display='none';}}

function foldSynth(){const b=document.getElementById('synthbody');
b.style.display=b.style.display==='none'?'block':'none';}
function closeSynth(){const[s,e]=bornes();synthHidden[s+'_'+e]=true;
document.getElementById('synthbox').style.display='none';}

async function refresh(){show('Rafraîchissement en cours…');
const r=await fetch('/api/refresh',{method:'POST'});const j=await r.json();
const ligne=(j.erreurs?'⚠ ':'✓ ')+j.nouveaux+' nouveaux articles'
+(j.erreurs?' — '+j.erreurs+' flux en erreur':'');
show(ligne,j.rapport.join('\\n'));load();}

async function analyse(){
show('🧠 Analyse en cours (Sonnet)… jusqu\\'à 2 minutes selon le volume.');
const r=await fetch('/api/analyse?'+qs(),{method:'POST'});const j=await r.json();
if(j.ok){const[s,e]=bornes();synthHidden[s+'_'+e]=false;
show('✓ Synthèse générée.');load();}
else{show('⚠ '+j.message);}}

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
html+=j.a_supprimer.map(x=>`<label style="display:block"><input type="checkbox" class="opmlsupchk" value="${x.id}" checked> ${esc(x.titre)} <span class="furl">${esc(x.url)}</span></label>`).join('');
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
xmlEnAttente='';loadFeeds();load();}

async function hook(){const r=await fetch('/api/webhook?'+qs(),{method:'POST'});
const j=await r.json();show(j.message);}

async function loadFeeds(){const r=await fetch('/api/feeds');const f=await r.json();
document.getElementById('feedlist').innerHTML=f.length?f.map(x=>
`<div class="feedrow"><input type="checkbox" class="fdelchk" value="${x.id}">
<div class="finfo"><b>${esc(x.titre)}</b>${x.alerte?' <span class="badge" title="'+x.echecs+' échecs consécutifs">⚠️</span>':''}
${x.categorie?' · '+esc(x.categorie):''} · ${x.articles} art.
<div class="furl">${esc(x.url)}</div>
<div class="editbox" id="editbox${x.id}" style="display:none;margin-top:6px">
<input type="text" id="etitre${x.id}" value="${esc(x.titre)}" placeholder="Titre">
<input type="text" id="ecat${x.id}" value="${esc(x.categorie||'')}" placeholder="Catégorie">
<button onclick="saveEditFeed(${x.id})">💾</button>
<button onclick="toggleEdit(${x.id})">Annuler</button>
</div></div>
<button onclick="toggleEdit(${x.id})">✏️</button>
<button onclick="purgeFlux(${x.id},'${esc(x.titre).replace(/'/g,"\\\\'")}')">🧹 Vider</button>
<button onclick="delFeed(${x.id},'${esc(x.titre).replace(/'/g,"\\\\'")}')">🗑 Supprimer</button></div>`).join('')
:'<p>Aucun flux. Ajoutez-en un ci-dessous ou importez un OPML.</p>';}

function toggleEdit(id){const b=document.getElementById('editbox'+id);
b.style.display=b.style.display==='none'?'block':'none';}

async function saveEditFeed(id){
const titre=document.getElementById('etitre'+id).value;
const cat=document.getElementById('ecat'+id).value;
const r=await fetch('/api/feeds/edit',{method:'POST',
body:JSON.stringify({id:id,titre:titre,categorie:cat})});
const j=await r.json();show(j.message);loadFeeds();}

async function delFeedsBulk(){
const ids=[...document.querySelectorAll('.fdelchk:checked')].map(c=>+c.value);
if(!ids.length){show('Aucun flux sélectionné.');return;}
if(!confirm('Supprimer les '+ids.length+' flux sélectionnés ?'))return;
const wipe=confirm('Effacer AUSSI leurs articles archivés ?\\nOK = effacer, Annuler = conserver');
const r=await fetch('/api/feeds/delete-bulk',{method:'POST',
body:JSON.stringify({ids:ids,purge:wipe})});const j=await r.json();
show(j.message);loadFeeds();load();}

async function addFeed(){
const body=JSON.stringify({url:document.getElementById('furl').value,
titre:document.getElementById('ftitre').value,
categorie:document.getElementById('fcat').value});
const r=await fetch('/api/feeds/add',{method:'POST',body:body});const j=await r.json();
show(j.message);if(j.ok){document.getElementById('furl').value='';
document.getElementById('ftitre').value='';document.getElementById('fcat').value='';loadFeeds();}}

async function delFeed(id,titre){
if(!confirm('Supprimer le flux « '+titre+' » ?'))return;
const wipe=confirm('Effacer AUSSI ses articles archivés ?\\nOK = effacer, Annuler = conserver');
const r=await fetch('/api/feeds/delete',{method:'POST',
body:JSON.stringify({id:id,purge:wipe})});const j=await r.json();
show(j.message);loadFeeds();load();}

async function purgeFlux(id,titre){
if(!confirm('Vider tous les articles de « '+titre+' » ?\\n(Le flux reste abonné.)'))return;
const r=await fetch('/api/purge/flux',{method:'POST',
body:JSON.stringify({id:id})});const j=await r.json();
show(j.message);loadFeeds();load();}

async function loadFiltreFlux(){const r=await fetch('/api/feeds');const f=await r.json();
document.getElementById('filtrefluxlist').innerHTML=f.map(x=>
`<label><input type="checkbox" class="fltchk" value="${x.id}" ${fluxFiltre.has(x.id)?'checked':''} onchange="toggleFiltreFlux(${x.id},this.checked)"> ${esc(x.titre)}${x.categorie?' · '+esc(x.categorie):''}</label>`).join('');}

function toggleFiltreFlux(id,checked){
if(checked)fluxFiltre.add(id);else fluxFiltre.delete(id);
load();}

function filtreFluxTout(sel){
document.querySelectorAll('.fltchk').forEach(cb=>{cb.checked=sel;
if(sel)fluxFiltre.add(+cb.value);else fluxFiltre.delete(+cb.value);});
load();}

async function purgePeriode(){const[s,e]=bornes();
if(!confirm('Effacer tous les articles de la période '+s+' → '+e+' ?'))return;
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

(async()=>{const r=await fetch('/api/reglages');const j=await r.json();
mailDefaut=j.mail;
motsCles=(j.motscles||'').split(',').map(s=>s.trim().toLowerCase()).filter(Boolean);
load();})();
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
        return u, start, end, feed_ids

    def do_GET(self):
        u, start, end, feed_ids = self._params()
        if u.path == "/": self._send(PAGE)
        elif u.path == "/api/articles":
            self._json(articles_periode(start, end, feed_ids))
        elif u.path == "/api/feeds":
            self._json(list_feeds())
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
            w.writerow(["titre","lien","resume","date","flux","categorie","lu"])
            for a in articles_periode(s, e, feed_ids):
                w.writerow([a["titre"],a["lien"],a["resume"],a["date"],a["flux"],a["categorie"],
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
        u, start, end, feed_ids = self._params()
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
            ok, msg = add_feed(d.get("url",""), d.get("titre",""), d.get("categorie",""))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/feeds/edit":
            d = jbody()
            ok, msg = edit_feed(int(d.get("id", 0)), d.get("titre",""), d.get("categorie",""))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/feeds/delete":
            d = jbody()
            ok, msg = delete_feed(int(d.get("id", 0)), bool(d.get("purge", False)))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/feeds/delete-bulk":
            d = jbody()
            ok, msg = delete_feeds_bulk(d.get("ids", []), bool(d.get("purge", False)))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/articles/lu":
            d = jbody()
            marquer_lu(int(d.get("id", 0)), bool(d.get("lu", False)))
            self._json({"ok": True})
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
            ok, msg = purge_periode(s, e)
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
    print(f"RSSLocal v4 démarré → http://localhost:{PORT}  (Ctrl+C pour arrêter)")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
