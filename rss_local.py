# -*- coding: utf-8 -*-
"""
RSSLocal v2 — Lecteur/agrégateur RSS 100 % local + analyse IA en entonnoir.
Aucun droit admin, aucune dépendance externe (stdlib Python uniquement).
Usage : python rss_local.py   puis ouvrir http://localhost:8765
"""
import sqlite3, json, csv, io, re, threading, webbrowser
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

# ---------- CONFIGURATION ----------
PORT = 8765
DB_PATH = "rss_local.db"
WEBHOOK_N8N = ""     # ex: "http://localhost:5678/webhook/rss" — vide si inutilisé
USER_AGENT = "Mozilla/5.0 (RSSLocal/2.0; lecteur personnel)"
TIMEOUT = 15

# --- Analyse IA (laisser API_KEY vide pour désactiver) ---
API_KEY = ""                                # clé depuis console.anthropic.com
MODELE_TRI = "claude-haiku-4-5-20251001"    # tâches simples : tri, classement
MODELE_SYNTHESE = "claude-sonnet-4-6"       # synthèse éditoriale finale
TAILLE_LOT = 20                             # articles par lot envoyé à Haiku
# -----------------------------------

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
    return c

# ==================== COLLECTE DES FLUX ====================

def parse_date(s):
    if not s: return None
    s = s.strip()
    try: return parsedate_to_datetime(s)                          # RFC 822 (RSS)
    except Exception: pass
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO (Atom)
    except Exception: return None

def strip_html(t):
    return re.sub(r"<[^>]+>", " ", t or "").strip()[:600]

def fetch_feed(feed_id, url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
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
    c = db()
    feeds = c.execute("SELECT id, url, title FROM feeds").fetchall()
    def work(f):
        fid, url, ftitle = f
        items, err = fetch_feed(fid, url)
        return fid, ftitle or url, items, err
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, feeds))
    now = datetime.now(timezone.utc).isoformat()
    new_count, report = 0, []
    for fid, ftitle, items, err in results:
        if err:
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
    return new_count, report

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

# ==================== EXPORTS / WEBHOOK ====================

def articles_of_day(day=None, feed_ids=None):
    day = day or datetime.now().strftime("%Y-%m-%d")
    c = db()
    q = """SELECT a.title, a.link, a.summary, a.published, f.title, f.category
           FROM articles a JOIN feeds f ON f.id=a.feed_id
           WHERE substr(a.published,1,10)=?"""
    params = [day]
    if feed_ids:
        q += f" AND a.feed_id IN ({','.join('?'*len(feed_ids))})"
        params += feed_ids
    q += " ORDER BY a.published DESC"
    rows = c.execute(q, params).fetchall(); c.close()
    return [{"titre": r[0], "lien": r[1], "resume": r[2], "date": r[3],
             "flux": r[4], "categorie": r[5]} for r in rows]

