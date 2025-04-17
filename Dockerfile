FROM python:3.13-slim

WORKDIR /app

# Install dependencies clearly
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Explicitly install gunicorn and eventlet
RUN pip install --no-cache-dir gunicorn eventlet

# Copy the application code
COPY . .

# Create directories for volumes if they don't exist
RUN mkdir -p storage/original storage/replica

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=app.py

# Expose the port the app runs on
EXPOSE 6920

# Command to run the application using Gunicorn with Eventlet worker
CMD ["gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:6920", "app:app"]