from cookie_manager import CookieManager
import os

# Change to the project root directory
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cm = CookieManager()
files = cm.get_cookie_files()
print(f"Found {len(files)} cookie files:")
for f in files:
    print(f"- {f.name}")
