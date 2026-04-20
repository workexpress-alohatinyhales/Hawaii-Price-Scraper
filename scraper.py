import os
import time
import gspread
import json
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from google import genai

# Google Sheets Configuration
SPREADSHEET_ID = "1nNOLMAMiJdfMg9l9FfIlzW3DFJSuXcSJQNxaY53TYww"

def scrape_page_content(url, page_cache, playwright_page):
    if url in page_cache:
        return page_cache[url]
    
    print(f"Fetching URL: {url}")
    try:
        playwright_page.goto(url, timeout=30000, wait_until="networkidle")
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
    # 1. Initialize Gemini using GitHub Secret
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY secret not found.")
        return
    client = genai.Client(api_key=api_key)

    # 2. Load Service Account from GitHub Secret (The JSON text)
    service_json_string = os.environ.get("SERVICE_ACCOUNT_JSON")
    if not service_json_string:
        print("Error: SERVICE_ACCOUNT_JSON secret not found.")
        return
    
    try:
        service_account_info = json.loads(service_json_string)
    except Exception as e:
        print(f"Error parsing SERVICE_ACCOUNT_JSON: {e}")
        return
        
    print("Authenticating with Google Sheets...")
    # Using 'from_dict' instead of a file name
    gc = gspread.service_account_from_dict(service_account_info)
    
    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
        worksheet = sh.sheet1
    except Exception as e:
        print(f"Failed to open Google Sheet. Check sharing permissions! Error: {e}")
        return
        
    print("Loading data from Google Sheet...")
    records = worksheet.get_all_records()
    headers = worksheet.row_values(1)
    
    try:
        price_col_index = headers.index("Current Price") + 1
    except ValueError:
        print("Could not find 'Current Price' column exactly.")
        return
        
    page_cache = {}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        for index, row in enumerate(records):
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
                time.sleep(2)
                continue
                
            price = extract_price_with_llm(client, text_content, model_name)
            print(f"--> Extracted Price: {price}")
            
            if price == "Error":
                worksheet.update_cell(sheet_row_num, price_col_index, "API Error")
                time.sleep(2)
                continue
                
            worksheet.update_cell(sheet_row_num, price_col_index, price)
            time.sleep(13)
            
        browser.close()
        
    print("Done! The Google Sheet has been successfully updated.")

if __name__ == "__main__":
    main()
