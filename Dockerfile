# Use a version of Python that includes Playwright dependencies
FROM ://microsoft.com

# Set the working directory inside the container
WORKDIR /app

# Copy your requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the Chromium browser for Playwright
RUN playwright install chromium

# Copy the rest of your code
COPY . .

# Tell it to run your script
CMD ["python", "scraper.py"]
