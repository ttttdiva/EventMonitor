import shutil
import sys

cmd = "gemini"
resolved = shutil.which(cmd)
print(f"Command '{cmd}' resolves to: {resolved}")

cmd_python = "python"
resolved_python = shutil.which(cmd_python)
print(f"Command '{cmd_python}' resolves to: {resolved_python}")

if resolved:
    print("SUCCESS: gemini found")
else:
    print("FAILURE: gemini not found (this assumes it is in PATH)")
