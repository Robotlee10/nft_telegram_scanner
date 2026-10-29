# Use official Python 3.12 slim image (Guaranteed Python 3.12, pip, and setuptools included)
FROM python:3.12-slim

# Prevent interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive

# Install curl, git, and Node.js dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (LTS version) for npx tools
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install the official Binance Agentic Wallet toolkit via npx
RUN npx --yes skills add binance/binance-skills-hub/skills/binance-web3/binance-agentic-wallet

# Set working directory inside the container
WORKDIR /app

# Copy project requirements and install Python dependencies natively
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

# Copy all project files into the container
COPY . .

# Command to run both scripts concurrently using runner.py
CMD ["python3", "-u", "runner.py"]
