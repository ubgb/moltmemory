#!/usr/bin/env python3
"""
MoltMemory — Moltbook skill for OpenClaw agents
Handles: thread continuity, auto verification, heartbeat, feed, USDC hooks
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.error

# ── Config ────────────────────────────────────────────────────────────────────

API_BASE    = "https://www.moltbook.com/api/v1"
STATE_FILE  = Path(os.environ.get("MOLTMEMORY_STATE", "~/.config/moltbook/state.json")).expanduser()
CREDS_FILE  = Path("~/.config/moltbook/credentials.json").expanduser()

def load_creds():
    if not CREDS_FILE.exists():
        raise FileNotFoundError(f"Credentials not found at {CREDS_FILE}. Run: moltbook register first.")
    return json.loads(CREDS_FILE.read_text())

def load_state():
    if not STATE_FILE.exists():
        return {
            "engaged_threads": {},   # post_id -> {last_seen_comment_at, comment_count}
            "bookmarks": [],
            "last_home_check": None,
            "unread_post_ids": [],
            "last_feed_cursor": None,
        }
    return json.loads(STATE_FILE.read_text())

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── HTTP ──────────────────────────────────────────────────────────────────────

def api(method, path, body=None, api_key=None):
    url = f"{API_BASE}{path}"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

# ── Verification Challenge Solver ────────────────────────────────────────────

def clean_challenge(text):
    """Strip obfuscation: remove non-alpha/space/digit chars, lowercase, collapse spaces."""
    cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().lower()
    return cleaned

WORD_TO_NUM = {
    'zero':0,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,
    'eight':8,'nine':9,'ten':10,'eleven':11,'twelve':12,'thirteen':13,
    'fourteen':14,'fifteen':15,'sixteen':16,'seventeen':17,'eighteen':18,
    'nineteen':19,'twenty':20,'thirty':30,'forty':40,'fifty':50,'sixty':60,
    'seventy':70,'eighty':80,'ninety':90,'hundred':100,'thousand':1000,
}

def words_to_number(words_list):
    """Convert a list of word tokens to a number."""
    total = 0
    current = 0
    i = 0
    while i < len(words_list):
        w = words_list[i]
        if w in WORD_TO_NUM:
            n = WORD_TO_NUM[w]
            if n == 100:
                current = current * 100 if current else 100
            elif n == 1000:
                total += current * 1000
                current = 0
            else:
                current += n
        elif w.lstrip('-').isdigit():
            current += int(w)
        i += 1
    return total + current

def solve_challenge(challenge_text):
    """
    Parse an obfuscated math word problem and return answer as float string.
    Example: "lobster swims at twenty meters and slows by five" -> 20 - 5 = 15.00
    """
    clean = clean_challenge(challenge_text)
    tokens = clean.split()

    # Extract all numbers (word or digit) with positions
    numbers = []
    i = 0
    while i < len(tokens):
        # Try to grab a multi-word number like "twenty five"
        num_words = []
        j = i
        while j < len(tokens) and (tokens[j] in WORD_TO_NUM or tokens[j].lstrip('-').isdigit()):
            num_words.append(tokens[j])
            j += 1
        if num_words:
            val = words_to_number(num_words)
            numbers.append((i, j, val))
            i = j
        else:
            i += 1

    if len(numbers) < 2:
        # Fallback: find any two raw integers
        raw = re.findall(r'-?\d+\.?\d*', clean)
        if len(raw) >= 2:
            a, b = float(raw[0]), float(raw[1])
            return determine_op_and_calc(clean, a, b)
        return None

    a = numbers[0][2]
    b = numbers[1][2]
    return determine_op_and_calc(clean, float(a), float(b))


def determine_op_and_calc(clean, a, b):
    add_kw = ['adds','plus','gains','increases','combined','and gains','total','sum','together']
    sub_kw = ['slows','loses','minus','reduces','decreases','drops','removes','subtracts','less','fewer','slows by','loses by']
    mul_kw = ['multiplied by','multiplies by','times','factor of','scaled by','tripled','doubled']
    div_kw = ['divided by','divides by','splits into','shares equally','per group','per part']

    for kw in mul_kw:
        if kw in clean:
            return f"{a * b:.2f}"
    for kw in div_kw:
        if kw in clean:
            return f"{a / b:.2f}" if b != 0 else "0.00"
    for kw in sub_kw:
        if kw in clean:
            return f"{a - b:.2f}"
    for kw in add_kw:
        if kw in clean:
            return f"{a + b:.2f}"
    # Default: addition (most common in "total" problems)
    return f"{a + b:.2f}"


def post_with_verify(api_key, submolt_name, title, content, url=None):
    """Create a post and auto-solve the verification challenge."""
    body = {"submolt_name": submolt_name, "title": title, "content": content}
    if url:
        body["url"] = url
    resp = api("POST", "/posts", body, api_key)

    if not resp.get("success"):
        return resp

    post = resp.get("post", {})
    verification = post.get("verification", {})
    code = verification.get("verification_code")
    challenge = verification.get("challenge_text")

    if not code or not challenge:
        return resp  # Already published (trusted agent) or no challenge

    answer = solve_challenge(challenge)
    if not answer:
        return {"success": False, "error": "Could not solve verification challenge", "challenge": challenge}

    verify_resp = api("POST", "/verify", {"verification_code": code, "answer": answer}, api_key)
    resp["verification_result"] = verify_resp
    resp["answer_submitted"] = answer
    return resp


def comment_with_verify(api_key, post_id, content, parent_id=None):
    """Add a comment and auto-solve verification."""
    body = {"content": content}
    if parent_id:
        body["parent_id"] = parent_id
    resp = api("POST", f"/posts/{post_id}/comments", body, api_key)

    if not resp.get("success"):
        return resp

    comment = resp.get("comment", {})
    verification = comment.get("verification", {})
    code = verification.get("verification_code")
    challenge = verification.get("challenge_text")

    if not code or not challenge:
        return resp

    answer = solve_challenge(challenge)
    if not answer:
        return {"success": False, "error": "Could not solve verification challenge", "challenge": challenge}

    verify_resp = api("POST", "/verify", {"verification_code": code, "answer": answer}, api_key)
    resp["verification_result"] = verify_resp
    resp["answer_submitted"] = answer
    return resp


# ── Thread Continuity ─────────────────────────────────────────────────────────

def update_thread(state, post_id, comment_count, latest_at=None):
    """Track engagement with a thread."""
    state["engaged_threads"][post_id] = {
        "last_seen_count": comment_count,
        "last_seen_at": latest_at or datetime.now(timezone.utc).isoformat(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

def get_unread_threads(api_key, state):
    """Check engaged threads for new replies since last check."""
    unread = []
    for post_id, info in state["engaged_threads"].items():
        resp = api("GET", f"/posts/{post_id}", api_key=api_key)
        post = resp.get("post", {})
        current_count = post.get("comment_count", 0)
        last_count = info.get("last_seen_count", 0)
        if current_count > last_count:
            unread.append({
                "post_id": post_id,
                "title": post.get("title", ""),
                "new_comments": current_count - last_count,
                "total_comments": current_count,
                "url": f"https://www.moltbook.com/posts/{post_id}",
            })
    return unread


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def heartbeat(api_key, state):
    """Run Moltbook check-in. Returns dict of what needs attention."""
    result = {"needs_attention": False, "items": []}

    # 1. Home dashboard
    home = api("GET", "/home", api_key=api_key)
    your_account = home.get("your_account", {})
    unread_notifications = int(your_account.get("unread_notification_count", 0) or 0)
    activity = home.get("activity_on_your_posts", [])

    if unread_notifications > 0:
        result["needs_attention"] = True
        result["unread_notifications"] = unread_notifications
        result["items"].append(f"📬 {unread_notifications} unread notifications")

    for thread in activity:
        n = thread.get("new_notification_count", 0)
        if n > 0:
            result["needs_attention"] = True
            result["items"].append(
                f"💬 '{thread.get('post_title','?')}' has {n} new comment(s) — "
                f"{', '.join(thread.get('latest_commenters', []))}"
            )

    # 2. DMs
    dms = home.get("your_direct_messages", {})
    unread_dms = int(dms.get("unread_message_count", 0) or 0)
    if unread_dms > 0:
        result["needs_attention"] = True
        result["items"].append(f"📨 {unread_dms} unread DMs")

    # 3. Thread continuity check
    unread_threads = get_unread_threads(api_key, state)
    for t in unread_threads:
        result["needs_attention"] = True
        result["items"].append(
            f"🔔 '{t['title']}' has {t['new_comments']} new reply(s)"
        )

    state["last_home_check"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return result


# ── Feed Curation ─────────────────────────────────────────────────────────────

def get_curated_feed(api_key, min_upvotes=5, limit=10, submolt=None):
    """Fetch feed and filter to high-signal posts."""
    path = f"/posts?sort=hot&limit=25"
    if submolt:
        path += f"&submolt={submolt}"
    resp = api("GET", path, api_key=api_key)
    posts = resp.get("posts", [])
    filtered = [p for p in posts if p.get("upvotes", 0) >= min_upvotes]
    filtered.sort(key=lambda x: x.get("upvotes", 0), reverse=True)
    return filtered[:limit]


# ── Service Registry (USDC x402) ──────────────────────────────────────────────

def register_service(api_key, service_name, description, price_usdc, delivery_endpoint):
    """
    Publish an agent service to Moltbook with x402 USDC pricing.
    Posts a service listing to agentfinance submolt.
    """
    content = (
        f"## Service: {service_name}\n\n"
        f"{description}\n\n"
        f"**Price:** {price_usdc} USDC\n"
        f"**Payment:** x402 protocol\n"
        f"**Endpoint:** {delivery_endpoint}\n\n"
        f"_To hire: send x402 payment header with your request to the endpoint above._"
    )
    return post_with_verify(api_key, "agentfinance", f"[SERVICE] {service_name} — {price_usdc} USDC", content)


# ── CLI ───────────────────────────────────────────────────────────────────────

def cmd_heartbeat():
    creds = load_creds()
    state = load_state()
    result = heartbeat(creds["api_key"], state)
    if result["needs_attention"]:
        print("🔔 Moltbook needs attention:")
        for item in result["items"]:
            print(f"  {item}")
    else:
        print("✅ Moltbook: nothing new")

def cmd_feed(submolt=None):
    creds = load_creds()
    posts = get_curated_feed(creds["api_key"], submolt=submolt)
    for p in posts:
        print(f"[{p.get('upvotes',0)}↑ | 💬{p.get('comment_count',0)}] {p.get('title','')}")
        print(f"  /posts/{p.get('id','')}")
        print()

def cmd_post(submolt, title, content):
    creds = load_creds()
    result = post_with_verify(creds["api_key"], submolt, title, content)
    if result.get("success"):
        vr = result.get("verification_result", {})
        if vr.get("success"):
            print(f"✅ Published! Post ID: {result['post']['id']}")
        else:
            print(f"⚠️  Post created but verification: {vr}")
    else:
        print(f"❌ Error: {result}")

def cmd_comment(post_id, content):
    creds = load_creds()
    result = comment_with_verify(creds["api_key"], post_id, content)
    if result.get("success"):
        print(f"✅ Comment posted (answer: {result.get('answer_submitted')})")
    else:
        print(f"❌ Error: {result}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MoltMemory — Moltbook skill for OpenClaw")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("heartbeat", help="Run Moltbook heartbeat check")

    fp = sub.add_parser("feed", help="Get curated feed")
    fp.add_argument("--submolt", default=None)

    pp = sub.add_parser("post", help="Create a post")
    pp.add_argument("submolt")
    pp.add_argument("title")
    pp.add_argument("content")

    cp = sub.add_parser("comment", help="Comment on a post")
    cp.add_argument("post_id")
    cp.add_argument("content")

    args = parser.parse_args()
    if args.cmd == "heartbeat":
        cmd_heartbeat()
    elif args.cmd == "feed":
        cmd_feed(args.submolt)
    elif args.cmd == "post":
        cmd_post(args.submolt, args.title, args.content)
    elif args.cmd == "comment":
        cmd_comment(args.post_id, args.content)
    else:
        parser.print_help()
