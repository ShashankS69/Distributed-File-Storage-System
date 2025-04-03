FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 6969

# Add debugging tools
RUN pip install ipython

# Default command with improved error reporting
CMD ["python3", "-u", "app.py"]
