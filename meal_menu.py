"""웰리브·풀무원 식단 통합 수집.
설치: pip install playwright beautifulsoup4
브라우저 설치: playwright install chromium
실행: python meal_menu.py
웰리브 파싱 구조 참고: https://github.com/junh0601/dsme/blob/16b325ac7c290a2eb37cd7019c935e87d7d90225/menu.py
"""
from playwright.sync_api import sync_playwright
import json
import os
import re
from datetime import datetime, timedelta


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
START_URL = "https://puls.pulmuone.com/plurestaurant/"
STORE_NAME = "한화오션"


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def get_menu_page(playwright):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        )
    )

    try:
        page.goto(START_URL, wait_until="domcontentloaded", timeout=30_000)
        page.locator("#dvStore").click()
        page.locator("#pul-inStoreFind").fill(STORE_NAME)
        page.locator("#pul-imStoreFind").click()
        page.locator("#pul-dvPopStoreListWrapper > div").filter(
            has_text=STORE_NAME
        ).first.click()
        page.wait_for_url("**/main_home.php", timeout=30_000)
        popup_close = page.locator("#dvHomePopClose")
        if popup_close.count() and popup_close.is_visible():
            popup_close.click()
        page.locator("#top-quickMenuMeal").evaluate("element => element.click()")
        page.wait_for_url("**/today.php", timeout=30_000)
        page.locator("#dvPageMove").wait_for(state="visible", timeout=30_000)
        page.wait_for_timeout(1_000)
        page.locator("#dvPageMove").click()
        page.wait_for_url("**/week.php", timeout=30_000)
        page.locator(".classMenu").first.wait_for(timeout=30_000)
        return browser, page
    except Exception as error:
        print(f"현재 페이지: {page.url}")
        print(clean_text(page.locator("body").inner_text())[:1000])
        browser.close()
        print(f"🚨Didn't request from URL: {error}")
        return {"is_error": True, "error_msg": str(error)}, None


def query_menu(page, request_id, endpoint, params):
    """사이트가 사용하는 읽기 전용 식단 조회를 호출한다."""
    response = page.evaluate("""args => new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('식단 조회 시간 초과')), 30000);
        common.serTransaction({requestId:args.id, requestUrl:args.url,
            requestFormId:'frmWeek', requestParam:args.params},
            (id, code, data) => {clearTimeout(timer); resolve({code,data});});
    })""", {"id": request_id, "url": endpoint, "params": params})
    if response["code"] != 1 or not isinstance(response["data"], dict):
        raise RuntimeError(f"식단 조회 실패: {response}")
    return response["data"]


def number_or_none(value):
    if value is None or str(value).strip() == "":
        return None
    return float(str(value).replace(",", ""))


def collect_with_nutrition(page):
    codes = page.evaluate("""() => ({
        oper:document.querySelector('#topOperCd').value,
        assign:document.querySelector('#topAssignCd').value})""")
    params = {"topOperCd": codes["oper"], "topAssignCd": codes["assign"],
              "menuDay": 0, "srchCurShopclsCd": "", "custCd": ""}
    endpoint = "/src/sql/menu/week_sql.php"
    first = query_menu(page, "search_week", endpoint, params)
    result = []
    for index, day in enumerate(first["day"][:7]):
        daily = first if index == 0 else query_menu(
            page, "search_week", endpoint, dict(params, menuDay=day[2]))
        for app_time, label in [("010", "아침"), ("020", "점심"), ("030", "저녁")]:
            record = {"date": f"{day[2][:4]}-{day[2][4:6]}-{day[2][6:]}",
                      "time": label, "menu": {}, "nutrition": []}
            for row in daily["data"]:
                if row[0] != app_time or row[2] != day[2]:
                    continue
                detail = query_menu(page, "search_menuDetail",
                    "/src/sql/menu/nutrient_sql.php", {
                        "srchOperCd": codes["oper"], "srchAssignCd": codes["assign"],
                        "srchMenuDay": row[2], "srchTimeCd": row[11],
                        "srchShopCd": row[13]})
                nutrients = detail.get("data") or []
                items = [{"name": clean_text(n[7]), "kcal": number_or_none(n[8])}
                         for n in nutrients if n[7] is not None]
                site_total = number_or_none(nutrients[0][1]) if nutrients else None
                complete = bool(items) and all(n["kcal"] is not None for n in items)
                item_sum = round(sum(n["kcal"] for n in items), 3) if complete else None
                foods = [clean_text(x) for x in re.split(r"\s+/\s+", row[5] or "") if clean_text(x)]
                record["menu"].setdefault(row[1], []).extend(foods or [row[3]])
                record["nutrition"].append({
                    "corner": row[1], "menu_name": row[3], "items": items,
                    "site_total_kcal": site_total, "items_sum_kcal": item_sum,
                    "total_kcal": site_total if site_total is not None else item_sum,
                    "total_source": "site" if site_total is not None else
                        ("items_sum" if complete else None),
                    "status": "available" if items or site_total is not None else "not_provided"})
            if record["menu"]:
                result.append(record)
    return result


