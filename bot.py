import json
import os
import re
import subprocess
from datetime import datetime, timezone
from html import unescape

import requests
from bs4 import BeautifulSoup


# =============================================================================
# CONFIG
# =============================================================================

TRUMP_DIR = "trump"

WATCHLIST_FILE = f"{TRUMP_DIR}/watchlist.json"
ALERTS_FILE = f"{TRUMP_DIR}/alerts.json"
SEEN_FILE = f"{TRUMP_DIR}/seen_posts.json"

NTFY_TOPIC = os.getenv("NTFY_TOPIC")

# Set TEST_MODE=true in GitHub Actions if you want to force a fake post test.
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

# Public archive fallback.
# This is useful because GitHub Actions can get blocked directly by Truth Social.
PUBLIC_ARCHIVE_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"

TRUTH_HANDLE = "realDonaldTrump"
TRUTH_SOCIAL_RSS = "https://truthsocial.com/@realDonaldTrump.rss"

MAX_POSTS_TO_CHECK = 25
MAX_ALERTS_TO_KEEP = 200

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


# =============================================================================
# FILE HELPERS
# =============================================================================

def ensure_files_exist():
    os.makedirs(TRUMP_DIR, exist_ok=True)

    if not os.path.exists(WATCHLIST_FILE):
        save_json(WATCHLIST_FILE, {})

    if not os.path.exists(ALERTS_FILE):
        save_json(ALERTS_FILE, [])

    if not os.path.exists(SEEN_FILE):
        save_json(SEEN_FILE, [])


def load_json(path, default):
    if not os.path.exists(path):
        print(f"WARNING: {path} does not exist. Using default.")
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"WARNING: {path} is invalid JSON. Using default.")
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# =============================================================================
# TEXT HELPERS
# =============================================================================

def clean_html_text(value):
    if value is None:
        return ""

    text = str(value)

    # Strip HTML if present
    text = BeautifulSoup(text, "html.parser").get_text(separator=" ")

    # Decode entities like &amp;
    text = unescape(text)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


def keyword_matches(text, keyword):
    """
    Safer keyword matching:
    - matches phrases like "electric vehicle"
    - matches whole words like "oil", "china", "tesla"
    - avoids tiny substring issues like "ev" matching "every"
    """
    text = text.lower()
    keyword = keyword.lower().strip()

    if not keyword:
        return False

    # For very short keywords, require exact word boundary.
    # Example: "ev" is dangerous, but if you keep it, this prevents matching "every".
    escaped = re.escape(keyword)
    escaped = escaped.replace(r"\ ", r"\s+")

    pattern = rf"(?<![a-zA-Z0-9]){escaped}(?![a-zA-Z0-9])"

    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def find_matches(text, watchlist):
    matches = []

    for category, keywords in watchlist.items():
        for keyword in keywords:
            if keyword_matches(text, keyword):
                matches.append({
                    "category": category,
                    "matched_word": keyword
                })
                break  # one alert per category per post

    return matches


# =============================================================================
# FETCHERS
# =============================================================================

def fetch_latest_posts():
    """
    Returns posts in this format:
    [
      {
        "id": "...",
        "text": "...",
        "url": "...",
        "created_at": "..."
      }
    ]
    """

    if TEST_MODE:
        print("TEST_MODE=true, using fake test post.")
        return fetch_test_posts()

    # 1. Try public archive first because GitHub Actions may be blocked by Truth Social.
    posts = fetch_from_public_archive()
    if posts:
        print(f"Fetched {len(posts)} posts from public archive.")
        return posts[:MAX_POSTS_TO_CHECK]

    # 2. Try Truthbrush public mode.
    posts = fetch_from_truthbrush()
    if posts:
        print(f"Fetched {len(posts)} posts from Truthbrush.")
        return posts[:MAX_POSTS_TO_CHECK]

    # 3. Try RSS last.
    posts = fetch_from_rss()
    if posts:
        print(f"Fetched {len(posts)} posts from RSS.")
        return posts[:MAX_POSTS_TO_CHECK]

    print("All fetch methods failed. Returning empty list.")
    return []


