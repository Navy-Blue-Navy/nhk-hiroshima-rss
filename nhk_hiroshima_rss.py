import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET


SOURCE_URL = "https://news.web.nhk/newsweb/area/340"
OUTPUT_FILE = "nhk_hiroshima.xml"

JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def fetch_page():
    for attempt in range(1, 4):
        try:
            print(f"取得 {attempt}/3: {SOURCE_URL}")

            response = requests.get(
                SOURCE_URL,
                headers=HEADERS,
                timeout=30,
            )

            print(f"HTTP: {response.status_code}")

            response.raise_for_status()

            return response.text

        except requests.RequestException as e:
            print(f"取得失敗: {e}")

            if attempt < 3:
                print("10秒待って再試行します。")
                time.sleep(10)

    return None


def make_guid(link):
    return hashlib.sha256(
        link.encode("utf-8")
    ).hexdigest()


def load_existing_items():
    items = {}

    if not os.path.exists(OUTPUT_FILE):
        return items

    try:
        tree = ET.parse(OUTPUT_FILE)
        root = tree.getroot()

        for item in root.findall("./channel/item"):
            guid = item.findtext("guid", "").strip()

            if not guid:
                continue

            items[guid] = {
                "title": item.findtext("title", ""),
                "link": item.findtext("link", ""),
                "description": item.findtext("description", ""),
                "pubDate": item.findtext("pubDate", ""),
                "guid": guid,
            }

    except Exception as e:
        print(f"既存RSS読込失敗: {e}")

    return items


def parse_nhk_datetime(text):
    now = datetime.now(JST)

    match = re.search(
        r"(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})",
        text,
    )

    if not match:
        return None

    month = int(match.group(1))
    day = int(match.group(2))
    hour = int(match.group(3))
    minute = int(match.group(4))

    year = now.year

    try:
        dt = datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=JST,
        )
    except ValueError:
        return None

    # 年末年始対策。
    # 現在より約1か月以上未来なら前年の記事とみなす。
    if dt > now + timedelta(days=31):
        try:
            dt = dt.replace(
                year=year - 1
            )
        except ValueError:
            return None

    return dt


def remove_datetime_from_title(text):
    return clean_text(
        re.sub(
            r"\s*\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}\s*$",
            "",
            text,
        )
    )


def parse_articles(html):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    articles = []
    seen_links = set()

    # 「広島県の最新ニュース」という見出し／リンクを探す
    marker = None

    for element in soup.find_all(
        ["a", "h2", "h3", "div", "span"]
    ):
        text = clean_text(
            element.get_text(
                " ",
                strip=True,
            )
        )

        if text == "広島県の最新ニュース":
            marker = element
            break

    if marker is None:
        print(
            "「広島県の最新ニュース」の"
            "開始位置を見つけられませんでした。"
        )
        return []

    # marker以降から、NHK記事リンクを順番に取得する。
    # 「広島県の最新ニュースをもっと見る」までを対象とする。
    started = False

    for element in soup.find_all(
        ["a", "h2", "h3", "div", "span"]
    ):
        if element is marker:
            started = True
            continue

        if not started:
            continue

        text = clean_text(
            element.get_text(
                " ",
                strip=True,
            )
        )

        if (
            text == "広島県の最新ニュースをもっと見る"
            or text == "広島県の深掘りコンテンツ"
        ):
            break

        if element.name != "a":
            continue

        href = element.get(
            "href",
            "",
        ).strip()

        if not href:
            continue

        link = urljoin(
            SOURCE_URL,
            href,
        )

        if not re.fullmatch(
            r"https://news\.web\.nhk/newsweb/na/nd-[A-Za-z0-9_-]+/?",
            link,
        ):
            continue

        if link in seen_links:
            continue

        article_date = parse_nhk_datetime(
            text
        )

        if article_date is None:
            continue

        title = remove_datetime_from_title(
            text
        )

        if not title:
            continue

        seen_links.add(link)

        articles.append(
            {
                "title": title,
                "link": link,
                "date": article_date,
            }
        )

    return articles


