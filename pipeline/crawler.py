"""
STEP 1. ALIO 연구보고서 PDF 크롤링 (함수형 리팩터링)

- `run(...)` 을 import해서 직접 호출하거나, CLI로도 실행 가능.
- 진행 상황은 `on_log` 콜백으로 전달됨.
- Selenium은 로컬에선 Chrome, Streamlit Cloud에선 chromium(apt) 자동 감지.
"""
from __future__ import annotations

import os
import re
import csv
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

import requests

from .utils import sanitize_filename, ensure_dir

BASE_URL = "https://alio.go.kr"
ORGAN_LIST_API = "https://alio.go.kr/item/itemOrganListSusi.json"
REPORT_ROOT_NO = "B1040"

LogFn = Callable[[str], None]


# ────────────────────────────────────────────────────────────
# ALIO API: 기관명 → apbaId
# ────────────────────────────────────────────────────────────
def resolve_organ_url(organ_name: str) -> tuple[str, str, str]:
    """기관명으로 ALIO 검색해서 (list_url, apba_id, apba_na) 반환."""
    payload = {
        "apbaType": [], "jidtDptm": [], "area": [],
        "apbaId": "", "apbaNa": organ_name,
        "reportFormRootNo": REPORT_ROOT_NO,
    }
    headers = {"Content-Type": "application/json;charset=UTF-8", "Referer": BASE_URL}
    resp = requests.post(ORGAN_LIST_API, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json() or {}
    items = (data.get("data") or {}).get("organList", []) or []
    if not items:
        raise RuntimeError(f"기관 '{organ_name}'을 찾지 못했습니다.")

    exact = [x for x in items if x.get("apbaNa") == organ_name]
    organ = exact[0] if exact else items[0]
    apba_id = organ.get("apbaId", "")
    apba_na = organ.get("apbaNa", organ_name)
    if not apba_id:
        raise RuntimeError(f"기관 ID를 찾지 못했습니다: {organ}")

    list_url = f"{BASE_URL}/item/itemOrganList.do?apbaId={apba_id}&reportFormRootNo={REPORT_ROOT_NO}"
    return list_url, apba_id, apba_na


# ────────────────────────────────────────────────────────────
# Chrome/Chromium 자동 감지 (로컬/Cloud 호환)
# ────────────────────────────────────────────────────────────
def _find_chromium_binary() -> Optional[str]:
    """Streamlit Cloud에서는 /usr/bin/chromium 을 쓴다."""
    for path in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                 "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if os.path.exists(path):
            return path
    which = shutil.which("chromium") or shutil.which("chromium-browser") \
            or shutil.which("google-chrome")
    return which


def build_driver(headless: bool = True):
    """Selenium 드라이버 생성. Cloud/로컬 모두 대응."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")

    # ── Streamlit Cloud / 컨테이너 필수 플래그 ──
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-breakpad")
    opts.add_argument("--disable-client-side-phishing-detection")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-hang-monitor")
    opts.add_argument("--disable-ipc-flooding-protection")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--disable-prompt-on-repost")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--disable-sync")
    opts.add_argument("--force-color-profile=srgb")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--no-first-run")
    opts.add_argument("--enable-automation")
    opts.add_argument("--password-store=basic")
    opts.add_argument("--use-mock-keychain")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--remote-debugging-port=9222")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--lang=ko-KR")

    binary = _find_chromium_binary()
    if binary:
        opts.binary_location = binary

    driver_path = shutil.which("chromedriver") or "/usr/bin/chromedriver"
    if os.path.exists(driver_path):
        return webdriver.Chrome(service=Service(driver_path), options=opts)
    return webdriver.Chrome(options=opts)

def _session_from_driver(driver) -> requests.Session:
    sess = requests.Session()
    for c in driver.get_cookies():
        sess.cookies.set(c["name"], c["value"])
    try:
        sess.headers.update({"User-Agent": driver.execute_script("return navigator.userAgent;")})
    except Exception:
        pass
    return sess


# ────────────────────────────────────────────────────────────
# 다운로드 로직
# ────────────────────────────────────────────────────────────
_CTYPE_TO_EXT = {
    "application/pdf": ".pdf",
    "haansofthwp": ".hwp",
    "hwpx": ".hwpx",
    "msword": ".doc",
    "officedocument.wordprocessing": ".docx",
    "officedocument.spreadsheet": ".xlsx",
    "officedocument.presentation": ".pptx",
}


def _ext_from_ctype(ctype: str, default: str = ".pdf") -> str:
    ctype = (ctype or "").lower()
    for key, ext in _CTYPE_TO_EXT.items():
        if key in ctype:
            return ext
    return default


def _save_stream(resp: requests.Response, out_path: Path) -> None:
    resp.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=262144):
            if chunk:
                f.write(chunk)


def _download_via_alio(sess: requests.Session, href: str, out_path: Path) -> Path:
    """ALIO download.json / 직접 PDF / JSON 리다이렉트 모두 대응."""
    url1 = urljoin(BASE_URL, href)
    r1 = sess.get(url1, stream=True, allow_redirects=True, timeout=60)
    ctype = (r1.headers.get("Content-Type") or "").lower()

    if "application/pdf" in ctype or url1.lower().endswith(".pdf"):
        _save_stream(r1, out_path)
        return out_path

    if any(t in ctype for t in ("haansofthwp", "hwp", "msword", "officedocument")):
        ext = _ext_from_ctype(ctype)
        actual = Path(str(out_path).rsplit(".pdf", 1)[0] + ext)
        _save_stream(r1, actual)
        return actual

    if "json" in ctype:
        data = r1.json()
        def _find(obj: dict) -> Optional[str]:
            for k in ("url", "downUrl", "downloadUrl", "fileUrl", "link", "path"):
                v = obj.get(k)
                if isinstance(v, str) and v:
                    return v
            return None
        cand = _find(data) or (_find(data["data"]) if isinstance(data.get("data"), dict) else None)
        if not cand:
            raise RuntimeError(f"download.json에서 URL 탐색 실패: {data}")
        r2 = sess.get(urljoin(BASE_URL, cand), stream=True, allow_redirects=True, timeout=60)
        _save_stream(r2, out_path)
        return out_path

    raise RuntimeError(f"예상치 못한 Content-Type={ctype}")


def _find_pdfs_in_html(html: str, base: str) -> list[str]:
    found: set[str] = set()
    for m in re.finditer(r'href=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE):
        href = m.group(1).strip()
        if ".pdf" in href.lower():
            found.add(urljoin(base, href))
    return sorted(found)


def _download_external_pdfs(sess: requests.Session, page_url: str,
                             prefix: str, out_dir: Path, max_files: int = 10) -> list[Path]:
    r = sess.get(page_url, timeout=60)
    r.raise_for_status()
    saved: list[Path] = []
    for idx, pdf_url in enumerate(_find_pdfs_in_html(r.text, page_url)[:max_files], start=1):
        safe = sanitize_filename(f"{prefix}_external_{idx}")
        out_path = out_dir / f"{safe}.pdf"
        rr = sess.get(pdf_url, stream=True, allow_redirects=True, timeout=60)
        ctype = (rr.headers.get("Content-Type") or "").lower()
        if "application/pdf" not in ctype and not pdf_url.lower().endswith(".pdf"):
            continue
        _save_stream(rr, out_path)
        saved.append(out_path)
    return saved


# ────────────────────────────────────────────────────────────
# 메인 크롤링
# ────────────────────────────────────────────────────────────
@dataclass
class CrawlResult:
    organ_name: str
    apba_id: str
    out_dir: Path
    downloaded: list[Path] = field(default_factory=list)
    log_csv: Optional[Path] = None
    errors: list[str] = field(default_factory=list)


def run(
    organ_name: str,
    out_dir: str | Path,
    *,
    headless: bool = True,
    max_pages: Optional[int] = None,
    crawl_external_pdf: bool = True,
    log_csv: Optional[str | Path] = None,
    on_log: Optional[LogFn] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> CrawlResult:
    """
    ALIO 크롤링 실행. 
    
    Args:
        organ_name: ALIO 기관명 (예: "한국수자원공사")
        out_dir: PDF 저장 폴더
        headless: Chrome 헤드리스 모드 (Cloud에선 True 필수)
        max_pages: 최대 크롤링 페이지 수 (None이면 전체)
        crawl_external_pdf: 외부 링크의 PDF도 스캔할지
        log_csv: 상세 로그 CSV 경로 (None이면 out_dir 내 자동 생성)
        on_log: 로그 라인 콜백 `fn(line: str)`
        on_progress: 진행 콜백 `fn(current, total, message)` — total이 0이면 페이지 기반
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    log = on_log or (lambda s: print(s, flush=True))
    progress = on_progress or (lambda c, t, m: None)

    out_path = ensure_dir(out_dir)
    log(f"기관 검색 중: {organ_name}")
    list_url, apba_id, apba_na = resolve_organ_url(organ_name)
    log(f"기관명: {apba_na} / ID: {apba_id}")
    log(f"저장 폴더: {out_path}")

    result = CrawlResult(organ_name=apba_na, apba_id=apba_id, out_dir=out_path)

    if log_csv is None:
        log_csv = out_path / f"_crawl_log_{apba_id}.csv"
    log_csv = Path(log_csv)
    new_file = not log_csv.exists()
    log_fp = open(log_csv, "a", newline="", encoding="utf-8-sig")
    log_writer = csv.writer(log_fp)
    if new_file:
        log_writer.writerow(["ts", "page", "idx", "title", "date",
                             "link_type", "source", "status", "info"])
    result.log_csv = log_csv

    now_ts = lambda: time.strftime("%Y-%m-%d %H:%M:%S")
    driver = None
    try:
        driver = build_driver(headless=headless)
        wait = WebDriverWait(driver, 20)

        driver.get(list_url)
        # 조회 결과 실제 로딩까지 최대 15초 재시도
        deadline = time.time() + 15
        while time.time() < deadline:
            lis = driver.find_elements(By.CSS_SELECTOR, ".list-inner ul li")
            if lis and "조회 결과가 없습니다" not in (lis[0].text or ""):
                break
            time.sleep(0.5)
        sess = _session_from_driver(driver)

        visited: set[str] = set()
        page_no = 1
        total_downloaded = 0

        while True:
            if max_pages and page_no > max_pages:
                log(f"\n=== max_pages={max_pages} 도달 ===")
                break

            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, ".list-inner ul li"))
            lis = driver.find_elements(By.CSS_SELECTOR, ".list-inner ul li")
            log(f"\n=== PAGE {page_no} / items={len(lis)} ===")
            progress(total_downloaded, 0, f"페이지 {page_no} ({len(lis)}개 항목)")

            for i in range(len(lis)):
                # 매 루프마다 재조회 (DOM stale 방지)
                lis = driver.find_elements(By.CSS_SELECTOR, ".list-inner ul li")
                if i >= len(lis):
                    break
                li = lis[i]
                li_text = (li.text or "").strip()
                if "조회 결과가 없습니다" in li_text:
                    continue

                try:
                    title = li.find_element(By.CSS_SELECTOR, "span.tit").text.strip()
                except Exception:
                    title = li_text.split("\n")[0].strip()
                try:
                    date = li.find_element(By.CSS_SELECTOR, "span.date").text.strip()
                except Exception:
                    date = ""

                log(f"[{page_no}-{i+1}/{len(lis)}] {title} ({date})")
                progress(total_downloaded, 0, f"P{page_no}-{i+1} {title[:40]}")

                main_handle = driver.current_window_handle
                try:
                    anchor = li.find_element(By.TAG_NAME, "a")
                    driver.execute_script("arguments[0].click();", anchor)
                except Exception:
                    driver.execute_script("arguments[0].click();", li)

                # 팝업 대기
                new_handle = None
                end = time.time() + 15
                while time.time() < end:
                    for h in driver.window_handles:
                        if h != main_handle:
                            new_handle = h; break
                    if new_handle: break
                    time.sleep(0.3)

                if not new_handle:
                    log_writer.writerow([now_ts(), page_no, i+1, title, date,
                                         "detail", "", "SKIP", "새 창 열리지 않음"])
                    log_fp.flush(); continue

                driver.switch_to.window(new_handle)
                try:
                    WebDriverWait(driver, 15).until(
                        lambda d: d.find_elements(By.CSS_SELECTOR, "a[href*='download.json']")
                    )
                except Exception:
                    pass

                anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='download.json']")
                if not anchors:
                    anchors = driver.find_elements(By.CSS_SELECTOR, ".bt-list p a")

                detail_links = []
                for a in anchors:
                    href = (a.get_attribute("href") or "").strip()
                    text = (a.text or "").strip()
                    if href and not href.lower().startswith("javascript:"):
                        detail_links.append((text, href))

                try: driver.close()
                except Exception: pass
                driver.switch_to.window(main_handle)

                if not detail_links:
                    log_writer.writerow([now_ts(), page_no, i+1, title, date,
                                         "detail", "", "SKIP", "유효 링크 없음"])
                    log_fp.flush(); continue

                for (text, href) in detail_links:
                    label = text or href
                    safe_prefix = sanitize_filename(f"{title}_{date}_{label}")
                    out_pdf = out_path / f"{safe_prefix}.pdf"

                    link_type = ""
                    if "/download/download.json?fileNo=" in href:
                        link_type = "alio_download_json"
                    elif href.lower().endswith(".pdf"):
                        link_type = "direct_pdf"
                    else:
                        link_type = "external_page"

                    if link_type in ("alio_download_json", "direct_pdf"):
                        if href in visited:
                            log_writer.writerow([now_ts(), page_no, i+1, title, date,
                                                 link_type, href, "SKIP", "중복"])
                            log_fp.flush(); continue
                        visited.add(href)
                        try:
                            saved = _download_via_alio(sess, href, out_pdf)
                            log(f"  - saved: {saved.name}")
                            log_writer.writerow([now_ts(), page_no, i+1, title, date,
                                                 link_type, href, "OK", str(saved)])
                            result.downloaded.append(saved)
                            total_downloaded += 1
                        except Exception as e:
                            log(f"  - FAIL: {e}")
                            log_writer.writerow([now_ts(), page_no, i+1, title, date,
                                                 link_type, href, "FAIL", str(e)])
                            result.errors.append(f"{title}: {e}")
                        log_fp.flush()
                    else:
                        if not crawl_external_pdf:
                            log_writer.writerow([now_ts(), page_no, i+1, title, date,
                                                 link_type, href, "SKIP", "외부 크롤 비활성"])
                            log_fp.flush(); continue
                        try:
                            saved_list = _download_external_pdfs(
                                sess, href, safe_prefix, out_path, max_files=10)
                            if saved_list:
                                for p in saved_list:
                                    log(f"  - external saved: {p.name}")
                                    log_writer.writerow([now_ts(), page_no, i+1, title, date,
                                                         "external_page_pdf", href, "OK", str(p)])
                                    result.downloaded.append(p)
                                    total_downloaded += 1
                            else:
                                log_writer.writerow([now_ts(), page_no, i+1, title, date,
                                                     link_type, href, "SKIP", "PDF 후보 없음"])
                        except Exception as e:
                            log(f"  - external FAIL: {e}")
                            log_writer.writerow([now_ts(), page_no, i+1, title, date,
                                                 link_type, href, "FAIL", str(e)])
                            result.errors.append(f"{title} (external): {e}")
                        log_fp.flush()

            # 페이지 이동
            next_page = page_no + 1
            moved = False
            try:
                xpath = f'//a[normalize-space()="{next_page}"]|//button[normalize-space()="{next_page}"]'
                btn = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                driver.execute_script("arguments[0].click();", btn)
                wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, ".list-inner ul li"))
                moved = True
                log(f"  -> 숫자 버튼으로 {next_page}페이지 이동")
            except Exception:
                pass
            if not moved:
                try:
                    nxt = driver.find_element(By.CSS_SELECTOR, "a.nxt-bt")
                    driver.execute_script("arguments[0].click();", nxt)
                    wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, ".list-inner ul li"))
                    moved = True
                    log(f"  -> 다음 버튼으로 {next_page}페이지 이동")
                except Exception:
                    pass
            if not moved:
                log("\n=== 마지막 페이지 도달 ===")
                break
            page_no += 1
            time.sleep(0.4)

    finally:
        if driver is not None:
            try: driver.quit()
            except Exception: pass
        log_fp.close()
        log(f"\n로그 저장: {log_csv}  |  PDF {len(result.downloaded)}개 수집")

    return result


# ────────────────────────────────────────────────────────────
# CLI (대시보드에서 subprocess로 호출할 때의 하위 호환)
# ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from datetime import datetime as _dt
    _today = os.environ.get("RUN_DATE", _dt.now().strftime("%Y%m%d"))
    organ = os.environ.get("CRAWL_ORG_NAME", "한국수자원공사")
    safe_organ = organ.strip().replace(" ", "_")
    out_dir = os.environ.get("DOWNLOAD_DIR", f"./downloads_{safe_organ}_{_today}")
    headless_env = os.environ.get("HEADLESS", "").strip().lower()
    headless = headless_env in ("1", "true", "yes") or not headless_env == "0"
    if headless_env == "":
        # 로컬 기본은 비헤드리스(사용자 시인성), Cloud에선 HEADLESS=1 설정
        headless = False

    run(organ, out_dir, headless=headless)
