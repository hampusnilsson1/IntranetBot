import re
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import os


def get_page_details(page_url, cookie_name, cookie_value):
    # Playwright Test
    with sync_playwright() as p:  # I Framtiden i Dockerfile kör "playwright install"
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
        main_content = soup.find("main")
        if main_content:
            for cookie_div in main_content.find_all(
                "div", id=re.compile("cookie", re.IGNORECASE)
            ):
                cookie_div.decompose()
            texts = " ".join(main_content.stripped_strings)
        else:
            texts = "Main tag not found or empty"

        results = []

        results.append({"url": page_url, "title": title, "texts": texts})

        # Alternatively, get all links on the page
        for link in soup.find_all("a"):
            href = link.get("href")
            # Filter out private links
            if href and (
                "/min-profil" in href
                or "/loggaut" in href
                or "/uppdatera-min-profil" in href
                or "/mina-kontakter" in href
                or "/samarbete" in href
                or "/sok-efter-anvandare-och-grupper" in href
                or "/visa-allaanvandare" in href
                # Unsure of this link: https://intranet.falkenberg.se/index.php?option=com_easysocial&view=profile&layout=downloadFile&fileid=7888&tmpl=component
            ):
                continue

            if href and href.startswith("/"):
                href = "https://intranet.falkenberg.se" + href
            if href and (
                href.startswith("https://intranet.falkenberg.se")
                or href.startswith("/")
            ):
                print(href)


# Start
load_dotenv("COOKIE.env")
COOKIE_NAME = os.getenv("COOKIE_NAME")
COOKIE_VALUE = os.getenv("COOKIE_VALUE")

get_page_details(
    page_url="https://intranet.falkenberg.se/start2",
    cookie_name=COOKIE_NAME,
    cookie_value=COOKIE_VALUE,
)
