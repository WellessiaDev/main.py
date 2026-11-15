import os
import time
import multiprocessing
import random
import hashlib
import json
import threading
import http.server
import socketserver
import sys

# --- CONFIGURATION ---
# Adjust these based on your Railway plan limits
MEMORY_TARGET_GB = 4      # Increase to match your plan's memory limit (e.g., 8, 16)
PORT = 8080               # Standard port for Railway
EGRESS_PAYLOAD_MB = 50    # Size of data served per HTTP request
DISK_STRESS_ENABLED = True
DISK_STRESS_FILE_SIZE_MB = 100 # Size of the temp file for disk I/O
LOG_INTERVAL = 10         # Seconds between status logs

# --- CPU WORKER ---
def cpu_worker(core_id, stop_event):
    """
    Consumes CPU by performing heavy cryptographic and mathematical operations.
    This ensures 100% CPU utilization on the assigned core.
    """
    print(f"[CPU] Worker {core_id} started.")
    counter = 0
    hash_data = b"0123456789abcdef"
    
    while not stop_event.is_set():
        # 1. Heavy Hashing (SHA-256 is CPU intensive)
        for _ in range(1000):
            hash_data = hashlib.sha256(hash_data).digest()
        
        # 2. Complex Math (Prime checking approximation)
        # Generates a large random number and checks for factors
        large_num = random.getrandbits(64)
        divisor = 3
        while divisor * divisor <= large_num:
            if large_num % divisor == 0:
                break
            divisor += 2
            
        # 3. String Manipulation (High overhead)
        temp_str = "A" * 1000
        temp_str = temp_str[::-1] * 10
        
        counter += 1
        if counter % 10000 == 0:
            print(f"[CPU] Worker {core_id}: Iterations {counter}")

# --- MEMORY HOG ---
def memory_hog(target_gb):
    """
    Allocates and retains a large amount of RAM to maximize memory-time billing.
    """
    print(f"[MEM] Allocating {target_gb}GB of memory...")
    size_bytes = int(target_gb * 1024 * 1024 * 1024)
    
    # Use a bytearray for efficient allocation
    # This data must be kept in a global variable to prevent GC from freeing it
    memory_pool = bytearray(size_bytes)
    
    # Fill it with random data to ensure it's not compressed by the OS
    # and to simulate active memory usage
    for i in range(0, size_bytes, 4096):
        memory_pool[i:i+4096] = os.urandom(4096)
        
    print(f"[MEM] Memory allocated: {len(memory_pool) / (1024**3):.2f} GB")
    return memory_pool

# --- EGRESS GENERATOR ---
def egress_generator(port):
    """
    Generates network egress by serving large, non-compressible data streams.
    """
    class EgressHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # Generate a large random payload (e.g., 50MB per request)
            payload_size = EGRESS_PAYLOAD_MB * 1024 * 1024
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
        
        def log_message(self, format, *args):
            pass # Suppress logs to save I/O

    print(f"[NET] Starting egress server on port {port}")
    with socketserver.TCPServer(("", port), EgressHandler) as httpd:
        httpd.serve_forever()

# --- DISK I/O STRESS ---
def disk_stress_worker(stop_event, file_size_mb):
    """
    Continuously writes and reads a large file to stress disk I/O.
    """
    if not DISK_STRESS_ENABLED:
        return
        
    file_path = "/tmp/stress_test.bin"
    file_size_bytes = file_size_mb * 1024 * 1024
    print(f"[DISK] Starting disk stress test. File size: {file_size_mb}MB")
    
    # Initial write
    with open(file_path, 'wb') as f:
        for _ in range(0, file_size_bytes, 4096):
            f.write(os.urandom(4096))
            
    read_buffer = bytearray(4096)
    while not stop_event.is_set():
        # Read
        with open(file_path, 'rb') as f:
            for _ in range(0, file_size_bytes, 4096):
                f.read(4096)
                
        # Write (overwrite)
        with open(file_path, 'wb') as f:
            for _ in range(0, file_size_bytes, 4096):
                f.write(os.urandom(4096))
                
        time.sleep(0.1) # Small delay to prevent 100% disk saturation causing I/O errors

# --- MAIN ORCHESTRATOR ---
def main():
    print("[INIT] Starting Resource Hog System...")
    cpu_count = multiprocessing.cpu_count()
    print(f"[INIT] Detected {cpu_count} CPU cores.")
    
    # 1. Start CPU Workers
    stop_event = multiprocessing.Event()
    processes = []
    for i in range(cpu_count):
        p = multiprocessing.Process(target=cpu_worker, args=(i, stop_event))
        p.start()
        processes.append(p)
        
    # 2. Start Memory Hog in Main Process
    # Keeping it in main process is fine, but separate is better for isolation
    # We keep it in main to ensure it's never garbage collected
    global_memory_holder = memory_hog(MEMORY_TARGET_GB)
    
    # 3. Start Disk Stress Worker (if enabled)
    if DISK_STRESS_ENABLED:
        disk_thread = threading.Thread(target=disk_stress_worker, args=(stop_event, DISK_STRESS_FILE_SIZE_MB))
        disk_thread.daemon = True
        disk_thread.start()
        
    # 4. Start Egress Server
    # Run in a separate thread to not block the main loop
    egress_thread = threading.Thread(target=egress_generator, args=(PORT,))
    egress_thread.daemon = True
    egress_thread.start()
    
    # 5. Keep Main Process Alive & Log Status
    print("[INIT] All systems running. Waiting for termination...")
    try:
        while True:
            time.sleep(LOG_INTERVAL)
            # Force memory touch to ensure it's not swapped out (if possible)
            # This is a light touch to keep the page cache warm
            _ = global_memory_holder[0]
            
            # Check if any worker died
            for p in processes:
                if not p.is_alive():
                    print(f"[ERROR] CPU Worker {p.pid} died. Restarting...")
                    p = multiprocessing.Process(target=cpu_worker, args=(p.pid, stop_event))
                    p.start()
                    
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Stopping...")
        stop_event.set()
        for p in processes:
            p.join()
        print("[SHUTDOWN] Complete.")

if __name__ == "__main__":
    main()