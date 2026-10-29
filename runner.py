import os
import sys
import time
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Render assigns a dynamic port via environment variables (default to 10000 if local)
PORT = int(os.getenv("PORT", 10000))

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Handles Render web traffic health checks so the service stays active."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"NFT Agentic Service is active and healthy!")
    
    def log_message(self, format, *args):
        # Suppress spammy HTTP request logs to keep console clean
        return

def run_health_server():
    """Runs a minimal HTTP server to satisfy Render's web service port requirement."""
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    print(f"[Runner] Health check web server listening on port {PORT}...")
    server.serve_forever()

def run_service(script_name):
    """Starts a python script process."""
    print(f"[Runner] Launching {script_name}...")
    return subprocess.Popen([sys.executable, "-u", script_name])

if __name__ == "__main__":
    print("==================================================")
    print(" Starting Render Free Web Service Supervisor")
    print("==================================================")

    # 1. Start the HTTP health server in a background thread (satisfies Render port requirement)
    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()

    # 2. Launch your background scanner and execution worker processes
    scanner_process = run_service("nft_telegram_scanner.py")
    worker_process = run_service("execution_worker.py")

    try:
        while True:
            for p, name in [(scanner_process, "nft_telegram_scanner.py"), (worker_process, "execution_worker.py")]:
                ret = p.poll()
                if ret is not None:
                    print(f"[Runner] Warning: {name} exited with code {ret}. Restarting...")
                    if name == "nft_telegram_scanner.py":
                        scanner_process = run_service(name)
                    else:
                        worker_process = run_service(name)
            time.sleep(5)
    except KeyboardInterrupt:
        print("[Runner] Shutting down services...")
        scanner_process.terminate()
        worker_process.terminate()
        sys.exit(0)
        