def send_webhook(day=None):
    if not WEBHOOK_N8N:
        return False, "Aucune URL de webhook configurée (variable WEBHOOK_N8N)."
    payload = json.dumps({"jour": day or datetime.now().strftime("%Y-%m-%d"),
                          "articles": articles_of_day(day)}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_N8N, data=payload,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return True, f"Webhook OK (HTTP {r.status})"
    except Exception as e:
        return False, f"Échec webhook: {e}"

# ==================== ANALYSE IA EN ENTONNOIR ====================

def appel_claude(modele, prompt, max_tokens=1024):
    """Appel minimal à l'API Anthropic Messages, sans bibliothèque externe."""
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
    with urllib.request.urlopen(req, timeout=120) as r:
        rep = json.loads(r.read())
    return rep["content"][0]["text"]

def analyser_jour(day=None):
    """Étape 1 : Haiku trie/classe par lots (économique).
       Étape 2 : Sonnet synthétise à partir des tris condensés.
       Retourne (ok, texte_ou_erreur)."""
    if not API_KEY:
        return False, ("Aucune clé API configurée. Ouvrez rss_local.py et renseignez "
                       "la variable API_KEY (clé disponible sur console.anthropic.com).")
    day = day or datetime.now().strftime("%Y-%m-%d")
    arts = articles_of_day(day)
    if not arts:
        return False, f"Aucun article pour le {day}. Rafraîchissez d'abord les flux."

    try:
        # --- Étape 1 : tri par lots via Haiku ---
        lots = [arts[i:i+TAILLE_LOT] for i in range(0, len(arts), TAILLE_LOT)]
        tris = []
        for lot in lots:
            liste = "\n".join(
                f"- [{a['flux']}] {a['titre']} — {a['resume'][:150]}" for a in lot)
            tris.append(appel_claude(MODELE_TRI,
                "Tu es un assistant de veille pour un journaliste. "
                "Classe les articles suivants par grand thème (politique, économie, "
                "santé, culture, sport, autre). Pour chaque thème, liste les titres "
                "et signale d'un ⭐ les 2-3 articles les plus notables. "
                "Sois concis, pas de commentaire superflu.\n\n" + liste,
                max_tokens=800))

        # --- Étape 2 : synthèse via Sonnet ---
        synthese = appel_claude(MODELE_SYNTHESE,
            f"Tu es l'assistant de veille d'un journaliste généraliste. "
            f"Voici les tris thématiques des {len(arts)} articles du {day}, "
            f"réalisés par lots. Rédige une synthèse éditoriale structurée : "
            f"1) les 5 faits marquants du jour, 2) un panorama par thème, "
            f"3) trois angles d'articles possibles. Style factuel et neutre.\n\n"
            + "\n---\n".join(tris),
            max_tokens=2000)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return False, f"Erreur API (HTTP {e.code}) : {detail}"
    except Exception as e:
        return False, f"Erreur d'analyse : {e}"

    # Sauvegarde : base + fichier texte daté
    c = db()
    c.execute("INSERT OR REPLACE INTO syntheses(jour, texte, cree) VALUES(?,?,?)",
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

# ==================== INTERFACE WEB ====================

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>RSSLocal</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px;background:#f7f7f5;color:#222}
h1{font-size:1.4rem} .bar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
button,a.btn,label.btn{padding:8px 14px;border:1px solid #bbb;border-radius:8px;background:#fff;cursor:pointer;text-decoration:none;color:#222;font-size:.9rem}
button:hover,a.btn:hover,label.btn:hover{background:#eee}
button.ia{background:#4a3b8f;color:#fff;border-color:#4a3b8f}
button.ia:hover{background:#5c4bb0}
.art{background:#fff;border:1px solid #e2e2e0;border-radius:10px;padding:12px 16px;margin:10px 0}
.art h3{margin:0 0 4px;font-size:1.02rem}.art .meta{color:#777;font-size:.8rem}
.art p{margin:6px 0 0;font-size:.9rem;color:#444}
#status{white-space:pre-wrap;font-size:.8rem;color:#555;background:#eee;padding:8px;border-radius:8px;display:none}
#synthese{white-space:pre-wrap;background:#fffbe8;border:1px solid #e6d98a;border-radius:10px;padding:16px;margin:12px 0;display:none;font-size:.92rem}
input[type=date]{padding:6px;border-radius:8px;border:1px solid #bbb}
.spin{display:inline-block;animation:r 1s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
</style></head><body>
<h1>📰 RSSLocal — agrégateur local</h1>
<div class="bar">
<button onclick="refresh()">🔄 Rafraîchir les flux</button>
<label>Jour : <input type="date" id="day" onchange="load()"></label>
<button class="ia" onclick="analyse()">🧠 Analyser la journée</button>
<a class="btn" id="ejson" href="#">⬇ JSON</a>
<a class="btn" id="ecsv" href="#">⬇ CSV</a>
<a class="btn" href="/export/opml">⬇ OPML</a>
<button onclick="hook()">📤 Envoyer à n8n</button>
<label class="btn">📁 Importer OPML<input type="file" id="opml" style="display:none" onchange="upload(this)"></label>
</div>
<div id="status"></div>
<div id="synthese"></div>
<div id="list"></div>
<script>
const day=document.getElementById('day');day.value=new Date().toISOString().slice(0,10);
function links(){document.getElementById('ejson').href='/export/json?day='+day.value;
document.getElementById('ecsv').href='/export/csv?day='+day.value;}
async function load(){links();
const r=await fetch('/api/articles?day='+day.value);const a=await r.json();
document.getElementById('list').innerHTML=a.length?a.map(x=>
`<div class="art"><h3><a href="${x.lien}" target="_blank">${x.titre}</a></h3>
<div class="meta">${x.flux} — ${x.date?x.date.slice(0,16).replace('T',' '):''}</div>
<p>${x.resume||''}</p></div>`).join(''):'<p>Aucun article ce jour. Importez un OPML puis rafraîchissez.</p>';
const s=await fetch('/api/synthese?day='+day.value);const js=await s.json();
const box=document.getElementById('synthese');
if(js.texte){box.style.display='block';box.textContent='🧠 Synthèse du '+day.value+'\\n\\n'+js.texte;}
else{box.style.display='none';box.textContent='';}}
async function refresh(){show('Rafraîchissement en cours…');
const r=await fetch('/api/refresh',{method:'POST'});const j=await r.json();
show(j.nouveaux+' nouveaux articles\\n'+j.rapport.join('\\n'));load();}
async function analyse(){
show('🧠 Analyse en cours (tri par Haiku, synthèse par Sonnet)… Cela peut prendre 1 à 2 minutes.');
const r=await fetch('/api/analyse?day='+day.value,{method:'POST'});const j=await r.json();
if(j.ok){show('Synthèse générée et sauvegardée (synthese_'+day.value+'.txt).');load();}
else{show('⚠ '+j.message);}}
async function upload(inp){const t=await inp.files[0].text();
const r=await fetch('/api/import',{method:'POST',body:t});const j=await r.json();
show(j.ajoutes+' flux ajoutés. Cliquez sur Rafraîchir.');}
async function hook(){const r=await fetch('/api/webhook?day='+day.value,{method:'POST'});
const j=await r.json();show(j.message);}
function show(t){const s=document.getElementById('status');s.style.display='block';s.textContent=t;}
load();
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

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        day = q.get("day", [None])[0]
        if u.path == "/": self._send(PAGE)
        elif u.path == "/api/articles":
            self._send(json.dumps(articles_of_day(day), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif u.path == "/api/synthese":
            d = day or datetime.now().strftime("%Y-%m-%d")
            self._send(json.dumps({"texte": synthese_existante(d)}, ensure_ascii=False),
                       "application/json; charset=utf-8")
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
        elif u.path == "/export/opml":
            self._send(export_opml(), "text/xml; charset=utf-8", "flux.opml")
        else: self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        if u.path == "/api/refresh":
            n, rep = refresh_all()
            self._send(json.dumps({"nouveaux": n, "rapport": rep}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif u.path == "/api/analyse":
            ok, msg = analyser_jour(q.get("day", [None])[0])
            self._send(json.dumps({"ok": ok, "message": msg if not ok else "OK"},
                                  ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif u.path == "/api/import":
            try: n = import_opml(body)
            except Exception as e:
                self._send(json.dumps({"ajoutes": 0, "erreur": str(e)}),
                           "application/json; charset=utf-8"); return
            self._send(json.dumps({"ajoutes": n}), "application/json; charset=utf-8")
        elif u.path == "/api/webhook":
            ok, msg = send_webhook(q.get("day", [None])[0])
            self._send(json.dumps({"ok": ok, "message": msg}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        else: self.send_error(404)

if __name__ == "__main__":
    db().close()
    print(f"RSSLocal v2 démarré → http://localhost:{PORT}  (Ctrl+C pour arrêter)")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()