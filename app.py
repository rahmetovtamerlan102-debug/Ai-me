import os
import re
import json
import sqlite3
import hashlib
from urllib.parse import quote
from flask import Flask, request, jsonify, send_from_directory
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

app = Flask(__name__)
DB = "cache.db"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            data TEXT,
            ts INTEGER
        )
    """)
    conn.commit()
    conn.close()


init_db()


def cache_get(key):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT data FROM cache WHERE key=?", (key,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def cache_set(key, data):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT OR REPLACE INTO cache VALUES (?, ?, strftime('%s','now'))",
        (key, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


# ============ DUCKDUCKGO ============
def search_ddg(query, max_results=5):
    try:
        ddgs = DDGS()
        results = ddgs.text(query, max_results=max_results)
        return list(results) if results else []
    except Exception as e:
        print("DDG error:", e)
        return []


# ============ WIKIPEDIA ============
def search_wikipedia(query, lang="ru"):
    try:
        url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(query)}"
        r = requests.get(url, timeout=6, headers={"User-Agent": "my-ai"})
        if r.status_code == 200:
            data = r.json()
            extract = data.get("extract", "")
            if extract:
                return {"text": extract, "title": data.get("title", "")}
    except Exception:
        pass
    return None


# ============ GITHUB ============
def github_headers():
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "my-ai",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def search_github_repos(query, max_results=5):
    try:
        url = "https://api.github.com/search/repositories"
        params = {
            "q": query,
            "per_page": max_results,
            "sort": "stars",
            "order": "desc",
        }
        r = requests.get(url, headers=github_headers(), params=params, timeout=10)
        if r.status_code != 200:
            print("GitHub repos:", r.status_code)
            return []
        data = r.json()
        results = []
        for item in data.get("items", []):
            results.append({
                "name": item["full_name"],
                "description": item.get("description") or "",
                "stars": item.get("stargazers_count", 0),
                "language": item.get("language") or "",
                "url": item["html_url"],
                "topics": (item.get("topics") or [])[:5],
            })
        return results
    except Exception as e:
        print("GitHub repos error:", e)
        return []


def search_github_code(query, max_results=3):
    if not GITHUB_TOKEN:
        return []
    try:
        url = "https://api.github.com/search/code"
        params = {"q": query, "per_page": max_results}
        r = requests.get(url, headers=github_headers(), params=params, timeout=10)
        if r.status_code != 200:
            print("GitHub code:", r.status_code)
            return []
        data = r.json()
        results = []
        for item in data.get("items", []):
            results.append({
                "file": item.get("name", ""),
                "path": item.get("path", ""),
                "repo": item["repository"]["full_name"],
                "url": item["html_url"],
            })
        return results
    except Exception as e:
        print("GitHub code error:", e)
        return []


def fetch_github_raw(raw_url, max_chars=1500):
    try:
        r = requests.get(raw_url, timeout=8, headers={"User-Agent": "my-ai"})
        if r.status_code == 200:
            return r.text[:max_chars]
    except Exception:
        pass
    return ""


# ============ PAGE PARSER ============
def fetch_page(url, timeout=6):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"}
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        main = soup.find("article") or soup.find("main") or soup.body
        if not main:
            return ""
        text = main.get_text(separator="\n", strip=True)
        return re.sub(r"\n{2,}", "\n", text)
    except Exception:
        return ""


def extract_relevant(text, keywords, max_paragraphs=2, max_len=400):
    if not text:
        return []
    keywords = [k.lower() for k in keywords if len(k) > 2]
    paragraphs = [p.strip() for p in text.split("\n") if len(p.strip()) > 40]

    scored = []
    for p in paragraphs:
        p_low = p.lower()
        score = sum(1 for k in keywords if k in p_low)
        if score > 0:
            scored.append((score, p[:max_len]))

    scored.sort(reverse=True, key=lambda x: x[0])

    seen, result = set(), []
    for _, p in scored:
        k = p[:60]
        if k not in seen:
            seen.add(k)
            result.append(p)
        if len(result) >= max_paragraphs:
            break
    return result


def is_code_query(q):
    kw = ["код", "функци", "напиши", "code", "function", "def ",
          "класс", "class ", "скрипт", "script", "python", "javascript",
          "библиотек", "repo", "репозитор", "github", "гитхаб"]
    ql = q.lower()
    return any(k in ql for k in kw)


# ============ MAIN SEARCH ============
def do_search(query):
    cache_key = hashlib.md5(query.lower().strip().encode()).hexdigest()
    cached = cache_get(cache_key)
    if cached:
        cached["from_cache"] = True
        return cached

    keywords = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    paragraphs = []
    repos = []
    code = []

    wiki = search_wikipedia(query)
    if wiki:
        paragraphs.append(wiki["text"][:500])

    want_github = is_code_query(query) or "github" in query.lower()
    if want_github:
        repos = search_github_repos(query, max_results=5)

    if want_github and GITHUB_TOKEN:
        gh_code = search_github_code(query, max_results=3)
        for item in gh_code:
            raw_url = item["url"].replace("/blob/", "/raw/")
            content = fetch_github_raw(raw_url)
            if content and len(content) > 30:
                code.append({
                    "file": f"{item['repo']}/{item['path']}",
                    "content": content[:1200],
                    "url": item["url"],
                })

    results = search_ddg(query, max_results=5)
    for r in results:
        url = r.get("href") or r.get("url") or ""
        snippet = r.get("body", "")
        if snippet:
            paragraphs.append(snippet)

        if url and "github.com" not in url:
            html = fetch_page(url)
            if html:
                for p in extract_relevant(html, keywords, max_paragraphs=1):
                    paragraphs.append(p)

        if len(paragraphs) >= 5:
            break

    seen, final_paras = set(), []
    for p in paragraphs:
        k = p[:80]
        if k not in seen:
            seen.add(k)
            final_paras.append(p)
        if len(final_paras) >= 4:
            break

    result = {
        "answers": final_paras,
        "repos": repos,
        "code": code,
        "from_cache": False,
    }
    cache_set(cache_key, result)
    return result


# ============ ROUTES ============
@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json() or {}
    q = (data.get("message") or "").strip()
    if not q:
        return jsonify({"error": "Пустой запрос"}), 400
    try:
        return jsonify(do_search(q))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/status")
def status():
    return jsonify({
        "github_token": bool(GITHUB_TOKEN),
        "ddg": True,
        "wikipedia": True,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
