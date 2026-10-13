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


SOURCE_URL = (
    "https://news.web.nhk/newsweb/pl/"
    "news-nwa-all-local340-weekly"
)

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
        "application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


def clean_text(text):
    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def make_guid(link):
    return hashlib.sha256(
        link.encode("utf-8")
    ).hexdigest()


def fetch_page(url):
    for attempt in range(1, 4):
        try:
            print(
                f"取得 {attempt}/3: {url}"
            )

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=30,
            )

            print(
                f"HTTP: {response.status_code}"
            )

            response.raise_for_status()

            return response.text

        except requests.RequestException as e:
            print(
                f"取得失敗: {e}"
            )

            if attempt < 3:
                print(
                    "10秒待って再試行します。"
                )
                time.sleep(10)

    return None


def load_existing_items():
    items = {}

    if not os.path.exists(
        OUTPUT_FILE
    ):
        return items

    try:
        tree = ET.parse(
            OUTPUT_FILE
        )

        root = tree.getroot()

        for item in root.findall(
            "./channel/item"
        ):
            guid = (
                item.findtext(
                    "guid",
                    "",
                ).strip()
            )

            link = (
                item.findtext(
                    "link",
                    "",
                ).strip()
            )

            if not guid and link:
                guid = make_guid(link)

            if not guid:
                continue

            items[guid] = {
                "title": item.findtext(
                    "title",
                    "",
                ),
                "link": link,
                "description": item.findtext(
                    "description",
                    "",
                ),
                "pubDate": item.findtext(
                    "pubDate",
                    "",
                ),
                "guid": guid,
            }

    except Exception as e:
        print(
            f"既存RSS読込失敗: {e}"
        )

    return items


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(
            value
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=JST
            )

        return dt

    except ValueError:
        return None


def parse_article_from_link(a):
    href = a.get(
        "href",
        "",
    ).strip()

    if not href:
        return None

    link = urljoin(
        SOURCE_URL,
        href,
    )

    if not re.fullmatch(
        r"https://news\.web\.nhk/"
        r"newsweb/na/"
        r"nd-[A-Za-z0-9_-]+/?",
        link,
    ):
        return None

    # 広島県一覧の記事には、
    # リンク内に記事タイトルのp要素と
    # time datetime属性がある。
    title_element = a.find("p")
    time_element = a.find(
        "time",
        datetime=True,
    )

    if (
        title_element is None
        or time_element is None
    ):
        return None

    title = clean_text(
        title_element.get_text(
            " ",
            strip=True,
        )
    )

    dt = parse_iso_datetime(
        time_element.get(
            "datetime",
            "",
        )
    )

    if not title or dt is None:
        return None

    return {
        "title": title,
        "link": link,
        "date": dt,
    }


def find_local_news_links(soup):
    """
    ページ上部の
    「過去1週間分のニュース（広島県）」
    の一覧だけを抽出する。

    下部にある「新着ニュース」などの
    全国ニュースは除外する。
    """

    articles = []

    # ページ本文には
    # 「1〜12 件目表示 / 58 件」
    # のような件数表示がある。
    # その前に並んでいる記事一覧を対象にする。

    for a in soup.find_all(
        "a",
        href=True,
    ):
        article = parse_article_from_link(
            a
        )

        if article is None:
            continue

        # 広島県一覧の記事は li の中にある。
        li = a.find_parent("li")

        if li is None:
            continue

        # 全国新着ニュース等との混入を避けるため、
        # 記事リンクから後方を調べ、
        # 一覧件数表示より前にあるものだけ採用する。
        articles.append(article)

    # HTML上では広島県一覧が先に並び、
    # その後に全国の「新着ニュース」が続く。
    #
    # 「1〜12 件目表示 / XX 件」の表示件数を利用して
    # そのページの広島県記事数を決定する。

    body_text = clean_text(
        soup.get_text(
            " ",
            strip=True,
        )
    )

    match = re.search(
        r"(\d+)\s*[〜～~-]\s*(\d+)"
        r"\s*件目表示\s*/\s*(\d+)\s*件",
        body_text,
    )

    if match:
        start_number = int(
            match.group(1)
        )

        end_number = int(
            match.group(2)
        )

        expected_count = (
            end_number
            - start_number
            + 1
        )

        if len(articles) >= expected_count:
            articles = articles[
                :expected_count
            ]

    return articles


