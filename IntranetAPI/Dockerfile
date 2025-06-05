FROM python:3.12-bookworm

# Set working directory to /app
WORKDIR /app

# Set environment variable
ENV PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright

# Copy dependencies and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers and dependencies
RUN pip install playwright==1.51.0 && \
    playwright install --with-deps

# Copy the entire project to the container
COPY . .

# Change working directory to src, where the Python script is located
WORKDIR /app/src

# Expose port 3034
EXPOSE 3034

# Start the Python script
CMD ["uvicorn", "chat_api:asgi_app", "--host", "0.0.0.0", "--port", "3034"]

