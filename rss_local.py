# -*- coding: utf-8 -*-
"""
<<<<<<< HEAD
RSSLocal v2 — Lecteur/agrégateur RSS 100 % local + analyse IA en entonnoir.
Aucun droit admin, aucune dépendance externe (stdlib Python uniquement).
Usage : python3 rss_local.py   puis ouvrir http://localhost:8765

Nouveautés v2 :
- Gestion des flux dans l'interface (ajout unitaire, suppression, purge optionnelle)
- Tri des articles (date ↓/↑) et vue dépliante par média
- Partage d'article : mail, WhatsApp, copie du lien
- Synthèse éditoriale repliable/masquable
- Prompts d'analyse affichables et modifiables (sauvegardés en base)
- Correctif SSL macOS intégré
=======
RSSLocal v3 — Lecteur/agrégateur RSS 100 % local + analyse IA simplifiée.
Aucun droit admin, aucune dépendance externe (stdlib Python uniquement).
Usage : python3 rss_local.py   puis ouvrir http://localhost:8765

Nouveautés v3 :
- Sélection de période : aujourd'hui / 7 jours / 30 jours / personnalisée
- Analyse simplifiée : un seul modèle (Sonnet), un seul prompt éditable
- Destinataire mail par défaut (réglage dans l'interface)
- Envoi de la synthèse par mail, export de la synthèse en .md
- Statut de rafraîchissement sur une ligne (détail dépliable)
- Purge automatique par ancienneté (CONSERVER_JOURS, articles + synthèses)
- Purges manuelles : totale, par média, par période
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
"""
import sqlite3, json, csv, io, re, threading, webbrowser, ssl, os
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
<<<<<<< HEAD
from datetime import datetime, timezone
=======
from datetime import datetime, timezone, timedelta
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

# ---------- CONFIGURATION ----------
PORT = 8765
DB_PATH = "rss_local.db"
WEBHOOK_N8N = ""     # ex: "http://localhost:5678/webhook/rss" — vide si inutilisé
<<<<<<< HEAD
USER_AGENT = "Mozilla/5.0 (RSSLocal/2.0; lecteur personnel)"
TIMEOUT = 15

# --- Analyse IA (laisser API_KEY vide pour désactiver) ---
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # ou collez la clé ici : "sk-ant-..."
MODELE_TRI = "claude-haiku-4-5-20251001"
MODELE_SYNTHESE = "claude-sonnet-4-6"
TAILLE_LOT = 20
# -----------------------------------

# ---------- CONTEXTE SSL (correctif macOS) ----------
def make_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for path in ("/etc/ssl/cert.pem",
                 "/etc/ssl/certs/ca-certificates.crt",
                 "/private/etc/ssl/cert.pem"):
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()

SSL_CTX = make_ssl_context()
# ----------------------------------------------------

# ---------- PROMPTS PAR DÉFAUT ----------
PROMPT_TRI_DEFAUT = (
    "Tu es un assistant de veille pour un journaliste. "
    "Classe les articles suivants par grand thème (politique, économie, "
    "santé, culture, sport, autre). Pour chaque thème, liste les titres "
    "et signale d'un ⭐ les 2-3 articles les plus notables. "
    "Sois concis, pas de commentaire superflu.")

PROMPT_SYNTHESE_DEFAUT = (
    "Tu es l'assistant de veille d'un journaliste généraliste. "
    "Voici les tris thématiques des articles du jour, réalisés par lots. "
    "Rédige une synthèse éditoriale structurée : "
    "1) les 5 faits marquants du jour, 2) un panorama par thème, "
    "3) trois angles d'articles possibles. Style factuel et neutre.")
# ----------------------------------------
=======
USER_AGENT = "Mozilla/5.0 (RSSLocal/3.0; lecteur personnel)"
TIMEOUT = 15
CONSERVER_JOURS = 14  # purge auto : articles et synthèses plus vieux supprimés

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
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)

# ==================== BASE DE DONNÉES ====================

