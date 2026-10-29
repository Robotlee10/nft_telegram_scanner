import subprocess
import sys
import time

def run_service(script_name):
    print(f"[Runner] Starting {script_name}...")
    return subprocess.Popen([sys.executable, "-u", script_name])

if __name__ == "__main__":
    print("==================================================")
    print(" Starting Bot-Hosting Dual-Service Supervisor")
    print("==================================================")
    
    scanner_process = run_service("nft_telegram_scanner.py")
    worker_process = run_service("execution_worker.py")

    try:
        while True:
            for p, name in [(scanner_process, "nft_telegram_scanner.py"), (worker_process, "execution_worker.py")]:
                if p.poll() is not None:
                    print(f"[Runner] {name} exited. Restarting...")
                    if name == "nft_telegram_scanner.py":
                        scanner_process = run_service(name)
                    else:
                        worker_process = run_service(name)
            time.sleep(5)
    except KeyboardInterrupt:
        scanner_process.terminate()
        worker_process.terminate()
        sys.exit(0)
        