def find_next_page(soup, current_url):
    for a in soup.find_all(
        "a",
        href=True,
    ):
        text = clean_text(
            a.get_text(
                " ",
                strip=True,
            )
        )

        if text != "次へ":
            continue

        next_url = urljoin(
            current_url,
            a["href"],
        )

        if (
            "news-nwa-all-local340-weekly"
            in next_url
        ):
            return next_url

    return None


def crawl_weekly_news():
    all_articles = {}
    visited_urls = set()

    current_url = SOURCE_URL
    page_number = 1

    while current_url:
        if current_url in visited_urls:
            print(
                "同じページURLを再検出したため"
                "終了します。"
            )
            break

        if page_number > 10:
            print(
                "安全のため10ページで"
                "巡回を終了します。"
            )
            break

        visited_urls.add(
            current_url
        )

        print()
        print(
            f"=== 一覧ページ {page_number} ==="
        )

        html = fetch_page(
            current_url
        )

        if html is None:
            return None

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        page_articles = (
            find_local_news_links(
                soup
            )
        )

        print(
            f"広島県記事: "
            f"{len(page_articles)}件"
        )

        if len(page_articles) == 0:
            print(
                "広島県の記事を"
                "取得できませんでした。"
            )
            return None

        for article in page_articles:
            all_articles[
                article["link"]
            ] = article

        next_url = find_next_page(
            soup,
            current_url,
        )

        if not next_url:
            break

        current_url = next_url
        page_number += 1

        time.sleep(1)

    articles = list(
        all_articles.values()
    )

    articles.sort(
        key=lambda x: x["date"],
        reverse=True,
    )

    return articles


def build_rss(
    existing_items,
    current_articles,
):
    now = datetime.now(JST)

    merged = dict(
        existing_items
    )

    new_count = 0

    for article in current_articles:
        guid = make_guid(
            article["link"]
        )

        if guid not in merged:
            new_count += 1

        merged[guid] = {
            "title": article["title"],
            "link": article["link"],
            "description": (
                "NHK 過去1週間分のニュース"
                "（広島県）"
            ),
            "pubDate": format_datetime(
                article["date"]
            ),
            "guid": guid,
        }

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
    ).text = (
        "NHK 広島県のニュース"
    )

    ET.SubElement(
        channel,
        "link",
    ).text = SOURCE_URL

    ET.SubElement(
        channel,
        "description",
    ).text = (
        "NHKニュース "
        "過去1週間分のニュース（広島県）"
    )

    ET.SubElement(
        channel,
        "language",
    ).text = "ja"

    ET.SubElement(
        channel,
        "lastBuildDate",
    ).text = format_datetime(
        now
    )

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
        ).text = data[
            "description"
        ]

        ET.SubElement(
            item,
            "pubDate",
        ).text = data[
            "pubDate"
        ]

        guid_element = ET.SubElement(
            item,
            "guid",
        )

        guid_element.set(
            "isPermaLink",
            "false",
        )

        guid_element.text = data[
            "guid"
        ]

    tree = ET.ElementTree(
        rss
    )

    ET.indent(
        tree,
        space="  ",
    )

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    return (
        len(sorted_items),
        new_count,
    )


def main():
    existing_items = (
        load_existing_items()
    )

    print(
        f"既存RSS件数: "
        f"{len(existing_items)}"
    )

    articles = (
        crawl_weekly_news()
    )

    if articles is None:
        if existing_items:
            print()
            print(
                "NHKの取得に失敗したため、"
                "既存XMLを維持します。"
            )
            return

        raise RuntimeError(
            "NHKの取得に失敗し、"
            "既存XMLもありません。"
        )

    print()
    print(
        "=== 取得結果 ==="
    )

    print(
        f"広島県ニュース総取得件数: "
        f"{len(articles)}"
    )

    # 過去1週間ページなので、
    # 通常は数十件ある。
    if len(articles) < 20:
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
    print(
        "最新20件:"
    )

    for i, article in enumerate(
        articles[:20],
        1,
    ):
        print(
            f"{i:2d}. "
            f"{article['date'].strftime('%Y-%m-%d %H:%M:%S')} "
            f"{article['title']}"
        )

        print(
            f"    "
            f"{article['link']}"
        )

    total_count, new_count = (
        build_rss(
            existing_items,
            articles,
        )
    )

    print()
    print(
        f"今回の新規記事: "
        f"{new_count}"
    )

    print(
        f"RSS保存件数: "
        f"{total_count}"
    )

    print(
        f"保存先: "
        f"{os.path.abspath(OUTPUT_FILE)}"
    )


if __name__ == "__main__":
    main()
