FROM python:3.11-slim

Prevent Python from buffering stdout/stderr

ENV PYTHONUNBUFFERED=1

Set working directory

WORKDIR /app

Install system dependencies required for Playwright

RUN apt-get update && apt-get install -y 
wget 
gnupg 
ca-certificates 
fonts-liberation 
libasound2 
libatk-bridge2.0-0 
libatk1.0-0 
libc6 
libcairo2 
libcups2 
libdbus-1-3 
libdrm2 
libexpat1 
libfontconfig1 
libgbm1 
libgcc1 
libglib2.0-0 
libgtk-3-0 
libnspr4 
libnss3 
libpango-1.0-0 
libpangocairo-1.0-0 
libstdc++6 
libx11-6 
libx11-xcb1 
libxcb1 
libxcomposite1 
libxdamage1 
libxext6 
libxfixes3 
libxrandr2 
libxrender1 
libxshmfence1 
libxss1 
libxtst6 
lsb-release 
xdg-utils 
&& rm -rf /var/lib/apt/lists/*

Copy requirements first for better layer caching

COPY requirements.txt .

Install Python dependencies

RUN pip install --no-cache-dir -r requirements.txt

Install Playwright browsers

RUN playwright install chromium

Copy the rest of the project files

COPY . .

Default command

CMD ["python", "bot.py"]
