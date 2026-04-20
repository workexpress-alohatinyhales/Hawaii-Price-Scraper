import os
import time
import gspread
import json
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from google import genai
from dotenv import load_dotenv

load_dotenv()

# Google Sheets Configuration
SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_ID = "1nNOLMAMiJdfMg9l9FfIlzW3DFJSuXcSJQNxaY53TYww"

def init_gemini():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not found in .env file.")
        exit(1)
    return genai.Client(api_key=api_key)

def scrape_page_content(url, page_cache, playwright_page):
    if url in page_cache:
        return page_cache[url]
    
    print(f"Fetching URL: {url}")
    try:
        try:
            playwright_page.goto(url, timeout=30000, wait_until="domcontentloaded")
            playwright_page.wait_for_timeout(2000)
        except PlaywrightTimeoutError:
            print(f"Timeout while loading {url}, but attempting to parse loaded content...")
            
        html = playwright_page.content()
        soup = BeautifulSoup(html, "html.parser")
        for script in soup(["script", "style", "noscript", "header", "footer"]):
            script.extract()
            
        text = soup.get_text(separator=' ', strip=True)
        text = ' '.join(text.split())
        page_cache[url] = text
        return text
    except Exception as e:
        print(f"Failed to fetch {url}: {e}")
        return None

def extract_price_with_llm(client, text, model_name):
    if text and len(text) > 80000:
        text = text[:80000]
        
    prompt = f"""
You are an expert data extractor. I am giving you the text content of a website selling tiny homes.
I need you to find the current price for the following tiny home model.

Model Name: {model_name}

Website Text:
{text}

Return ONLY the price amount. For example, "$55,000". If the model name is not mentioned or the price is not available on this page, return "Not Found". DO NOT include any other text in your response.
"""
    
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model='gemini-flash-latest',
                contents=prompt,
            )
            return response.text.strip()
        except Exception as e:
            print(f"LLM extraction error (Attempt {attempt+1}): {e}")
            time.sleep(15)
            
    return "Error"

def main():
    client = init_gemini()

    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    service_json_string = os.environ.get("SERVICE_ACCOUNT_JSON")
    if not service_json_string:
        print("Error: SERVICE_ACCOUNT_JSON secret not found.")
        return
        
    service_account_info = json.loads(service_json_string)

    print("Authenticating with Google Sheets...")
    gc = gspread.service_account_from_dict(service_account_info)
    
    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
        worksheet = sh.sheet1
    except Exception as e:
        print(f"Failed to open Google Sheet. Make sure you shared it with the service account email! Error: {e}")
        exit(1)
        
    print("Loading data from Google Sheet...")
    records = worksheet.get_all_records()
    headers = worksheet.row_values(1)
    
    try:
        price_col_index = headers.index("Current Price") + 1
    except ValueError:
        print("Could not find 'Current Price' column exactly.")
        exit(1)
        
    page_cache = {}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        for index, row in enumerate(records):
            # Row index in Google Sheets is 1-based, and row 1 is headers.
            # So enumerate index 0 is row 2.
            sheet_row_num = index + 2 
            
            model_name = row.get('Model Name', '')
            url = row.get('Website', '')
            
            if not url or not str(url).startswith('http'):
                print(f"Skipping row {sheet_row_num}: invalid URL.")
                continue
                
            print(f"[{index + 1}/{len(records)}] Processing model '{model_name}'...")
            
            text_content = scrape_page_content(url, page_cache, page)
            if not text_content:
                worksheet.update_cell(sheet_row_num, price_col_index, "Fetch Failed")
                time.sleep(2) # Google Sheets API rate limit safety
                continue
                
            price = extract_price_with_llm(client, text_content, model_name)
            print(f"--> Extracted Price: {price}")
            
            if price == "Error":
                print("Skipping this model due to repeated API errors.")
                worksheet.update_cell(sheet_row_num, price_col_index, "API Error")
                time.sleep(2)
                continue
                
            # Update the specific cell dynamically
            worksheet.update_cell(sheet_row_num, price_col_index, price)
            
            # Rate limit for free tier is 15 RPM, but we saw a limit of 5 for gemini sometimes
            # Google Sheets API also has limits, sleep handles both.
            time.sleep(13)
            
        browser.close()
        
    print("Done! The Google Sheet has been successfully updated.")

if __name__ == "__main__":
    main()
