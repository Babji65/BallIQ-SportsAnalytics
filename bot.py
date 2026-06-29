import json
import os
import re
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

WATCHLIST_FILE = "watchlist.json"
ALERTS_FILE = "alerts.json"
SEEN_FILE = "seen_posts.json"

NTFY_TOPIC = os.getenv("NTFY_TOPIC")

# Truth Social public RSS feed for Trump's account
TRUTH_SOCIAL_RSS = "https://truthsocial.com/@realDonaldTrump.rss"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Truth Social scraper
# ---------------------------------------------------------------------------

def fetch_latest_posts():
   return [
    {
        "id": "test-trump-post-001",
        "text": "This is a test post about China, tariffs, Tesla, oil, and semiconductors.",
        "url": "https://truthsocial.com/@realDonaldTrump",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


    
def _fetch_via_rss():
    """Primary method: parse the public RSS/Atom feed."""
    try:
        resp = requests.get(TRUTH_SOCIAL_RSS, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"RSS request failed: {e}")
        return []

    try:
        soup = BeautifulSoup(resp.text, "xml")
    except Exception:
        # lxml may not be available; fall back to html.parser
        soup = BeautifulSoup(resp.text, "html.parser")

    items = soup.find_all("item")
    if not items:
        # Try Atom <entry> tags
        items = soup.find_all("entry")

    posts = []
    for item in items:
        try:
            # Extract fields — tag names differ slightly between RSS 2 and Atom
            title_tag = item.find("title")
            link_tag = item.find("link")
            desc_tag = item.find("description") or item.find("content") or item.find("summary")
            date_tag = item.find("pubDate") or item.find("published") or item.find("updated")
            guid_tag = item.find("guid") or item.find("id")

            raw_text = ""
            if desc_tag and desc_tag.string:
                raw_text = desc_tag.string
            elif title_tag and title_tag.string:
                raw_text = title_tag.string

            # Strip any embedded HTML tags from the post body
            clean_text = BeautifulSoup(raw_text, "html.parser").get_text(separator=" ").strip()

            url = ""
            if link_tag:
                url = link_tag.get("href") or link_tag.string or ""
            url = url.strip()

            post_id = ""
            if guid_tag and guid_tag.string:
                post_id = guid_tag.string.strip()
            elif url:
                # Derive ID from the numeric part of the URL, e.g. /statuses/123456
                m = re.search(r"/(\d+)$", url)
                post_id = m.group(1) if m else url

            created_at = datetime.now(timezone.utc).isoformat()
            if date_tag and date_tag.string:
                try:
                    # RSS dates are usually RFC 2822; Python 3.3+ can parse them
                    from email.utils import parsedate_to_datetime
                    created_at = parsedate_to_datetime(date_tag.string.strip()).isoformat()
                except Exception:
                    pass  # leave as now()

            if not clean_text or not post_id:
                continue

            posts.append({
                "id": post_id,
                "text": clean_text,
                "url": url,
                "created_at": created_at,
            })
        except Exception as e:
            print(f"Error parsing RSS item: {e}")
            continue

    return posts


def _fetch_via_html():
    """
    Fallback: scrape the public HTML profile page.
    Truth Social renders posts server-side at /@realDonaldTrump
    """
    profile_url = "https://truthsocial.com/@realDonaldTrump"
    try:
        resp = requests.get(profile_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"HTML scrape request failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    posts = []

    # Truth Social uses Mastodon-style markup; statuses live in <div class="status__content">
    # or similar containers. We look for any <article> or <div data-id="...">
    candidates = soup.find_all(attrs={"data-id": True}) or soup.find_all("article")

    for el in candidates:
        try:
            post_id = el.get("data-id", "")

            # Post body
            body_el = el.find(class_=re.compile(r"status__content|e-content|post-body", re.I))
            if not body_el:
                body_el = el
            clean_text = body_el.get_text(separator=" ").strip()

            # Link to post
            link_el = el.find("a", href=re.compile(r"/statuses/\d+|/@\w+/\d+"))
            url = ""
            if link_el:
                href = link_el.get("href", "")
                url = href if href.startswith("http") else f"https://truthsocial.com{href}"
                if not post_id:
                    m = re.search(r"/(\d+)$", url)
                    post_id = m.group(1) if m else url

            if not clean_text or not post_id:
                continue

            posts.append({
                "id": post_id,
                "text": clean_text,
                "url": url or profile_url,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            print(f"Error parsing HTML element: {e}")
            continue

    return posts


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def find_matches(text, watchlist):
    text_lower = text.lower()
    matches = []
    seen_categories = set()

    for category, keywords in watchlist.items():
        for keyword in keywords:
            if keyword.lower() in text_lower and category not in seen_categories:
                matches.append({
                    "category": category,
                    "matched_word": keyword,
                })
                seen_categories.add(category)
                break  # one match per category per post is enough

    return matches


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_ntfy_notification(alert):
    if not NTFY_TOPIC:
        print("No NTFY_TOPIC set. Skipping notification.")
        return

    title = f"Trump mentioned {alert['category']}"
    message = (
        f"Matched: \"{alert['matched_word']}\"\n\n"
        f"{alert['text'][:280]}\n\n"
        f"{alert['url']}"
    )

    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "rotating_light,chart_with_upwards_trend",
            },
            timeout=10,
        )
        print(f"  → ntfy notification sent: {title}")
    except requests.RequestException as e:
        print(f"  → ntfy notification failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    watchlist = load_json(WATCHLIST_FILE, {})
    alerts = load_json(ALERTS_FILE, [])
    seen_posts = set(load_json(SEEN_FILE, []))

    posts = fetch_latest_posts()
    new_alerts = []

    for post in posts:
        post_id = str(post["id"])

        if post_id in seen_posts:
            print(f"  Skipping already-seen post {post_id}")
            continue

        seen_posts.add(post_id)
        matches = find_matches(post["text"], watchlist)

        for match in matches:
            alert = {
                "post_id": post_id,
                "category": match["category"],
                "matched_word": match["matched_word"],
                "text": post["text"],
                "url": post["url"],
                "post_created_at": post["created_at"],
                "alert_created_at": datetime.now(timezone.utc).isoformat(),
            }
            alerts.insert(0, alert)
            new_alerts.append(alert)
            print(f"  MATCH: [{match['category']}] \"{match['matched_word']}\" in post {post_id}")

    # Keep only the 200 most recent alerts
    alerts = alerts[:200]

    save_json(ALERTS_FILE, alerts)
    save_json(SEEN_FILE, list(seen_posts))

    for alert in new_alerts:
        send_ntfy_notification(alert)

    print(f"\nDone. Checked {len(posts)} posts. New alerts: {len(new_alerts)}")


if __name__ == "__main__":
    main()