def fetch_test_posts():
    return [
        {
            "id": "test-trump-post-999999",
            "text": (
                "This is a test post about China, tariffs, Tesla, oil, "
                "semiconductors, crypto, Apple, banking, and defense."
            ),
            "url": "https://truthsocial.com/@realDonaldTrump",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    ]


def fetch_from_public_archive():
    try:
        response = requests.get(PUBLIC_ARCHIVE_URL, headers=HEADERS, timeout=25)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"Public archive fetch failed: {e}")
        return []

    if not isinstance(data, list):
        print("Public archive returned non-list data.")
        return []

    posts = []

    for item in data[:MAX_POSTS_TO_CHECK]:
        try:
            post_id = str(item.get("id", "")).strip()
            raw_text = item.get("content") or item.get("text") or ""
            text = clean_html_text(raw_text)
            url = item.get("url") or f"https://truthsocial.com/@realDonaldTrump/{post_id}"
            created_at = item.get("created_at") or datetime.now(timezone.utc).isoformat()

            if not post_id:
                continue

            # Some posts are only images/videos and may have empty text.
            posts.append({
                "id": post_id,
                "text": text,
                "url": url,
                "created_at": created_at,
            })
        except Exception as e:
            print(f"Error parsing archive item: {e}")

    return posts


def parse_json_output(stdout):
    """
    Truthbrush output can vary depending on version.
    This tries:
    1. Whole stdout as JSON list/dict
    2. JSON lines
    """
    stdout = stdout.strip()

    if not stdout:
        return []

    # Whole stdout as JSON
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except Exception:
        pass

    # JSON lines fallback
    items = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            item = json.loads(line)
            items.append(item)
        except Exception:
            continue

    return items


def fetch_from_truthbrush():
    commands = [
        ["truthbrush", "--no-auth", "statuses", TRUTH_HANDLE],
        ["python", "-m", "truthbrush", "--no-auth", "statuses", TRUTH_HANDLE],
    ]

    last_error = None

    for cmd in commands:
        try:
            print("Trying Truthbrush command:", " ".join(cmd))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )

            if result.returncode != 0:
                last_error = result.stderr.strip() or result.stdout.strip()
                print(f"Truthbrush command failed: {last_error[:500]}")
                continue

            raw_items = parse_json_output(result.stdout)

            if not raw_items:
                print("Truthbrush returned no parseable posts.")
                continue

            posts = []

            for item in raw_items[:MAX_POSTS_TO_CHECK]:
                post_id = str(item.get("id") or item.get("uri") or item.get("url") or "").strip()
                raw_text = item.get("content") or item.get("text") or item.get("title") or ""
                text = clean_html_text(raw_text)

                url = (
                    item.get("url")
                    or item.get("uri")
                    or f"https://truthsocial.com/@realDonaldTrump/{post_id}"
                )

                created_at = (
                    item.get("created_at")
                    or item.get("createdAt")
                    or item.get("published")
                    or datetime.now(timezone.utc).isoformat()
                )

                if not post_id:
                    continue

                posts.append({
                    "id": post_id,
                    "text": text,
                    "url": url,
                    "created_at": created_at,
                })

            return posts

        except FileNotFoundError:
            last_error = "truthbrush not installed"
        except Exception as e:
            last_error = str(e)

    print(f"Truthbrush fetch failed: {last_error}")
    return []


def fetch_from_rss():
    try:
        response = requests.get(TRUTH_SOCIAL_RSS, headers=HEADERS, timeout=20)
        response.raise_for_status()
    except Exception as e:
        print(f"RSS request failed: {e}")
        return []

    try:
        soup = BeautifulSoup(response.text, "xml")
    except Exception:
        soup = BeautifulSoup(response.text, "html.parser")

    items = soup.find_all("item") or soup.find_all("entry")
    posts = []

    for item in items[:MAX_POSTS_TO_CHECK]:
        try:
            title_tag = item.find("title")
            link_tag = item.find("link")
            desc_tag = item.find("description") or item.find("content") or item.find("summary")
            date_tag = item.find("pubDate") or item.find("published") or item.find("updated")
            guid_tag = item.find("guid") or item.find("id")

            raw_text = ""
            if desc_tag:
                raw_text = desc_tag.get_text() or desc_tag.string or ""
            elif title_tag:
                raw_text = title_tag.get_text() or title_tag.string or ""

            text = clean_html_text(raw_text)

            url = ""
            if link_tag:
                url = link_tag.get("href") or link_tag.get_text() or ""
            url = url.strip()

            post_id = ""
            if guid_tag:
                post_id = guid_tag.get_text().strip()
            elif url:
                post_id = url.rstrip("/").split("/")[-1]

            created_at = datetime.now(timezone.utc).isoformat()
            if date_tag:
                created_at = date_tag.get_text().strip() or created_at

            if not post_id:
                continue

            posts.append({
                "id": post_id,
                "text": text,
                "url": url,
                "created_at": created_at,
            })

        except Exception as e:
            print(f"Error parsing RSS item: {e}")

    return posts


