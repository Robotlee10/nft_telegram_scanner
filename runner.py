import subprocess
import sys
import time

def run_service(script_name):
    """Starts a python script process."""
    print(f"[Runner] Launching {script_name}...")
    return subprocess.Popen([sys.executable, "-u", script_name])

if __name__ == "__main__":
    print("==================================================")
    print(" Starting Render Dual-Microservice Supervisor")
    print("==================================================")
    
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
        print("[Runner] Shutting down background processes...")
        scanner_process.terminate()
        worker_process.terminate()
        sys.exit(0)