def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS feeds(
        id INTEGER PRIMARY KEY, url TEXT UNIQUE, title TEXT, category TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY, feed_id INTEGER, guid TEXT UNIQUE,
        title TEXT, link TEXT, summary TEXT, published TEXT, fetched TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS syntheses(
        id INTEGER PRIMARY KEY, jour TEXT UNIQUE, texte TEXT, cree TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reglages(
        cle TEXT PRIMARY KEY, valeur TEXT)""")
    return c

<<<<<<< HEAD
def get_reglage(cle, defaut):
=======
def get_reglage(cle, defaut=""):
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
    c = db()
    row = c.execute("SELECT valeur FROM reglages WHERE cle=?", (cle,)).fetchone()
    c.close()
    return row[0] if row and row[0].strip() else defaut

def set_reglage(cle, valeur):
    c = db()
    c.execute("INSERT OR REPLACE INTO reglages(cle, valeur) VALUES(?,?)", (cle, valeur))
    c.commit(); c.close()

<<<<<<< HEAD
=======
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

>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
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
<<<<<<< HEAD

=======
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
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
<<<<<<< HEAD
=======
    purge_auto()  # nettoyage silencieux des articles/synthèses trop vieux
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
    c = db()
    feeds = c.execute("SELECT id, url, title FROM feeds").fetchall()
    def work(f):
        fid, url, ftitle = f
        items, err = fetch_feed(fid, url)
        return fid, ftitle or url, items, err
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, feeds))
    now = datetime.now(timezone.utc).isoformat()
<<<<<<< HEAD
    new_count, report = 0, []
    for fid, ftitle, items, err in results:
        if err:
=======
    new_count, report, erreurs = 0, [], 0
    for fid, ftitle, items, err in results:
        if err:
            erreurs += 1
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
            report.append(f"⚠ {ftitle} : {err}"); continue
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
<<<<<<< HEAD
    return new_count, report
=======
    return new_count, erreurs, report
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)

# ==================== GESTION DES FLUX ====================

def list_feeds():
    c = db()
    rows = c.execute("""SELECT f.id, f.title, f.url, f.category,
                        COUNT(a.id) FROM feeds f
                        LEFT JOIN articles a ON a.feed_id = f.id
                        GROUP BY f.id ORDER BY f.category, f.title""").fetchall()
    c.close()
    return [{"id": r[0], "titre": r[1], "url": r[2],
             "categorie": r[3], "articles": r[4]} for r in rows]

def add_feed(url, title="", category=""):
    if not url or not url.startswith(("http://", "https://")):
        return False, "URL invalide (elle doit commencer par http:// ou https://)."
    c = db()
    c.execute("INSERT OR IGNORE INTO feeds(url, title, category) VALUES(?,?,?)",
              (url.strip(), (title or url).strip(), category.strip()))
    n = c.execute("SELECT changes()").fetchone()[0]
    c.commit(); c.close()
    return (True, "Flux ajouté.") if n else (False, "Ce flux existe déjà.")

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

# ==================== OPML ====================

def import_opml(xml_text):
    root = ET.fromstring(xml_text)
    c = db(); n = 0
    def walk(node, cat=""):
        nonlocal n
        for o in node.findall("outline"):
            url = o.get("xmlUrl")
            if url:
                c.execute("INSERT OR IGNORE INTO feeds(url,title,category) VALUES(?,?,?)",
                          (url, o.get("title") or o.get("text") or url, cat))
                n += c.execute("SELECT changes()").fetchone()[0]
            else:
                walk(o, o.get("title") or o.get("text") or cat)
    body = root.find("body")
    if body is not None: walk(body)
    c.commit(); c.close()
    return n

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

<<<<<<< HEAD
# ==================== EXPORTS / WEBHOOK ====================

def articles_of_day(day=None):
    day = day or datetime.now().strftime("%Y-%m-%d")
    c = db()
    rows = c.execute("""SELECT a.title, a.link, a.summary, a.published, f.title, f.category
           FROM articles a JOIN feeds f ON f.id=a.feed_id
           WHERE substr(a.published,1,10)=?
           ORDER BY a.published DESC""", (day,)).fetchall()
=======
# ==================== PÉRIODES / EXPORTS / WEBHOOK ====================

def borne_periode(start, end):
    """Normalise les bornes ; par défaut : aujourd'hui."""
    today = datetime.now().strftime("%Y-%m-%d")
    start = start or today
    end = end or start
    if start > end: start, end = end, start
    return start, end

def articles_periode(start=None, end=None):
    start, end = borne_periode(start, end)
    c = db()
    rows = c.execute("""SELECT a.title, a.link, a.summary, a.published, f.title, f.category
           FROM articles a JOIN feeds f ON f.id=a.feed_id
           WHERE substr(a.published,1,10) >= ? AND substr(a.published,1,10) <= ?
           ORDER BY a.published DESC""", (start, end)).fetchall()
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
    c.close()
    return [{"titre": r[0], "lien": r[1], "resume": r[2], "date": r[3],
             "flux": r[4], "categorie": r[5]} for r in rows]

<<<<<<< HEAD
def send_webhook(day=None):
    if not WEBHOOK_N8N:
        return False, "Aucune URL de webhook configurée (variable WEBHOOK_N8N)."
    payload = json.dumps({"jour": day or datetime.now().strftime("%Y-%m-%d"),
                          "articles": articles_of_day(day)}, ensure_ascii=False).encode("utf-8")
=======
def send_webhook(start=None, end=None):
    if not WEBHOOK_N8N:
        return False, "Aucune URL de webhook configurée (variable WEBHOOK_N8N)."
    start, end = borne_periode(start, end)
    payload = json.dumps({"periode": f"{start}_{end}",
                          "articles": articles_periode(start, end)},
                         ensure_ascii=False).encode("utf-8")
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
    req = urllib.request.Request(WEBHOOK_N8N, data=payload,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            return True, f"Webhook OK (HTTP {r.status})"
    except Exception as e:
        return False, f"Échec webhook: {e}"

<<<<<<< HEAD
# ==================== ANALYSE IA EN ENTONNOIR ====================

def appel_claude(modele, prompt, max_tokens=1024):
=======
# ==================== ANALYSE IA (Sonnet seul, prompt unique) ====================

def appel_claude(modele, prompt, max_tokens=2500):
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
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
<<<<<<< HEAD
    with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as r:
        rep = json.loads(r.read())
    return rep["content"][0]["text"]

def analyser_jour(day=None):
=======
    with urllib.request.urlopen(req, timeout=180, context=SSL_CTX) as r:
        rep = json.loads(r.read())
    return rep["content"][0]["text"]

def analyser_periode(start=None, end=None):
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
    if not API_KEY:
        return False, ("Aucune clé API configurée. Renseignez API_KEY dans rss_local.py "
                       "ou la variable d'environnement ANTHROPIC_API_KEY "
                       "(clé + crédits sur console.anthropic.com, Plans & Billing).")
<<<<<<< HEAD
    day = day or datetime.now().strftime("%Y-%m-%d")
    arts = articles_of_day(day)
    if not arts:
        return False, f"Aucun article pour le {day}. Rafraîchissez d'abord les flux."

    prompt_tri = get_reglage("prompt_tri", PROMPT_TRI_DEFAUT)
    prompt_synthese = get_reglage("prompt_synthese", PROMPT_SYNTHESE_DEFAUT)

    try:
        lots = [arts[i:i+TAILLE_LOT] for i in range(0, len(arts), TAILLE_LOT)]
        tris = []
        for lot in lots:
            liste = "\n".join(
                f"- [{a['flux']}] {a['titre']} — {a['resume'][:150]}" for a in lot)
            tris.append(appel_claude(MODELE_TRI,
                prompt_tri + "\n\n" + liste, max_tokens=800))
        synthese = appel_claude(MODELE_SYNTHESE,
            prompt_synthese + f"\n\n(Journée du {day}, {len(arts)} articles.)\n\n"
            + "\n---\n".join(tris), max_tokens=2000)
=======
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
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return False, f"Erreur API (HTTP {e.code}) : {detail}"
    except Exception as e:
        return False, f"Erreur d'analyse : {e}"

    c = db()
    c.execute("INSERT OR REPLACE INTO syntheses(jour, texte, cree) VALUES(?,?,?)",
<<<<<<< HEAD
              (day, synthese, datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close()
    try:
        with open(f"synthese_{day}.txt", "w", encoding="utf-8") as f:
            f.write(f"Synthèse RSSLocal — {day}\n{'='*40}\n\n{synthese}")
    except Exception:
        pass
    return True, synthese

def synthese_existante(day):
    c = db()
    row = c.execute("SELECT texte FROM syntheses WHERE jour=?", (day,)).fetchone()
    c.close()
    return row[0] if row else None

=======
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

>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
# ==================== INTERFACE WEB ====================

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>RSSLocal</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px;background:#f7f7f5;color:#222}
h1{font-size:1.4rem} .bar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0;align-items:center}
button,a.btn,label.btn{padding:8px 14px;border:1px solid #bbb;border-radius:8px;background:#fff;cursor:pointer;text-decoration:none;color:#222;font-size:.9rem}
button:hover,a.btn:hover,label.btn:hover{background:#eee}
button.ia{background:#4a3b8f;color:#fff;border-color:#4a3b8f}button.ia:hover{background:#5c4bb0}
<<<<<<< HEAD
select,input[type=date],input[type=text]{padding:6px;border-radius:8px;border:1px solid #bbb;font-size:.9rem}
=======
button.danger{border-color:#c0392b;color:#c0392b}button.danger:hover{background:#fdf0ee}
select,input[type=date],input[type=text],input[type=email]{padding:6px;border-radius:8px;border:1px solid #bbb;font-size:.9rem}
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
.art{background:#fff;border:1px solid #e2e2e0;border-radius:10px;padding:12px 16px;margin:10px 0}
.art h3{margin:0 0 4px;font-size:1.02rem}.art .meta{color:#777;font-size:.8rem}
.art p{margin:6px 0 0;font-size:.9rem;color:#444}
.share{margin-top:6px;display:flex;gap:6px}
.share a,.share button{padding:3px 8px;font-size:.8rem;border:1px solid #ddd;border-radius:6px;background:#fafafa;cursor:pointer;text-decoration:none;color:#333}
<<<<<<< HEAD
#status{white-space:pre-wrap;font-size:.8rem;color:#555;background:#eee;padding:8px;border-radius:8px;display:none;margin:8px 0}
=======
#status{font-size:.85rem;color:#333;background:#eee;padding:8px 12px;border-radius:8px;display:none;margin:8px 0}
#status details{margin-top:6px}#status summary{cursor:pointer;color:#666;font-size:.8rem}
#status pre{white-space:pre-wrap;font-size:.78rem;color:#555;margin:6px 0 0}
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
details.media{background:#fff;border:1px solid #e2e2e0;border-radius:10px;margin:8px 0;padding:4px 12px}
details.media summary{cursor:pointer;font-weight:600;padding:8px 0}
details.media .art{border:none;border-top:1px solid #eee;border-radius:0;margin:0}
#synthbox{background:#fffbe8;border:1px solid #e6d98a;border-radius:10px;margin:12px 0;display:none}
#synthhead{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;cursor:pointer;font-weight:600}
<<<<<<< HEAD
#synthbody{white-space:pre-wrap;padding:0 16px 16px;font-size:.92rem}
=======
#synthbody{white-space:pre-wrap;padding:0 16px 12px;font-size:.92rem}
#synthactions{padding:0 16px 14px;display:flex;gap:8px}
#synthactions a,#synthactions button{padding:4px 10px;font-size:.8rem;border:1px solid #d8c96a;border-radius:6px;background:#fff;cursor:pointer;text-decoration:none;color:#333}
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
#synthclose{border:none;background:none;font-size:1.1rem;cursor:pointer;color:#8a7b2e}
.panel{background:#fff;border:1px solid #ccc;border-radius:10px;padding:16px;margin:12px 0;display:none}
.panel h2{font-size:1.05rem;margin:0 0 10px}
.feedrow{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #eee;font-size:.9rem;gap:8px}
.feedrow .finfo{flex:1;min-width:0}.feedrow .furl{color:#999;font-size:.75rem;word-break:break-all}
.feedrow button{padding:4px 10px;font-size:.8rem}
textarea{width:100%;box-sizing:border-box;min-height:110px;border:1px solid #bbb;border-radius:8px;padding:8px;font-size:.85rem;font-family:inherit}
<<<<<<< HEAD
.addrow{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.addrow input{flex:1;min-width:120px}
</style></head><body>
<h1>📰 RSSLocal v2 — agrégateur local</h1>
<div class="bar">
<button onclick="refresh()">🔄 Rafraîchir</button>
<label>Jour : <input type="date" id="day" onchange="load()"></label>
=======
.addrow{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.addrow input{flex:1;min-width:120px}
#customdates{display:none;gap:6px;align-items:center}
.reglrow{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}
</style></head><body>
<h1>📰 RSSLocal v3 — agrégateur local</h1>
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
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
<select id="tri" onchange="load()">
<option value="desc">Plus récents d'abord</option>
<option value="asc">Plus anciens d'abord</option>
<option value="media">Grouper par média</option>
</select>
<<<<<<< HEAD
<button class="ia" onclick="analyse()">🧠 Analyser la journée</button>
</div>
<div class="bar">
<button onclick="toggle('pfeeds');loadFeeds()">⚙️ Gérer les flux</button>
<button onclick="toggle('pprompts');loadPrompts()">🧠 Réglages de l'analyse</button>
=======
<button class="ia" onclick="analyse()">🧠 Analyser la période</button>
</div>
<div class="bar">
<button onclick="toggle('pfeeds');loadFeeds()">⚙️ Flux</button>
<button onclick="toggle('preglages');loadReglages()">🛠 Réglages</button>
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
<a class="btn" id="ejson" href="#">⬇ JSON</a>
<a class="btn" id="ecsv" href="#">⬇ CSV</a>
<a class="btn" href="/export/opml">⬇ OPML</a>
<button onclick="hook()">📤 n8n</button>
<label class="btn">📁 Importer OPML<input type="file" id="opml" style="display:none" onchange="upload(this)"></label>
</div>
<div id="status"></div>

<div class="panel" id="pfeeds">
<h2>⚙️ Gérer les flux</h2>
<div id="feedlist"></div>
<div class="addrow">
<input type="text" id="furl" placeholder="URL du flux (https://…)">
<input type="text" id="ftitre" placeholder="Titre (optionnel)">
<input type="text" id="fcat" placeholder="Catégorie (optionnel)">
<button onclick="addFeed()">➕ Ajouter</button>
</div>
</div>

<<<<<<< HEAD
<div class="panel" id="pprompts">
<h2>🧠 Réglages de l'analyse</h2>
<p style="font-size:.85rem;color:#666">Prompt de <b>tri</b> (envoyé à Haiku, par lots d'articles) :</p>
<textarea id="ptri"></textarea>
<p style="font-size:.85rem;color:#666">Prompt de <b>synthèse</b> (envoyé à Sonnet, avec les tris) :</p>
<textarea id="psynth"></textarea>
<div class="bar">
<button onclick="savePrompts()">💾 Enregistrer</button>
<button onclick="resetPrompts()">↩️ Rétablir les prompts par défaut</button>
</div>
=======
<div class="panel" id="preglages">
<h2>🛠 Réglages</h2>
<div class="reglrow"><label>✉️ Destinataire mail par défaut :</label>
<input type="email" id="rmail" placeholder="prenom.nom@exemple.fr" style="flex:1;min-width:200px">
<button onclick="saveMail()">💾</button></div>
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
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
</div>

<div id="synthbox">
<div id="synthhead" onclick="foldSynth()"><span id="synthtitle">🧠 Synthèse</span>
<button id="synthclose" onclick="event.stopPropagation();closeSynth()">✕</button></div>
<div id="synthbody"></div>
<<<<<<< HEAD
=======
<div id="synthactions">
<a id="smd" href="#">⬇ Export .md</a>
<a id="smail" href="#">✉️ Envoyer par mail</a>
</div>
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
</div>
<div id="list"></div>

<script>
<<<<<<< HEAD
const day=document.getElementById('day');day.value=new Date().toISOString().slice(0,10);
let synthHidden={};

function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function links(){document.getElementById('ejson').href='/export/json?day='+day.value;
document.getElementById('ecsv').href='/export/csv?day='+day.value;}
function toggle(id){const p=document.getElementById(id);
p.style.display=p.style.display==='block'?'none':'block';}
function show(t){const s=document.getElementById('status');s.style.display='block';s.textContent=t;}

function artHTML(x){
const txt=encodeURIComponent(x.titre+' — '+x.lien);
const mail='mailto:?subject='+encodeURIComponent(x.titre)+'&body='+txt;
=======
let synthHidden={};let mailDefaut='';
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
function qs(){const[s,e]=bornes();return'start='+s+'&end='+e;}
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
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
const wa='https://wa.me/?text='+txt;
return `<div class="art"><h3><a href="${esc(x.lien)}" target="_blank">${esc(x.titre)}</a></h3>
<div class="meta">${esc(x.flux)} — ${x.date?x.date.slice(0,16).replace('T',' '):''}</div>
<p>${esc(x.resume)}</p>
<div class="share"><a href="${mail}">✉️ Mail</a>
<a href="${wa}" target="_blank">💬 WhatsApp</a>
<button onclick="navigator.clipboard.writeText('${esc(x.lien)}');this.textContent='✓ Copié'">📋 Copier</button></div></div>`;}

async function load(){links();
<<<<<<< HEAD
const r=await fetch('/api/articles?day='+day.value);let a=await r.json();
const mode=document.getElementById('tri').value;
const list=document.getElementById('list');
if(!a.length){list.innerHTML='<p>Aucun article ce jour. Importez un OPML ou ajoutez des flux, puis rafraîchissez.</p>';}
=======
const r=await fetch('/api/articles?'+qs());let a=await r.json();
const mode=document.getElementById('tri').value;
const list=document.getElementById('list');
if(!a.length){list.innerHTML='<p>Aucun article sur cette période. Importez un OPML ou ajoutez des flux, puis rafraîchissez.</p>';}
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
else if(mode==='media'){
const g={};a.forEach(x=>{(g[x.flux]=g[x.flux]||[]).push(x);});
list.innerHTML=Object.keys(g).sort().map(m=>
`<details class="media" open><summary>${esc(m)} (${g[m].length})</summary>
${g[m].map(artHTML).join('')}</details>`).join('');}
else{if(mode==='asc')a=a.slice().reverse();
list.innerHTML=a.map(artHTML).join('');}
<<<<<<< HEAD
const s=await fetch('/api/synthese?day='+day.value);const js=await s.json();
const box=document.getElementById('synthbox');
if(js.texte&&!synthHidden[day.value]){box.style.display='block';
document.getElementById('synthtitle').textContent='🧠 Synthèse du '+day.value+' (cliquer pour plier/déplier)';
document.getElementById('synthbody').textContent=js.texte;}
=======
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
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
else{box.style.display='none';}}

function foldSynth(){const b=document.getElementById('synthbody');
b.style.display=b.style.display==='none'?'block':'none';}
<<<<<<< HEAD
function closeSynth(){synthHidden[day.value]=true;
=======
function closeSynth(){const[s,e]=bornes();synthHidden[s+'_'+e]=true;
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
document.getElementById('synthbox').style.display='none';}

async function refresh(){show('Rafraîchissement en cours…');
const r=await fetch('/api/refresh',{method:'POST'});const j=await r.json();
<<<<<<< HEAD
show(j.nouveaux+' nouveaux articles\\n'+j.rapport.join('\\n'));load();}

async function analyse(){
show('🧠 Analyse en cours (tri Haiku, synthèse Sonnet)… 1 à 2 minutes.');
const r=await fetch('/api/analyse?day='+day.value,{method:'POST'});const j=await r.json();
if(j.ok){synthHidden[day.value]=false;show('Synthèse générée et sauvegardée (synthese_'+day.value+'.txt).');load();}
=======
const ligne=(j.erreurs?'⚠ ':'✓ ')+j.nouveaux+' nouveaux articles'
+(j.erreurs?' — '+j.erreurs+' flux en erreur':'');
show(ligne,j.rapport.join('\\n'));load();}

async function analyse(){
show('🧠 Analyse en cours (Sonnet)… jusqu\\'à 2 minutes selon le volume.');
const r=await fetch('/api/analyse?'+qs(),{method:'POST'});const j=await r.json();
if(j.ok){const[s,e]=bornes();synthHidden[s+'_'+e]=false;
show('✓ Synthèse générée.');load();}
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
else{show('⚠ '+j.message);}}

async function upload(inp){const t=await inp.files[0].text();
const r=await fetch('/api/import',{method:'POST',body:t});const j=await r.json();
show((j.ajoutes||0)+' flux ajoutés. Cliquez sur Rafraîchir.');inp.value='';}

<<<<<<< HEAD
async function hook(){const r=await fetch('/api/webhook?day='+day.value,{method:'POST'});
=======
async function hook(){const r=await fetch('/api/webhook?'+qs(),{method:'POST'});
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
const j=await r.json();show(j.message);}

async function loadFeeds(){const r=await fetch('/api/feeds');const f=await r.json();
document.getElementById('feedlist').innerHTML=f.length?f.map(x=>
`<div class="feedrow"><div class="finfo"><b>${esc(x.titre)}</b>
${x.categorie?' · '+esc(x.categorie):''} · ${x.articles} art.
<div class="furl">${esc(x.url)}</div></div>
<<<<<<< HEAD
=======
<button onclick="purgeFlux(${x.id},'${esc(x.titre).replace(/'/g,"\\\\'")}')">🧹 Vider</button>
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
<button onclick="delFeed(${x.id},'${esc(x.titre).replace(/'/g,"\\\\'")}')">🗑 Supprimer</button></div>`).join('')
:'<p>Aucun flux. Ajoutez-en un ci-dessous ou importez un OPML.</p>';}

async function addFeed(){
const body=JSON.stringify({url:document.getElementById('furl').value,
titre:document.getElementById('ftitre').value,
categorie:document.getElementById('fcat').value});
const r=await fetch('/api/feeds/add',{method:'POST',body:body});const j=await r.json();
show(j.message);if(j.ok){document.getElementById('furl').value='';
document.getElementById('ftitre').value='';document.getElementById('fcat').value='';loadFeeds();}}

async function delFeed(id,titre){
<<<<<<< HEAD
const purge=confirm('Supprimer le flux « '+titre+' » ?\\n\\nOK = supprimer le flux en CONSERVANT ses articles archivés (recommandé).\\nPour effacer aussi les articles, maintenez la case suivante.')
if(purge===null)return;
let wipe=false;
if(purge){wipe=confirm('Effacer AUSSI tous les articles archivés de ce flux ?\\n\\nOK = tout effacer\\nAnnuler = conserver les articles');} 
else return;
=======
if(!confirm('Supprimer le flux « '+titre+' » ?'))return;
const wipe=confirm('Effacer AUSSI ses articles archivés ?\\nOK = effacer, Annuler = conserver');
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
const r=await fetch('/api/feeds/delete',{method:'POST',
body:JSON.stringify({id:id,purge:wipe})});const j=await r.json();
show(j.message);loadFeeds();load();}

<<<<<<< HEAD
async function loadPrompts(){const r=await fetch('/api/prompts');const j=await r.json();
document.getElementById('ptri').value=j.tri;
document.getElementById('psynth').value=j.synthese;}

async function savePrompts(){
const r=await fetch('/api/prompts',{method:'POST',
body:JSON.stringify({tri:document.getElementById('ptri').value,
synthese:document.getElementById('psynth').value})});
const j=await r.json();show(j.message);}

async function resetPrompts(){
const r=await fetch('/api/prompts/reset',{method:'POST'});const j=await r.json();
show(j.message);loadPrompts();}

load();
=======
async function purgeFlux(id,titre){
if(!confirm('Vider tous les articles de « '+titre+' » ?\\n(Le flux reste abonné.)'))return;
const r=await fetch('/api/purge/flux',{method:'POST',
body:JSON.stringify({id:id})});const j=await r.json();
show(j.message);loadFeeds();load();}

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
document.getElementById('rmail').value=j.mail;mailDefaut=j.mail;}

async function saveMail(){
const r=await fetch('/api/reglages/mail',{method:'POST',
body:JSON.stringify({mail:document.getElementById('rmail').value})});
const j=await r.json();mailDefaut=document.getElementById('rmail').value;
show(j.message);load();}

async function savePrompt(){
const r=await fetch('/api/reglages/prompt',{method:'POST',
body:JSON.stringify({prompt:document.getElementById('pana').value})});
const j=await r.json();show(j.message);}

async function resetPrompt(){
const r=await fetch('/api/reglages/prompt',{method:'POST',
body:JSON.stringify({prompt:''})});const j=await r.json();
show('Prompt par défaut rétabli.');loadReglages();}

(async()=>{const r=await fetch('/api/reglages');const j=await r.json();
mailDefaut=j.mail;load();})();
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
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
<<<<<<< HEAD

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        day = q.get("day", [None])[0]
        if u.path == "/": self._send(PAGE)
        elif u.path == "/api/articles":
            self._json(articles_of_day(day))
        elif u.path == "/api/feeds":
            self._json(list_feeds())
        elif u.path == "/api/prompts":
            self._json({"tri": get_reglage("prompt_tri", PROMPT_TRI_DEFAUT),
                        "synthese": get_reglage("prompt_synthese", PROMPT_SYNTHESE_DEFAUT)})
        elif u.path == "/api/synthese":
            d = day or datetime.now().strftime("%Y-%m-%d")
            self._json({"texte": synthese_existante(d)})
        elif u.path == "/export/json":
            self._send(json.dumps(articles_of_day(day), ensure_ascii=False, indent=2),
                       "application/json; charset=utf-8", f"articles_{day or 'jour'}.json")
        elif u.path == "/export/csv":
            buf = io.StringIO(); w = csv.writer(buf, delimiter=";")
            w.writerow(["titre","lien","resume","date","flux","categorie"])
            for a in articles_of_day(day):
                w.writerow([a["titre"],a["lien"],a["resume"],a["date"],a["flux"],a["categorie"]])
            self._send("\ufeff"+buf.getvalue(), "text/csv; charset=utf-8",
                       f"articles_{day or 'jour'}.csv")
=======
    def _params(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        return u, q.get("start", [None])[0], q.get("end", [None])[0]

    def do_GET(self):
        u, start, end = self._params()
        if u.path == "/": self._send(PAGE)
        elif u.path == "/api/articles":
            self._json(articles_periode(start, end))
        elif u.path == "/api/feeds":
            self._json(list_feeds())
        elif u.path == "/api/reglages":
            self._json({"prompt": get_reglage("prompt_analyse", PROMPT_ANALYSE_DEFAUT),
                        "mail": get_reglage("mail_defaut", "")})
        elif u.path == "/api/synthese":
            self._json({"texte": synthese_existante(start, end)})
        elif u.path == "/export/json":
            s, e = borne_periode(start, end)
            self._send(json.dumps(articles_periode(s, e), ensure_ascii=False, indent=2),
                       "application/json; charset=utf-8", f"articles_{s}_{e}.json")
        elif u.path == "/export/csv":
            s, e = borne_periode(start, end)
            buf = io.StringIO(); w = csv.writer(buf, delimiter=";")
            w.writerow(["titre","lien","resume","date","flux","categorie"])
            for a in articles_periode(s, e):
                w.writerow([a["titre"],a["lien"],a["resume"],a["date"],a["flux"],a["categorie"]])
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
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
        elif u.path == "/export/opml":
            self._send(export_opml(), "text/xml; charset=utf-8", "flux.opml")
        else: self.send_error(404)

    def do_POST(self):
<<<<<<< HEAD
        u = urlparse(self.path); q = parse_qs(u.query)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        if u.path == "/api/refresh":
            n, rep = refresh_all()
            self._json({"nouveaux": n, "rapport": rep})
        elif u.path == "/api/analyse":
            ok, msg = analyser_jour(q.get("day", [None])[0])
=======
        u, start, end = self._params()
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
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
            self._json({"ok": ok, "message": msg if not ok else "OK"})
        elif u.path == "/api/import":
            try: n = import_opml(body)
            except Exception as e:
                self._json({"ajoutes": 0, "erreur": str(e)}); return
            self._json({"ajoutes": n})
        elif u.path == "/api/feeds/add":
<<<<<<< HEAD
            try: d = json.loads(body)
            except Exception:
                self._json({"ok": False, "message": "Requête invalide."}); return
            ok, msg = add_feed(d.get("url",""), d.get("titre",""), d.get("categorie",""))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/feeds/delete":
            try: d = json.loads(body)
            except Exception:
                self._json({"ok": False, "message": "Requête invalide."}); return
            ok, msg = delete_feed(int(d.get("id", 0)), bool(d.get("purge", False)))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/prompts":
            try: d = json.loads(body)
            except Exception:
                self._json({"ok": False, "message": "Requête invalide."}); return
            set_reglage("prompt_tri", d.get("tri", ""))
            set_reglage("prompt_synthese", d.get("synthese", ""))
            self._json({"ok": True, "message": "Prompts enregistrés."})
        elif u.path == "/api/prompts/reset":
            set_reglage("prompt_tri", "")
            set_reglage("prompt_synthese", "")
            self._json({"ok": True, "message": "Prompts par défaut rétablis."})
        elif u.path == "/api/webhook":
            ok, msg = send_webhook(q.get("day", [None])[0])
=======
            d = jbody()
            ok, msg = add_feed(d.get("url",""), d.get("titre",""), d.get("categorie",""))
            self._json({"ok": ok, "message": msg})
        elif u.path == "/api/feeds/delete":
            d = jbody()
            ok, msg = delete_feed(int(d.get("id", 0)), bool(d.get("purge", False)))
            self._json({"ok": ok, "message": msg})
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
        elif u.path == "/api/reglages/prompt":
            set_reglage("prompt_analyse", jbody().get("prompt", ""))
            self._json({"ok": True, "message": "Prompt enregistré."})
        elif u.path == "/api/webhook":
            ok, msg = send_webhook(start, end)
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
            self._json({"ok": ok, "message": msg})
        else: self.send_error(404)

if __name__ == "__main__":
    db().close()
<<<<<<< HEAD
    print(f"RSSLocal v2 démarré → http://localhost:{PORT}  (Ctrl+C pour arrêter)")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
=======
    print(f"RSSLocal v3 démarré → http://localhost:{PORT}  (Ctrl+C pour arrêter)")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
>>>>>>> 900a377 (V3 avec Sélection de période et prompt unique d'analyse)