# =============================================================================
# NOTIFICATIONS
# =============================================================================

def send_ntfy_notification(alert):
    if not NTFY_TOPIC:
        print("No NTFY_TOPIC secret found. Skipping phone notification.")
        return

    title = f"Trump mentioned {alert['category']}"

    message = (
        f"Matched: {alert['matched_word']}\n\n"
        f"{alert['text'][:280]}\n\n"
        f"{alert['url']}"
    )

    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "rotating_light,chart_with_upwards_trend",
            },
            timeout=15,
        )

        if response.ok:
            print(f"ntfy notification sent: {title}")
        else:
            print(f"ntfy failed: HTTP {response.status_code} {response.text[:200]}")

    except Exception as e:
        print(f"ntfy notification error: {e}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("========== Trump Alert Bot Started ==========")
    print("Current UTC time:", datetime.now(timezone.utc).isoformat())
    print("TEST_MODE:", TEST_MODE)
    print("WATCHLIST_FILE:", WATCHLIST_FILE)
    print("ALERTS_FILE:", ALERTS_FILE)
    print("SEEN_FILE:", SEEN_FILE)

    ensure_files_exist()

    watchlist = load_json(WATCHLIST_FILE, {})
    alerts = load_json(ALERTS_FILE, [])
    seen_posts = set(load_json(SEEN_FILE, []))

    print("Loaded watchlist categories:", list(watchlist.keys()))
    print("Existing alerts count:", len(alerts))
    print("Seen posts count:", len(seen_posts))

    if not watchlist:
        print("ERROR: watchlist is empty. No matches can be created.")
        return

    posts = fetch_latest_posts()

    print(f"Fetched posts count: {len(posts)}")

    new_alerts = []

    for post in posts:
        post_id = str(post.get("id", "")).strip()
        text = post.get("text", "") or ""

        if not post_id:
            print("Skipping post with missing ID.")
            continue

        print("---------------------------------------------")
        print("Post ID:", post_id)
        print("Post text preview:", text[:180])

        if post_id in seen_posts:
            print("Already seen. Skipping.")
            continue

        matches = find_matches(text, watchlist)
        print("Matches found:", matches)

        # Mark seen even if there are no matches, so old posts do not get processed forever.
        seen_posts.add(post_id)

        if not matches:
            continue

        for match in matches:
            alert = {
                "post_id": post_id,
                "category": match["category"],
                "matched_word": match["matched_word"],
                "text": text,
                "url": post.get("url", ""),
                "post_created_at": post.get("created_at", ""),
                "alert_created_at": datetime.now(timezone.utc).isoformat(),
            }

            alerts.insert(0, alert)
            new_alerts.append(alert)

            print(
                f"MATCH: [{match['category']}] "
                f"'{match['matched_word']}' in post {post_id}"
            )

    alerts = alerts[:MAX_ALERTS_TO_KEEP]

    save_json(ALERTS_FILE, alerts)
    save_json(SEEN_FILE, sorted(list(seen_posts)))

    print("---------------------------------------------")
    print("Saved alerts count:", len(alerts))
    print("Saved seen posts count:", len(seen_posts))

    for alert in new_alerts:
        send_ntfy_notification(alert)

    print(f"Done. Checked {len(posts)} posts. New alerts: {len(new_alerts)}")
    print("========== Trump Alert Bot Finished ==========")


if __name__ == "__main__":
    main()
