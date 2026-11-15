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

# --- CONFIGURATION ---
# IMPORTANT: Set MEMORY_TARGET_GB to (Your Plan Limit - 1)
# e.g., If you have 8GB, set this to 7.
MEMORY_TARGET_GB = 7      
PORT = 8080               
EGRESS_PAYLOAD_MB = 25    # Reduced to 25MB to prevent buffer overflow during high concurrency
DISK_STRESS_ENABLED = True
DISK_STRESS_FILE_SIZE_MB = 50 # Reduced to 50MB to avoid filling /tmp
DISK_STRESS_PATH = "/tmp/railway_stress.bin"
LOG_INTERVAL = 10         
MAX_FILE_DESCRIPTORS = 1024 # Increase if you hammer with many curl requests

# --- GLOBAL STATE ---
stop_event = multiprocessing.Event()
global_memory_holder = None

def set_resource_limits():
    """
    Increase file descriptor limits to prevent 'Too many open files' errors.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard < MAX_FILE_DESCRIPTORS:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        else:
            resource.setrlimit(resource.RLIMIT_NOFILE, (MAX_FILE_DESCRIPTORS, MAX_FILE_DESCRIPTORS))
        print(f"[SYS] File descriptor limit set to {resource.getrlimit(resource.RLIMIT_NOFILE)}")
    except Exception as e:
        print(f"[SYS] Warning: Could not set file descriptor limits: {e}")

def cpu_worker(core_id):
    """
    Consumes CPU by performing heavy cryptographic and mathematical operations.
    """
    print(f"[CPU] Worker {core_id} started.")
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
                print(f"[CPU] Worker {core_id}: Iterations {counter}")
                
    except Exception as e:
        print(f"[CPU] Worker {core_id} crashed: {e}")
        import traceback
        traceback.print_exc()

def allocate_memory(target_gb):
    """
    Allocates and retains a large amount of RAM.
    """
    global global_memory_holder
    print(f"[MEM] Allocating {target_gb}GB of memory...")
    size_bytes = int(target_gb * 1024 * 1024 * 1024)
    
    try:
        # Use a bytearray for efficient allocation
        global_memory_holder = bytearray(size_bytes)
        
        # Fill it with random data to ensure it's not compressed by the OS
        # Do this in chunks to avoid a single massive memory spike
        chunk_size = 4096 * 1024 # 4MB chunks
        for i in range(0, size_bytes, chunk_size):
            global_memory_holder[i:i+chunk_size] = os.urandom(min(chunk_size, size_bytes - i))
            
        print(f"[MEM] Memory allocated successfully: {len(global_memory_holder) / (1024**3):.2f} GB")
        return True
    except MemoryError:
        print("[MEM] ERROR: Memory allocation failed. Reduce MEMORY_TARGET_GB.")
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
                # Note: This is CPU intensive. If CPU is already 100%, this might slow down.
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
                print(f"[NET] Error serving request: {e}")
        
        def log_message(self, format, *args):
            pass # Suppress logs to save I/O

    print(f"[NET] Starting egress server on port {port}")
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
    print(f"[DISK] Starting disk stress test. File size: {DISK_STRESS_FILE_SIZE_MB}MB at {file_path}")
    
    try:
        # Initial write
        with open(file_path, 'wb') as f:
            for _ in range(0, file_size_bytes, 4096):
                f.write(os.urandom(4096))
        
        print("[DISK] Initial write complete.")
        
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
                print(f"[DISK] Error (likely disk full or permission): {e}")
                # If disk is full, try to clean up and stop disk stress
                if os.path.exists(file_path):
                    os.remove(file_path)
                print("[DISK] Stopping disk stress due to error.")
                break
                
    except Exception as e:
        print(f"[DISK] Worker crashed: {e}")
        import traceback
        traceback.print_exc()

def main():
    print("[INIT] Starting Hardened Resource Hog System...")
    print(f"[INIT] Config: MEM={MEMORY_TARGET_GB}GB, PORT={PORT}, DISK={DISK_STRESS_ENABLED}")
    
    # 1. Set Resource Limits
    set_resource_limits()
    
    # 2. Allocate Memory First
    # Do this before starting CPU workers to ensure we have the RAM headroom
    if not allocate_memory(MEMORY_TARGET_GB):
        sys.exit(1)
        
    # 3. Start CPU Workers
    cpu_count = multiprocessing.cpu_count()
    print(f"[INIT] Detected {cpu_count} CPU cores.")
    processes = []
    for i in range(cpu_count):
        p = multiprocessing.Process(target=cpu_worker, args=(i,))
        p.daemon = True # Daemons will be killed if main process exits
        p.start()
        processes.append(p)
        
    # 4. Start Disk Stress Worker
    if DISK_STRESS_ENABLED:
        disk_thread = threading.Thread(target=disk_stress_worker)
        disk_thread.daemon = True
        disk_thread.start()
        
    # 5. Start Egress Server
    egress_thread = threading.Thread(target=egress_generator, args=(PORT,))
    egress_thread.daemon = True
    egress_thread.start()
    
    # 6. Signal Handling
    def signal_handler(signum, frame):
        print(f"\n[SIGNAL] Received signal {signum}. Shutting down...")
        stop_event.set()
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 7. Keep Main Process Alive & Monitor
    print("[INIT] All systems running. Waiting for termination...")
    try:
        while True:
            time.sleep(LOG_INTERVAL)
            
            # Force memory touch to ensure it's not swapped out
            if global_memory_holder:
                _ = global_memory_holder[0]
                
            # Check if any worker died
            for p in processes:
                if not p.is_alive():
                    print(f"[WARN] CPU Worker {p.pid} died. Restarting...")
                    p = multiprocessing.Process(target=cpu_worker, args=(p.pid,))
                    p.daemon = True
                    p.start()
                    
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Stopping...")
        stop_event.set()
        for p in processes:
            p.join(timeout=5)
        print("[SHUTDOWN] Complete.")

if __name__ == "__main__":
    main()
