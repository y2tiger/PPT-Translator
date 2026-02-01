FROM python:3.11-slim

# Install LibreOffice, poppler-utils, and fonts for PPT to image conversion
RUN apt-get update && apt-get install -y \
    libreoffice \
    poppler-utils \
    fontconfig \
    curl \
    unzip \
    # Korean fonts (fonts-nanum includes all nanum variants)
    fonts-nanum \
    fonts-noto-cjk \
    # Common fonts
    fonts-liberation \
    fonts-dejavu \
    fonts-freefont-ttf \
    # Microsoft core fonts (Arial, Times, etc.)
    fonts-wine \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Pretendard font (popular Korean font)
RUN mkdir -p /usr/share/fonts/pretendard \
    && curl -L -o /tmp/Pretendard.zip "https://github.com/orioncactus/pretendard/releases/download/v1.3.9/Pretendard-1.3.9.zip" \
    && unzip -j /tmp/Pretendard.zip "public/static/Pretendard-*.otf" -d /usr/share/fonts/pretendard/ \
    && rm /tmp/Pretendard.zip \
    && fc-cache -fv

# Configure fontconfig font substitution for missing Windows fonts
RUN mkdir -p /etc/fonts/conf.d && \
    echo '<?xml version="1.0"?>\n\
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n\
<fontconfig>\n\
  <!-- Substitute missing Korean fonts -->\n\
  <match target="pattern">\n\
    <test name="family"><string>맑은 고딕</string></test>\n\
    <edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit>\n\
  </match>\n\
  <match target="pattern">\n\
    <test name="family"><string>Malgun Gothic</string></test>\n\
    <edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit>\n\
  </match>\n\
  <match target="pattern">\n\
    <test name="family"><string>굴림</string></test>\n\
    <edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit>\n\
  </match>\n\
  <match target="pattern">\n\
    <test name="family"><string>Gulim</string></test>\n\
    <edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit>\n\
  </match>\n\
  <match target="pattern">\n\
    <test name="family"><string>돋움</string></test>\n\
    <edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit>\n\
  </match>\n\
  <match target="pattern">\n\
    <test name="family"><string>Dotum</string></test>\n\
    <edit name="family" mode="assign" binding="strong"><string>Noto Sans CJK KR</string></edit>\n\
  </match>\n\
  <!-- Substitute missing sans-serif fonts -->\n\
  <match target="pattern">\n\
    <test name="family"><string>Arial</string></test>\n\
    <edit name="family" mode="assign" binding="strong"><string>Liberation Sans</string></edit>\n\
  </match>\n\
  <match target="pattern">\n\
    <test name="family"><string>Calibri</string></test>\n\
    <edit name="family" mode="assign" binding="strong"><string>Liberation Sans</string></edit>\n\
  </match>\n\
  <match target="pattern">\n\
    <test name="family"><string>Trebuchet MS</string></test>\n\
    <edit name="family" mode="assign" binding="strong"><string>Liberation Sans</string></edit>\n\
  </match>\n\
</fontconfig>' > /etc/fonts/local.conf && \
    fc-cache -fv

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
