# Use official Ubuntu 22.04 base image
FROM ubuntu:22.04

# Prevent interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive

# Install core utilities and software-properties-common
RUN apt-get update && apt-get install -y \
    software-properties-common \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Add deadsnakes PPA and install Python 3.12 with setuptools (which contains compatibility tools)
RUN add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
    python3.12 \
    python3.12-dev \
    python3.12-setuptools \
    && rm -rf /var/lib/apt/lists/*

# Ensure python3.12 is the default python3 binary
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1

# Bootstrap clean, modern pip directly via get-pip.py (bypassing broken system wrappers)
RUN curl -sS https://bootstrap.pypa.io/get-pip.py -o get-pip.py \
    && python3 get-pip.py \
    && rm get-pip.py

# Install Node.js (LTS version) for npx tools
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# Install the official Binance Agentic Wallet toolkit via npx
RUN npx --yes skills add binance/binance-skills-hub/skills/binance-web3/binance-agentic-wallet

# Set working directory inside the container
WORKDIR /app

# Copy project requirements and install Python dependencies cleanly
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

# Copy all project files into the container
COPY . .

# Command to run both scripts concurrently using runner.py
CMD ["python3", "-u", "runner.py"]
