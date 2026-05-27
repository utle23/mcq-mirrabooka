#!/usr/bin/env python3
"""
MCQ Mirrabooka - PythonAnywhere Auto-Deploy Script
Run: python3 deploy_pythonanywhere.py
"""

import requests, os, zipfile, json, time, sys

# ── CONFIG (filled in by deploy script) ──────────────────────────────────────
USERNAME  = ""   # e.g. mcqmirrabooka
API_TOKEN = ""   # from pythonanywhere.com/user/USERNAME/account/#api_token
ZIP_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcq_webapp.zip")
# ─────────────────────────────────────────────────────────────────────────────

BASE     = f"https://www.pythonanywhere.com/api/v0/user/{USERNAME}"
HEADERS  = {"Authorization": f"Token {API_TOKEN}"}
DOMAIN   = f"{USERNAME}.pythonanywhere.com"
APP_PATH = f"/home/{USERNAME}/webapp"

def ok(r, label):
    if r.status_code not in (200, 201, 204):
        print(f"  ✗ {label}: {r.status_code} — {r.text[:200]}")
        sys.exit(1)
    print(f"  ✓ {label}")
    return r

def run_console(cmd):
    """Run a bash command via a new console and wait for output."""
    r = requests.post(f"{BASE}/consoles/", headers=HEADERS,
                      json={"executable": "bash", "arguments": "", "working_directory": f"/home/{USERNAME}"})
    ok(r, f"Open console")
    cid = r.json()["id"]
    time.sleep(2)
    requests.post(f"{BASE}/consoles/{cid}/send_input/", headers=HEADERS,
                  json={"input": cmd + "\nexit\n"})
    time.sleep(8)
    out = requests.get(f"{BASE}/consoles/{cid}/get_latest_output/", headers=HEADERS)
    print(f"     > {out.json().get('output','')[:300].strip()}")
    requests.delete(f"{BASE}/consoles/{cid}/", headers=HEADERS)

def upload_file(local_path, remote_path):
    with open(local_path, "rb") as f:
        r = requests.post(f"{BASE}/files/path{remote_path}",
                          headers=HEADERS, files={"content": f})
    ok(r, f"Upload {os.path.basename(local_path)}")

def main():
    if not USERNAME or not API_TOKEN:
        print("ERROR: Fill in USERNAME and API_TOKEN at the top of this script first.")
        sys.exit(1)

    print(f"\n{'='*55}")
    print(f"  MCQ Mirrabooka — PythonAnywhere Deploy")
    print(f"  Target: https://{DOMAIN}")
    print(f"{'='*55}\n")

    # 1. Upload zip
    print("1. Uploading webapp zip...")
    upload_file(ZIP_PATH, f"/home/{USERNAME}/mcq_webapp.zip")

    # 2. Unzip + install deps
    print("\n2. Unzipping and installing dependencies...")
    run_console(
        f"cd /home/{USERNAME} && "
        f"rm -rf webapp && "
        f"unzip -q mcq_webapp.zip && "
        f"pip3 install --user flask openpyxl werkzeug --quiet && "
        f"mkdir -p {APP_PATH}/uploads && "
        f"echo DONE"
    )

    # 3. Create WSGI file
    print("\n3. Writing WSGI file...")
    wsgi_content = f"""import sys, os
sys.path.insert(0, '{APP_PATH}')
from app import app as application
"""
    r = requests.post(f"{BASE}/files/path/home/{USERNAME}/webapp/wsgi.py",
                      headers=HEADERS, files={"content": ("wsgi.py", wsgi_content.encode())})
    ok(r, "Write wsgi.py")

    # 4. Create web app
    print("\n4. Creating web app...")
    r = requests.post(f"{BASE}/webapps/", headers=HEADERS,
                      json={"domain_name": DOMAIN, "python_version": "python311"})
    if r.status_code == 409:
        print("  ✓ Web app already exists, continuing...")
    else:
        ok(r, "Create web app")

    # 5. Configure WSGI path
    print("\n5. Configuring WSGI path...")
    r = requests.patch(f"{BASE}/webapps/{DOMAIN}/",
                       headers=HEADERS,
                       json={"source_directory": APP_PATH,
                             "working_directory": APP_PATH,
                             "virtualenv_path": ""})
    ok(r, "Set source/working directory")

    # 6. Set WSGI file content
    print("\n6. Setting WSGI file content...")
    r = requests.post(f"{BASE}/webapps/{DOMAIN}/wsgifile/",
                      headers=HEADERS,
                      data=wsgi_content)
    if r.status_code not in (200, 201):
        # Try PUT
        r = requests.put(f"{BASE}/webapps/{DOMAIN}/wsgifile/",
                         headers=HEADERS, data=wsgi_content)
    ok(r, "Configure WSGI content")

    # 7. Reload
    print("\n7. Reloading web app...")
    r = requests.post(f"{BASE}/webapps/{DOMAIN}/reload/", headers=HEADERS)
    ok(r, "Reload web app")

    print(f"\n{'='*55}")
    print(f"  DEPLOY COMPLETE!")
    print(f"  URL: https://{DOMAIN}")
    print(f"  Login: location=mirrabooka  |  user=7777  |  admin=77771")
    print(f"{'='*55}\n")

if __name__ == "__main__":
    main()