WELLIV_URL = "http://m.welliv.co.kr/mobile/mealmenu_list.jsp"


def collect_welliv():
    from urllib.request import Request, urlopen
    from bs4 import BeautifulSoup
    request = Request(WELLIV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        raw = response.read()
    # 사이트 선언과 실제 인코딩이 다를 수 있어 실제 바이트로 판별.
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        html = raw.decode("cp949")
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("#mainContent .food_sch")
    if container is None:
        raise ValueError("웰리브 식단 영역을 찾지 못했습니다.")
    table = container.find("table", recursive=False)
    if table is None:
        raise ValueError("웰리브 식단 표를 찾지 못했습니다.")
    rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
    records = []
    for row in rows[1:8]:
        cols = row.find_all("td", recursive=False)
        if len(cols) < 4:
            raise ValueError("웰리브 식단 표의 열 구성이 변경되었습니다.")
        date_label = clean_text(cols[0].get_text(" ", strip=True))
        for index, label in enumerate(["아침", "점심", "저녁"], 1):
            menus = {}
            kind = "메뉴"
            for cell in cols[index].find_all("td"):
                if cell.find("td"):
                    continue
                text = clean_text(cell.get_text(" ", strip=True))
                if not text:
                    continue
                if cell.find("span"):
                    kind = text
                    menus.setdefault(kind, [])
                else:
                    menus.setdefault(kind, []).append(text)
            menus = {k: v for k, v in menus.items() if v}
            if menus:
                records.append({"date": date_label, "time": label, "menu": menus})
    if not records:
        raise ValueError("웰리브 식단을 추출하지 못했습니다.")
    return records


def collect_pulmuone():
    with sync_playwright() as playwright:
        browser, page = get_menu_page(playwright)
        if page is None:
            raise RuntimeError(browser["error_msg"])
        try:
            return collect_with_nutrition(page)
        finally:
            browser.close()


def main():
    from pathlib import Path
    from zoneinfo import ZoneInfo
    import sys
    output = {"collected_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
              "welliv": None, "pulmuone": None, "errors": {}}
    for provider, collect in [("welliv", collect_welliv), ("pulmuone", collect_pulmuone)]:
        try:
            output[provider] = collect()
            print(f"{provider}: {len(output[provider])}개 식단 수집")
        except Exception as error:
            output["errors"][provider] = str(error)
            print(f"{provider} 수집 실패: {error}", file=sys.stderr)
    directory = Path(BASE_DIR) / "src"
    directory.mkdir(parents=True, exist_ok=True)
    # 부분 실패 때 이전 정상 파일을 덮어쓰지 않는다.
    name = "meal_menu.partial.json" if output["errors"] else "meal_menu.json"
    path = directory / name
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    print(f"저장: {path}")
    return 1 if output["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

