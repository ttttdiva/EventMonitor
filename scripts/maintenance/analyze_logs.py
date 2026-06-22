import os
import sys

LOG_FILE = "logs/app.log"
SEARCH_TERMS = ["ERROR", "CRITICAL", "Exception", "Traceback"]

def tail(f, lines=20):
    total_lines_wanted = lines
    BLOCK_SIZE = 1024
    f.seek(0, 2)
    block_end_byte = f.tell()
    lines_to_go = total_lines_wanted
    block_number = -1
    blocks = []
    
    while lines_to_go > 0 and block_end_byte > 0:
        if block_end_byte - BLOCK_SIZE > 0:
            f.seek(block_number * BLOCK_SIZE, 2)
            blocks.append(f.read(BLOCK_SIZE))
        else:
            f.seek(0, 0)
            blocks.append(f.read(block_end_byte))
        lines_found = blocks[-1].count(b'\n')
        lines_to_go -= lines_found
        block_end_byte -= BLOCK_SIZE
        block_number -= 1
    
    all_read_text = b''.join(reversed(blocks))
    return all_read_text.splitlines()[-total_lines_wanted:]

def main():
    if not os.path.exists(LOG_FILE):
        print(f"Log file not found: {LOG_FILE}")
        return

    print(f"Analyzing last 1000 lines of {LOG_FILE}...")
    
    try:
        with open(LOG_FILE, 'rb') as f:
            lines = tail(f, 1000)
        
        lines = [line.decode('utf-8', errors='replace') for line in lines]
            
        print(f"Read {len(lines)} lines.")
        
        error_lines = []
        for line in lines:
            if any(term in line for term in SEARCH_TERMS):
                error_lines.append(line)
        
        if error_lines:
            print(f"Found {len(error_lines)} error lines:")
            for line in error_lines[-20:]: # Show last 20 errors
                print(line)
        else:
            print("No recent errors found in the last 1000 lines.")
            
    except Exception as e:
        print(f"Error reading log file: {e}")

if __name__ == "__main__":
    main()