def build_rss(existing_items, current_articles):
    now = datetime.now(JST)

    merged = dict(existing_items)
    new_count = 0

    for article in current_articles:
        guid = make_guid(
            article["link"]
        )

        if guid in merged:
            old_pubdate = merged[guid].get(
                "pubDate",
                "",
            )

            merged[guid] = {
                "title": article["title"],
                "link": article["link"],
                "description": "NHK 広島県の最新ニュース",
                "pubDate": (
                    old_pubdate
                    if old_pubdate
                    else format_datetime(
                        article["date"]
                    )
                ),
                "guid": guid,
            }

            continue

        merged[guid] = {
            "title": article["title"],
            "link": article["link"],
            "description": "NHK 広島県の最新ニュース",
            "pubDate": format_datetime(
                article["date"]
            ),
            "guid": guid,
        }

        new_count += 1

    def get_pubdate(item):
        try:
            dt = parsedate_to_datetime(
                item["pubDate"]
            )

            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            return dt

        except Exception:
            return datetime(
                1970,
                1,
                1,
                tzinfo=timezone.utc,
            )

    sorted_items = sorted(
        merged.values(),
        key=get_pubdate,
        reverse=True,
    )

    rss = ET.Element(
        "rss",
        version="2.0",
    )

    channel = ET.SubElement(
        rss,
        "channel",
    )

    ET.SubElement(
        channel,
        "title",
    ).text = "NHK 広島県の最新ニュース"

    ET.SubElement(
        channel,
        "link",
    ).text = SOURCE_URL

    ET.SubElement(
        channel,
        "description",
    ).text = (
        "NHKニュース「広島県の最新ニュース」の更新情報"
    )

    ET.SubElement(
        channel,
        "language",
    ).text = "ja"

    ET.SubElement(
        channel,
        "lastBuildDate",
    ).text = format_datetime(now)

    for data in sorted_items:
        item = ET.SubElement(
            channel,
            "item",
        )

        ET.SubElement(
            item,
            "title",
        ).text = data["title"]

        ET.SubElement(
            item,
            "link",
        ).text = data["link"]

        ET.SubElement(
            item,
            "description",
        ).text = data["description"]

        ET.SubElement(
            item,
            "pubDate",
        ).text = data["pubDate"]

        guid_el = ET.SubElement(
            item,
            "guid",
        )

        guid_el.set(
            "isPermaLink",
            "false",
        )

        guid_el.text = data["guid"]

    tree = ET.ElementTree(rss)

    ET.indent(
        tree,
        space="  ",
    )

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    return len(sorted_items), new_count


def main():
    existing_items = load_existing_items()

    print(
        f"既存RSS件数: {len(existing_items)}"
    )

    html = fetch_page()

    if html is None:
        if existing_items:
            print(
                "NHKを取得できなかったため、"
                "既存XMLを維持します。"
            )
            return

        raise RuntimeError(
            "NHKを取得できず、"
            "既存XMLもありません。"
        )

    articles = parse_articles(html)

    print(
        f"広島県の最新ニュース取得件数: "
        f"{len(articles)}"
    )

    if len(articles) < 5:
        if existing_items:
            print(
                "取得件数が異常に少ないため、"
                "既存XMLを維持します。"
            )
            return

        raise RuntimeError(
            f"取得件数が少なすぎます: "
            f"{len(articles)}"
        )

    print()
    print("取得記事:")

    for i, article in enumerate(
        articles,
        1,
    ):
        print(
            f"{i:2d}. "
            f"{article['date'].strftime('%Y-%m-%d %H:%M')} "
            f"{article['title']}"
        )

        print(
            f"    {article['link']}"
        )

    total_count, new_count = build_rss(
        existing_items,
        articles,
    )

    print()
    print(
        f"今回の新規記事: {new_count}"
    )

    print(
        f"RSS保存件数: {total_count}"
    )

    print(
        f"保存先: "
        f"{os.path.abspath(OUTPUT_FILE)}"
    )


if __name__ == "__main__":
    main()