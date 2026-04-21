import os
import time
import gspread
import json
import re
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

Formatting Rules:
1. If the price uses a "k" or "K" suffix (e.g., "$199k" or "$199K"), convert it to the full number with commas (e.g., "$199,000").
2. If the price is a range with "k" (e.g., "$199k-349k"), explicitly expand both numbers with dollars signs (e.g., "$199,000 - $349,000").
3. Remove any "USD" currency indicators (e.g., change "$289,000 USD" to "$289,000").
"""
    
    for attempt in range(3):
        try:
            print("      [LLM] Requesting price extraction from Gemini...")
            response = client.models.generate_content(
                model='gemini-2.5-flash-lite',
                contents=prompt,
            )
            print("      [LLM] Received response!")
            return response.text.strip()
        except Exception as e:
            print(f"      [LLM] Extraction error (Attempt {attempt+1}): {e}")
            time.sleep(15)
            
    return "Error"

def _process_price_string(price_str):
    if price_str in ["Error", "Not Found", "Fetch Failed"]:
        return price_str
        
    def parse_num(s):
        s = s.replace(',', '')
        multiplier = 1
        if s.lower().endswith('k'):
            multiplier = 1000
            s = s[:-1]
        try:
            return float(s) * multiplier
        except ValueError:
            return None

    matches = re.findall(r'\d+(?:,\d+)*(?:\.\d+)?(?:[kK])?', price_str)
    
    if not matches:
        return price_str
        
    if len(matches) == 1:
        return price_str
        
    valid_nums = []
    for m in matches:
        val = parse_num(m)
        if val is not None:
            valid_nums.append(val)
            
    if not valid_nums:
        return price_str
        
    avg = sum(valid_nums) / len(valid_nums)
    if avg.is_integer():
        return f"${int(avg):,}"
    else:
        return f"${avg:,.2f}"

def main():
    client = init_gemini()

    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    service_json_string = os.environ.get("SERVICE_ACCOUNT_JSON")
    if service_json_string:
        service_account_info = json.loads(service_json_string)
    else:
        # Fallback for local testing
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
            print(f"Error: {SERVICE_ACCOUNT_FILE} not found locally and SERVICE_ACCOUNT_JSON env var missing.")
            return
        with open(SERVICE_ACCOUNT_FILE, "r") as f:
            service_account_info = json.load(f)

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
        
    try:
        last_updated_col_index = headers.index("Last Updated") + 1
    except ValueError:
        print("Could not find 'Last Updated' column exactly.")
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
                worksheet.update_cell(sheet_row_num, last_updated_col_index, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                time.sleep(2) # Google Sheets API rate limit safety
                continue
                
            price = extract_price_with_llm(client, text_content, model_name)
            print(f"--> Extracted Price: {price}")
            
            price = _process_price_string(price)
            if price not in ["Error", "Not Found", "Fetch Failed", ""]:
                print(f"--> Processed Price for Sheets: {price}")
            
            if price == "Error":
                print("Skipping this model due to repeated API errors.")
                worksheet.update_cell(sheet_row_num, price_col_index, "API Error")
                worksheet.update_cell(sheet_row_num, last_updated_col_index, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                time.sleep(2)
                continue
                
            # Update the specific cell dynamically
            print(f"      [Sheets] Saving price ({price}) to Google Sheets...")
            worksheet.update_cell(sheet_row_num, price_col_index, price)
            worksheet.update_cell(sheet_row_num, last_updated_col_index, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            print("      [Sheets] Saved successfully! Sleeping for 13s...")
            
            # Rate limit for free tier is 15 RPM, but we saw a limit of 5 for gemini sometimes
            # Google Sheets API also has limits, sleep handles both.
            time.sleep(13)
            
        browser.close()
        
    print("Done! The Google Sheet has been successfully updated.")

if __name__ == "__main__":
    main()
