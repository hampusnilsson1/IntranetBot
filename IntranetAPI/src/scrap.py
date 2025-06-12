import logging
import re
from bs4 import BeautifulSoup
import pdfplumber
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import os

import requests

UNWANTED_TAGS = [  # Due to site having no main element and inconsistency
    "header",
    "footer",
    "nav",
    "aside",
    "script",
    "style",
    "noscript",
    "iframe",
]
UNWANTED_CLASSES = [
    "tm-header",
    "tm-header-mobile",
    "tm-footer",
    "tm-sidebar",
    "uk-nav",
    "cookie",
    "search",
    "sidebarmenu",
]
UNWANTED_IDS = ["tm-header", "tm-footer", "tm-sidebar", "cookie", "assistant"]


def scrap_site(page_url, cookie_name, cookie_value):
    # Playwright Test
    with sync_playwright() as p:  # "playwright install" Needed in Dockerfile in future
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        # Set your cookies here
        context.add_cookies(
            [
                {
                    "name": cookie_name,
                    "value": cookie_value,
                    "domain": "intranet.falkenberg.se",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                }
            ]
        )

        page = context.new_page()
        page.goto(page_url)

        # Wait for the JavaScript to load ms
        page.wait_for_timeout(3000)

        # Check if page was loaded successfully
        current_url = page.url
        if current_url.startswith(
            "https://idp.falkenberg.se/saml/authenticate/samldispatchi"
        ):
            print("Error: Invalid cookie. Redirected to login page.")
            browser.close()
            return None

        # Grab the page content and format it
        content = page.content()
        browser.close()

        soup = BeautifulSoup(content, "html.parser")

        title = soup.title.string if soup.title else "No title found"

        main_content = soup.find("div", id="tm-main")
        if not main_content:
            main_content = soup.find("div", class_="tm-page") or soup.find("body")

        # Filter unnecessary elements
        for tag in UNWANTED_TAGS:
            for match in main_content.find_all(tag):
                match.decompose()
        for class_name in UNWANTED_CLASSES:
            for match in soup.find_all(class_=lambda c: c and class_name in c):
                match.decompose()
        for id in UNWANTED_IDS:
            for match in main_content.find_all(id=id):
                match.decompose()

        if main_content:
            for cookie_div in main_content.find_all(
                "div", id=re.compile("cookie", re.IGNORECASE)
            ):
                cookie_div.decompose()
            texts = " ".join(main_content.stripped_strings)
        else:
            texts = "Main or Page not found or empty."

        results = []
        results.append({"url": page_url, "title": title, "texts": texts})

        # Site Pdfs
        pdf_links = soup.find_all(
            "a", href=re.compile(r"(\.pdf$|/file$)", re.IGNORECASE)
        )
        for link in pdf_links:
            pdf_url = link["href"]
            if pdf_url.startswith("/"):
                pdf_url = "https://intranet.falkenberg.se" + pdf_url
            pdf_text = scrap_pdf(pdf_url=pdf_url)
            results.append(
                {
                    "url": pdf_url,
                    "title": link.text.strip() or "No title",
                    "texts": pdf_text,
                    "source_url": page_url,
                }
            )
        return results


def scrap_pdf(pdf_url):
    pdf_file_path = "temp.pdf"
    try:
        response = requests.get(pdf_url)
        response.raise_for_status()

        with open(pdf_file_path, "wb") as f:
            f.write(response.content)

        text_content = []
        with pdfplumber.open(pdf_file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_content.append(page_text)
        return " ".join(text_content) if text_content else "No text found in PDF"
    except (requests.exceptions.RequestException, Exception) as e:
        logging.info(f"Error fetching or processing PDF from {pdf_url}: {str(e)}")
        return "Error fetching or processing PDF"
    finally:
        if os.path.exists(pdf_file_path):
            os.remove(pdf_file_path)
