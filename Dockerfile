FROM python:3.11-slim

# Install LibreOffice, poppler-utils, and fonts for PPT to image conversion
RUN apt-get update && apt-get install -y \
    libreoffice \
    poppler-utils \
    fontconfig \
    curl \
    unzip \
    # Korean fonts
    fonts-nanum \
    fonts-nanum-coding \
    fonts-nanum-extra \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    # Common fonts
    fonts-liberation \
    fonts-dejavu \
    fonts-freefont-ttf \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Pretendard font (popular Korean font)
RUN mkdir -p /usr/share/fonts/pretendard \
    && curl -L -o /tmp/Pretendard.zip "https://github.com/orioncactus/pretendard/releases/download/v1.3.9/Pretendard-1.3.9.zip" \
    && unzip -j /tmp/Pretendard.zip "public/static/Pretendard-*.otf" -d /usr/share/fonts/pretendard/ \
    && rm /tmp/Pretendard.zip \
    && fc-cache -fv

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create uploads directory
RUN mkdir -p uploads

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
