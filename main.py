import os
import sys
import time
import multiprocessing
import random
import hashlib
import threading
import http.server
import socketserver
import signal
import resource
import gc
import errno
import logging

# --- CONFIGURATION ---
# Set to (Your Plan Limit - 1.5GB) to leave room for OS/Python overhead
MEMORY_TARGET_GB = 4      
PORT = 8080               
EGRESS_PAYLOAD_MB = 10    # Reduced to 10MB to prevent buffer overflow under high concurrency
DISK_STRESS_ENABLED = True
DISK_STRESS_FILE_SIZE_MB = 20 # Reduced to 20MB to avoid filling /tmp
DISK_STRESS_PATH = "/tmp/railway_stress.bin"
LOG_INTERVAL = 5          
MAX_FILE_DESCRIPTORS = 2048

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("ResourceHog")

# --- GLOBAL STATE ---
stop_event = multiprocessing.Event()
global_memory_holder = None
worker_processes = []

def set_resource_limits():
    """
    Increase file descriptor limits to prevent 'Too many open files' errors.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        new_limit = max(soft, hard, MAX_FILE_DESCRIPTORS)
        resource.setrlimit(resource.RLIMIT_NOFILE, (new_limit, new_limit))
        log.info(f"[SYS] File descriptor limit set to {resource.getrlimit(resource.RLIMIT_NOFILE)}")
    except Exception as e:
        log.warning(f"[SYS] Could not set file descriptor limits: {e}")

def cpu_worker(core_id):
    """
    Consumes CPU by performing heavy cryptographic and mathematical operations.
    """
    log.info(f"[CPU] Worker {core_id} started.")
    counter = 0
    hash_data = b"0123456789abcdef"
    
    try:
        while not stop_event.is_set():
            # 1. Heavy Hashing (SHA-256)
            for _ in range(1000):
                hash_data = hashlib.sha256(hash_data).digest()
            
            # 2. Complex Math (Prime checking approximation)
            large_num = random.getrandbits(64)
            divisor = 3
            while divisor * divisor <= large_num:
                if large_num % divisor == 0:
                    break
                divisor += 2
                
            # 3. String Manipulation
            temp_str = "A" * 1000
            temp_str = temp_str[::-1] * 10
            
            counter += 1
            if counter % 10000 == 0:
                log.info(f"[CPU] Worker {core_id}: Iterations {counter}")
                
    except Exception as e:
        log.error(f"[CPU] Worker {core_id} crashed: {e}", exc_info=True)

def allocate_memory(target_gb):
    """
    Allocates and retains a large amount of RAM.
    """
    global global_memory_holder
    log.info(f"[MEM] Allocating {target_gb}GB of memory...")
    size_bytes = int(target_gb * 1024 * 1024 * 1024)
    
    try:
        # Use a bytearray for efficient allocation
        global_memory_holder = bytearray(size_bytes)
        
        # Fill it with random data to ensure it's not compressed by the OS
        # Do this in chunks to avoid a single massive memory spike
        chunk_size = 4096 * 1024 # 4MB chunks
        for i in range(0, size_bytes, chunk_size):
            global_memory_holder[i:i+chunk_size] = os.urandom(min(chunk_size, size_bytes - i))
            
        log.info(f"[MEM] Memory allocated successfully: {len(global_memory_holder) / (1024**3):.2f} GB")
        return True
    except MemoryError:
        log.error("[MEM] ERROR: Memory allocation failed. Reduce MEMORY_TARGET_GB.")
        return False

def egress_generator(port):
    """
    Generates network egress by serving large, non-compressible data streams.
    """
    class EgressHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                # Generate a large random payload
                payload_size = EGRESS_PAYLOAD_MB * 1024 * 1024
                # Use os.urandom for true randomness (incompressible)
                payload = os.urandom(payload_size)
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                
                # Send in chunks to keep the connection active
                chunk_size = 65536
                sent = 0
                while sent < payload_size:
                    chunk = payload[sent:sent+chunk_size]
                    self.wfile.write(chunk)
                    sent += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass # Client disconnected, ignore
            except Exception as e:
                log.error(f"[NET] Error serving request: {e}")
        
        def log_message(self, format, *args):
            pass # Suppress logs to save I/O

    log.info(f"[NET] Starting egress server on port {port}")
    # Use ThreadingTCPServer to handle multiple concurrent requests
    with socketserver.ThreadingTCPServer(("", port), EgressHandler) as httpd:
        httpd.daemon_threads = True # Threads will die when main program exits
        httpd.serve_forever()

def disk_stress_worker():
    """
    Continuously writes and reads a large file to stress disk I/O.
    """
    if not DISK_STRESS_ENABLED:
        return
        
    file_path = DISK_STRESS_PATH
    file_size_bytes = DISK_STRESS_FILE_SIZE_MB * 1024 * 1024
    log.info(f"[DISK] Starting disk stress test. File size: {DISK_STRESS_FILE_SIZE_MB}MB at {file_path}")
    
    try:
        # Initial write
        with open(file_path, 'wb') as f:
            for _ in range(0, file_size_bytes, 4096):
                f.write(os.urandom(4096))
        
        log.info("[DISK] Initial write complete.")
        
        while not stop_event.is_set():
            try:
                # Read
                with open(file_path, 'rb') as f:
                    for _ in range(0, file_size_bytes, 4096):
                        f.read(4096)
                        
                # Write (overwrite)
                with open(file_path, 'wb') as f:
                    for _ in range(0, file_size_bytes, 4096):
                        f.write(os.urandom(4096))
                        
                time.sleep(0.1) # Small delay to prevent 100% disk saturation causing I/O errors
            except OSError as e:
                log.error(f"[DISK] Error (likely disk full or permission): {e}")
                # If disk is full, try to clean up and stop disk stress
                if os.path.exists(file_path):
                    os.remove(file_path)
                log.info("[DISK] Stopping disk stress due to error.")
                break
                
    except Exception as e:
        log.error(f"[DISK] Worker crashed: {e}", exc_info=True)

def monitor_workers():
    """
    Monitors worker processes and restarts them if they die.
    """
    global worker_processes
    while not stop_event.is_set():
        for p in worker_processes:
            if not p.is_alive():
                log.warning(f"[MONITOR] CPU Worker {p.pid} died. Restarting...")
                p = multiprocessing.Process(target=cpu_worker, args=(p.pid,))
                p.daemon = True
                p.start()
                # Update the list reference
                worker_processes[worker_processes.index(p)] = p
        time.sleep(2)

def main():
    global worker_processes
    log.info("[INIT] Starting Stable Resource Hog System...")
    log.info(f"[INIT] Config: MEM={MEMORY_TARGET_GB}GB, PORT={PORT}, DISK={DISK_STRESS_ENABLED}")
    
    # 1. Set Resource Limits
    set_resource_limits()
    
    # 2. Allocate Memory First
    # Do this before starting CPU workers to ensure we have the RAM headroom
    if not allocate_memory(MEMORY_TARGET_GB):
        sys.exit(1)
        
    # 3. Start CPU Workers
    cpu_count = multiprocessing.cpu_count()
    log.info(f"[INIT] Detected {cpu_count} CPU cores.")
    worker_processes = []
    for i in range(cpu_count):
        p = multiprocessing.Process(target=cpu_worker, args=(i,))
        p.daemon = True # Daemons will be killed if main process exits
        p.start()
        worker_processes.append(p)
        
    # 4. Start Disk Stress Worker
    if DISK_STRESS_ENABLED:
        disk_thread = threading.Thread(target=disk_stress_worker)
        disk_thread.daemon = True
        disk_thread.start()
        
    # 5. Start Egress Server
    egress_thread = threading.Thread(target=egress_generator, args=(PORT,))
    egress_thread.daemon = True
    egress_thread.start()
    
    # 6. Start Worker Monitor
    monitor_thread = threading.Thread(target=monitor_workers)
    monitor_thread.daemon = True
    monitor_thread.start()
    
    # 7. Signal Handling
    def signal_handler(signum, frame):
        log.info(f"[SIGNAL] Received signal {signum}. Shutting down...")
        stop_event.set()
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 8. Keep Main Process Alive & Monitor
    log.info("[INIT] All systems running. Waiting for termination...")
    try:
        while True:
            time.sleep(LOG_INTERVAL)
            
            # Force memory touch to ensure it's not swapped out
            if global_memory_holder:
                _ = global_memory_holder[0]
                
            # Periodic garbage collection to prevent memory leaks
            gc.collect()
            
    except KeyboardInterrupt:
        log.info("\n[SHUTDOWN] Stopping...")
        stop_event.set()
        for p in worker_processes:
            p.join(timeout=5)
        log.info("[SHUTDOWN] Complete.")

if __name__ == "__main__":
    main()
