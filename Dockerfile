# Use an official lightweight Ubuntu base image to support both Python & Node.js
FROM ubuntu:22.04

# Prevent interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.12, Node.js, npm, git, and build essentials
RUN apt-get update && apt-get install -y \
    python3.12 \
    python3-pip \
    python3-dev \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (LTS version) so we can run npm and npx tools
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# Install the official Binance Agentic Wallet toolkit globally
RUN npm install -g npx && npx skills add binance/binance-skills-hub/skills/binance-web3/binance-agentic-wallet

# Set working directory inside the container
WORKDIR /app

# Copy project requirements and install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy all project files into the container
COPY . .

# Command to run both scripts concurrently using python3
CMD ["python3", "-u", "runner.py"]
